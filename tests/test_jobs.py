from __future__ import annotations

import time
from datetime import timedelta

from adaptive_tutor.db import Database
from adaptive_tutor.errors import ExternalServiceError
from adaptive_tutor.jobs import EventStore, Job, JobQueue, Worker
from adaptive_tutor.time import utc_now


def event_payload() -> dict[str, object]:
    return {
        "ref": "refs/heads/assignment/0001-example",
        "repository": {"full_name": "owner/learning-workspace"},
    }


def test_duplicate_delivery_creates_exactly_one_event_and_job(database: Database) -> None:
    events = EventStore(database)
    first = events.ingest(event_type="push", delivery_id="delivery-1", payload=event_payload())
    second = events.ingest(event_type="push", delivery_id="delivery-1", payload=event_payload())
    assert first[0:2] == second[0:2]
    assert first[2] is False
    assert second[2] is True
    assert database.fetch_one("SELECT COUNT(*) count FROM events") == {"count": 1}
    assert database.fetch_one("SELECT COUNT(*) count FROM jobs") == {"count": 1}


def test_job_leasing_completion_and_expired_recovery(database: Database) -> None:
    queue = JobQueue(database)
    job_id = queue.enqueue("work", {"value": 1}, deduplication_key="one")
    claimed = queue.claim("worker-a", lease_seconds=30)
    assert claimed is not None and claimed.id == job_id
    assert queue.claim("worker-b") is None
    database.execute(
        "UPDATE jobs SET leased_until=? WHERE id=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(), job_id),
    )
    recovered = queue.claim("worker-b")
    assert recovered is not None and recovered.id == job_id
    assert recovered.attempts == 2
    assert queue.complete(claimed) is False
    assert queue.fail(claimed, ValueError("late worker")) == "lost_lease"
    assert queue.heartbeat(recovered, lease_seconds=60)
    assert queue.complete(recovered)
    assert queue.counts() == {"completed": 1}


def test_expired_final_lease_dead_letters_instead_of_restarting(database: Database) -> None:
    queue = JobQueue(database)
    job_id = queue.enqueue("work", {}, deduplication_key="final-lease", max_attempts=1)
    claimed = queue.claim("worker-a", lease_seconds=30)
    assert claimed is not None
    database.execute(
        "UPDATE jobs SET leased_until=? WHERE id=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(), job_id),
    )

    assert queue.claim("worker-b") is None
    assert queue.counts() == {"dead_letter": 1}


def test_retryable_failure_backs_off_and_nonretryable_dead_letters(database: Database) -> None:
    queue = JobQueue(database)
    queue.enqueue("retry", {}, deduplication_key="retry", max_attempts=3)
    retry = queue.claim("worker")
    assert retry is not None
    assert queue.fail(retry, ExternalServiceError("network", retryable=True)) == "queued"
    assert queue.counts() == {"queued": 1}
    database.execute(
        "UPDATE jobs SET available_at=?",
        ((utc_now() - timedelta(seconds=1)).isoformat(timespec="seconds"),),
    )
    again = queue.claim("worker")
    assert again is not None
    assert queue.fail(again, ValueError("bad payload")) == "dead_letter"
    assert queue.counts() == {"dead_letter": 1}


def test_worker_dispatches_without_losing_failures(database: Database) -> None:
    queue = JobQueue(database)
    queue.enqueue("known", {"number": 7}, deduplication_key="known")
    seen: list[int] = []
    worker = Worker(queue, {"known": lambda payload: seen.append(int(payload["number"]))})
    assert worker.run_once()
    assert seen == [7]
    assert not worker.run_once()


def test_worker_records_idle_heartbeat_and_clean_stop(database: Database) -> None:
    queue = JobQueue(database)
    worker = Worker(queue, {}, worker_id="worker-health")

    assert worker.run_once() is False
    heartbeat = database.fetch_one(
        "SELECT status, heartbeat_at, stopped_at FROM worker_heartbeats WHERE worker_id=?",
        ("worker-health",),
    )
    assert heartbeat is not None
    assert heartbeat["status"] == "running"
    assert heartbeat["heartbeat_at"]
    assert heartbeat["stopped_at"] is None

    worker.stop()
    stopped = database.fetch_one(
        "SELECT status, stopped_at FROM worker_heartbeats WHERE worker_id=?",
        ("worker-health",),
    )
    assert stopped is not None
    assert stopped["status"] == "stopped"
    assert stopped["stopped_at"]


def test_worker_renews_health_and_job_lease_during_handler(database: Database) -> None:
    queue = JobQueue(database)
    queue.enqueue("slow", {}, deduplication_key="slow-job")
    lease_renewals = 0
    health_renewals = 0
    original_lease_heartbeat = queue.heartbeat
    original_worker_heartbeat = queue.worker_heartbeat

    def lease_heartbeat(job: Job, *, lease_seconds: int = 900) -> bool:
        nonlocal lease_renewals
        lease_renewals += 1
        return original_lease_heartbeat(job, lease_seconds=lease_seconds)

    def worker_heartbeat(worker_id: str) -> None:
        nonlocal health_renewals
        health_renewals += 1
        original_worker_heartbeat(worker_id)

    queue.heartbeat = lease_heartbeat  # type: ignore[method-assign]
    queue.worker_heartbeat = worker_heartbeat  # type: ignore[method-assign]
    worker = Worker(
        queue,
        {"slow": lambda _payload: time.sleep(0.05)},
        worker_id="worker-slow",
        heartbeat_interval_seconds=0.01,
    )

    assert worker.run_once() is True
    assert lease_renewals >= 1
    assert health_renewals >= 3
