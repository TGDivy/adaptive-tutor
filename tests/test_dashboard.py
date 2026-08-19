from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adaptive_tutor.config import TutorSettings
from adaptive_tutor.dashboard import create_app
from adaptive_tutor.db import Database


def test_dashboard_and_api_require_authentication_and_secure_writes(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    token = "dashboard-test-token"
    monkeypatch.setenv("ADAPTIVE_TUTOR_API_TOKEN", token)
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        database_path=database.path,
        learner_id="learner",
        server={"host": "127.0.0.1", "allow_unauthenticated_loopback": False},
    )
    app = create_app(settings, database)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"ready": True}
        schema = client.get("/api/v1/openapi.json")
        assert schema.status_code == 200
        assert "/api/v1/get_status" in schema.json()["paths"]
        unauthorized = client.get("/api/v1/get_status")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in unauthorized.headers["content-security-policy"]
        assert client.get("/").status_code == 401

        bad_login = client.post(
            "/login", data={"token": "wrong"}, follow_redirects=False
        )
        assert bad_login.status_code == 401
        login = client.post("/login", data={"token": token}, follow_redirects=False)
        assert login.status_code == 303
        assert "HttpOnly" in login.headers["set-cookie"]
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "systems foundations" in dashboard.text

        assert client.get("/api/v1/get_status").status_code == 200
        assert client.post("/api/v1/pause").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post("/api/v1/pause", headers=headers).json() == {"paused": True}
        assert client.get("/api/v1/get_status", headers=headers).json()["paused"] is True
        assert client.post("/api/v1/resume", headers=headers).json() == {"paused": False}
        generated = client.post(
            "/api/v1/generate_report?period=weekly", headers=headers
        )
        assert generated.status_code == 200
        assert generated.json()["period"] == "weekly"
        assert client.post(
            "/api/v1/generate_report?period=yearly", headers=headers
        ).status_code == 422
        assert client.post(
            "/api/v1/create_assignment",
            headers=headers,
            json={"available_minutes": 30, "energy": "medium"},
        ).status_code == 503


def test_exposed_dashboard_refuses_to_start_without_token(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADAPTIVE_TUTOR_API_TOKEN", raising=False)
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        database_path=database.path,
        server={"host": "0.0.0.0"},  # noqa: S104 - verifies exposed-bind protection
    )

    with pytest.raises(ValueError, match="API token is required"):
        create_app(settings, database)
