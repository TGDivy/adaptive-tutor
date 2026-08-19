from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from adaptive_tutor.config import GitHubSettings, TutorSettings
from adaptive_tutor.db import Database
from adaptive_tutor.jobs import EventStore
from adaptive_tutor.webhooks import webhook_router

SECRET = "webhook-test-secret"


def client(database: Database, tmp_path: Path, monkeypatch: object) -> TestClient:
    monkeypatch.setenv("ADAPTIVE_TUTOR_WEBHOOK_SECRET", SECRET)  # type: ignore[attr-defined]
    settings = TutorSettings(
        data_dir=tmp_path,
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    app = FastAPI()
    app.include_router(webhook_router(settings, EventStore(database)))
    return TestClient(app)


def signed_headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": "sha256=" + digest,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


def test_webhook_persists_enqueues_and_returns_before_work(
    database: Database, tmp_path: Path, monkeypatch: object
) -> None:
    body = json.dumps(
        {"ref": "refs/heads/main", "repository": {"full_name": "owner/learning-workspace"}}
    ).encode()
    test_client = client(database, tmp_path, monkeypatch)
    response = test_client.post("/webhooks/github", content=body, headers=signed_headers(body))
    assert response.status_code == 202
    assert response.json()["duplicate"] is False
    duplicate = test_client.post(
        "/webhooks/github", content=body, headers=signed_headers(body)
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert database.fetch_one("SELECT status FROM events") == {"status": "queued"}
    assert database.fetch_one("SELECT kind FROM jobs") == {"kind": "record_submission"}


def test_webhook_rejects_invalid_signature_and_wrong_repository(
    database: Database, tmp_path: Path, monkeypatch: object
) -> None:
    test_client = client(database, tmp_path, monkeypatch)
    body = b'{"repository":{"full_name":"owner/learning-workspace"}}'
    headers = signed_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert test_client.post("/webhooks/github", content=body, headers=headers).status_code == 401
    wrong = b'{"repository":{"full_name":"another/repository"}}'
    wrong_response = test_client.post(
        "/webhooks/github", content=wrong, headers=signed_headers(wrong)
    )
    assert (
        wrong_response.status_code == 403
    )
    assert database.fetch_one("SELECT COUNT(*) count FROM events") == {"count": 0}
