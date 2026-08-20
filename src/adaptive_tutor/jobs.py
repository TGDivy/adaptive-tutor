"""Durable idempotent event ingestion and restart-safe job leasing."""

from __future__ import annotations

import hashlib
import json
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .db import Database
from .errors import TutorError
from .security import redact, sha256_digest
from .time import iso_now, utc_now

EVENT_JOB_KIND = {
    "push": "record_submission",
    "pull_request": "reconcile_pull_request",
    "workflow_run": "process_ci_result",
    "check_suite": "reconcile_checks",
    "check_run": "reconcile_checks",
    "issue_comment": "process_learner_command",
}


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    event_id: str | None
    worker_id: str
    lease_token: str


class JobQueue:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        deduplication_key: str,
        event_id: str | None = None,
        priority: int = 100,
        max_attempts: int = 5,
    ) -> str:
        job_id = str(uuid.uuid4())
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    id, event_id, kind, deduplication_key, payload_json,
                    priority, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_id,
                    kind,
                    deduplication_key,
                    json.dumps(dict(payload), sort_keys=True),
                    priority,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM jobs WHERE deduplication_key=?", (deduplication_key,)
            ).fetchone()
            if row is None:  # pragma: no cover - database invariant
                raise RuntimeError("Job enqueue did not produce a row")
            return str(row["id"])

    def claim(self, worker_id: str, *, lease_seconds: int = 900) -> Job | None:
        now = utc_now()
        now_text = now.isoformat(timespec="seconds")
        lease = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET status='dead_letter', leased_until=NULL,
                    worker_id=NULL, lease_token=NULL,
                    last_error='Worker lease expired after maximum attempts', updated_at=?
                WHERE status='running' AND leased_until < ? AND attempts >= max_attempts
                """,
                (now_text, now_text),
            )
            connection.execute(
                """
                UPDATE events SET status='failed',
                    error='Worker lease expired after maximum attempts'
                WHERE id IN (
                    SELECT event_id FROM jobs
                    WHERE status='dead_letter'
                      AND last_error='Worker lease expired after maximum attempts'
                )
                """
            )
            connection.execute(
                """
                UPDATE jobs SET status='queued', leased_until=NULL, worker_id=NULL,
                    lease_token=NULL, updated_at=?
                WHERE status='running' AND leased_until < ? AND attempts < max_attempts
                """,
                (now_text, now_text),
            )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status='queued' AND available_at <= ?
                ORDER BY priority ASC, created_at ASC LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            lease_token = uuid.uuid4().hex
            updated = connection.execute(
                """
                UPDATE jobs SET status='running', attempts=attempts+1,
                    leased_until=?, worker_id=?, lease_token=?,
                    lease_generation=lease_generation+1, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (lease, worker_id, lease_token, now_text, row["id"]),
            ).rowcount
            if updated != 1:
                return None
            return Job(
                id=str(row["id"]),
                kind=str(row["kind"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]) + 1,
                max_attempts=int(row["max_attempts"]),
                event_id=str(row["event_id"]) if row["event_id"] else None,
                worker_id=worker_id,
                lease_token=lease_token,
            )

    def heartbeat(self, job: Job, *, lease_seconds: int = 900) -> bool:
        now = utc_now()
        lease = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        updated = self.database.execute(
            """
            UPDATE jobs SET leased_until=?, updated_at=?
            WHERE id=? AND status='running' AND worker_id=? AND lease_token=?
            """,
            (lease, now.isoformat(timespec="seconds"), job.id, job.worker_id, job.lease_token),
        )
        return updated == 1

    def complete(self, job: Job) -> bool:
        now = iso_now()
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE jobs SET status='completed', completed_at=?, updated_at=?,
                    leased_until=NULL, worker_id=NULL, lease_token=NULL
                WHERE id=? AND status='running' AND worker_id=? AND lease_token=?
                """,
                (now, now, job.id, job.worker_id, job.lease_token),
            ).rowcount
            if updated and job.event_id:
                connection.execute(
                    "UPDATE events SET status='processed', processed_at=? WHERE id=?",
                    (now, job.event_id),
                )
            return updated == 1

    def fail(self, job: Job, error: BaseException) -> str:
        now = utc_now()
        retryable = isinstance(error, TutorError) and error.retryable
        exhausted = job.attempts >= job.max_attempts
        status = "dead_letter" if exhausted or not retryable else "queued"
        delay = min(3600, 15 * (2 ** max(job.attempts - 1, 0)))
        available = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        diagnostic = redact("".join(traceback.format_exception_only(type(error), error))).strip()
        diagnostic = diagnostic[:4000]
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE jobs SET status=?, available_at=?, leased_until=NULL,
                    worker_id=NULL, lease_token=NULL, last_error=?, updated_at=?
                WHERE id=? AND status='running' AND worker_id=? AND lease_token=?
                """,
                (
                    status,
                    available,
                    diagnostic,
                    now.isoformat(timespec="seconds"),
                    job.id,
                    job.worker_id,
                    job.lease_token,
                ),
            ).rowcount
            if not updated:
                return "lost_lease"
            if job.event_id:
                connection.execute(
                    "UPDATE events SET status=?, error=? WHERE id=?",
                    ("failed" if status == "dead_letter" else "retrying", diagnostic, job.event_id),
                )
        return status

    def counts(self) -> dict[str, int]:
        rows = self.database.fetch_all(
            "SELECT status, COUNT(*) count FROM jobs GROUP BY status ORDER BY status"
        )
        return {str(row["status"]): int(row["count"]) for row in rows}

    def worker_heartbeat(self, worker_id: str) -> None:
        if not worker_id or len(worker_id) > 100:
            raise ValueError("Worker identifier must contain 1 to 100 characters")
        now = iso_now()
        self.database.execute(
            """
            INSERT INTO worker_heartbeats(
                worker_id, status, started_at, heartbeat_at, stopped_at
            ) VALUES (?, 'running', ?, ?, NULL)
            ON CONFLICT(worker_id) DO UPDATE SET
                status='running', heartbeat_at=excluded.heartbeat_at, stopped_at=NULL
            """,
            (worker_id, now, now),
        )

    def worker_stopped(self, worker_id: str) -> None:
        now = iso_now()
        self.database.execute(
            """
            UPDATE worker_heartbeats
            SET status='stopped', heartbeat_at=?, stopped_at=?
            WHERE worker_id=?
            """,
            (now, now, worker_id),
        )


class EventStore:
    def __init__(self, database: Database, queue: JobQueue | None = None) -> None:
        self.database = database
        self.queue = queue or JobQueue(database)

    def ingest(
        self,
        *,
        event_type: str,
        delivery_id: str,
        payload: dict[str, Any],
    ) -> tuple[str, str | None, bool]:
        if not delivery_id or len(delivery_id) > 200:
            raise ValueError("A bounded delivery identifier is required")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        payload_digest = sha256_digest(raw)
        repository = (payload.get("repository") or {}).get("full_name")
        action = payload.get("action")
        event_id = str(uuid.uuid4())
        now = iso_now()
        with self.database.transaction() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO events(
                    id, source, event_type, action, repository, delivery_id,
                    payload_json, payload_digest, status, received_at
                ) VALUES (?, 'github', ?, ?, ?, ?, ?, ?, 'received', ?)
                """,
                (
                    event_id,
                    event_type,
                    str(action) if action is not None else None,
                    str(repository) if repository else None,
                    delivery_id,
                    raw,
                    payload_digest,
                    now,
                ),
            ).rowcount
            existing = connection.execute(
                "SELECT id FROM events WHERE source='github' AND delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if existing is None:  # pragma: no cover - database invariant
                raise RuntimeError("Event ingestion did not produce a row")
            actual_event_id = str(existing["id"])
            if not inserted:
                job = connection.execute(
                    "SELECT id FROM jobs WHERE event_id=?", (actual_event_id,)
                ).fetchone()
                return actual_event_id, str(job["id"]) if job else None, True
            job_kind = EVENT_JOB_KIND.get(event_type)
            if job_kind is None or event_type == "ping":
                connection.execute(
                    "UPDATE events SET status='ignored', processed_at=? WHERE id=?",
                    (now, actual_event_id),
                )
                return actual_event_id, None, False
            job_id = str(uuid.uuid4())
            deduplication_key = hashlib.sha256(
                f"github:{delivery_id}:{job_kind}".encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO jobs(
                    id, event_id, kind, deduplication_key, payload_json,
                    priority, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 100, 5, ?, ?, ?)
                """,
                (job_id, actual_event_id, job_kind, deduplication_key, raw, now, now, now),
            )
            connection.execute(
                "UPDATE events SET status='queued' WHERE id=?", (actual_event_id,)
            )
            return actual_event_id, job_id, False


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Callable[[dict[str, Any]], None]],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 900,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds or max(
            1.0, min(30.0, lease_seconds / 3)
        )
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("Worker heartbeat interval must be positive")

    def run_once(self) -> bool:
        self.queue.worker_heartbeat(self.worker_id)
        job = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        try:
            handler = self.handlers.get(job.kind)
            if handler is None:
                raise TutorError(f"No handler registered for job kind {job.kind}")
            self._run_handler(handler, job)
        except BaseException as exc:
            self.queue.fail(job, exc)
        else:
            self.queue.complete(job)
        return True

    def stop(self) -> None:
        self.queue.worker_stopped(self.worker_id)

    def _run_handler(self, handler: Callable[[dict[str, Any]], None], job: Job) -> None:
        stopped = threading.Event()

        def renew() -> None:
            while not stopped.wait(self.heartbeat_interval_seconds):
                self.queue.worker_heartbeat(self.worker_id)
                if not self.queue.heartbeat(job, lease_seconds=self.lease_seconds):
                    return

        heartbeat = threading.Thread(
            target=renew,
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            handler(job.payload)
        finally:
            stopped.set()
            heartbeat.join()
            self.queue.worker_heartbeat(self.worker_id)
