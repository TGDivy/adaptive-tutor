from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from adaptive_tutor.config import ServerSettings, TutorSettings, load_settings
from adaptive_tutor.dashboard import DashboardAuth, create_app
from adaptive_tutor.db import Database
from adaptive_tutor.demo import run_demo
from adaptive_tutor.models import LearnerContext


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
        server=ServerSettings(
            host="127.0.0.1", allow_unauthenticated_loopback=False
        ),
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
        anonymous_dashboard = client.get("/", follow_redirects=False)
        assert anonymous_dashboard.status_code == 303
        assert anonymous_dashboard.headers["location"] == "/login"

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
        assert "No learner evidence yet" in dashboard.text
        assert "Model usage" not in dashboard.text
        assert client.post("/actions/create-assignment").status_code == 403
        csrf = DashboardAuth(settings).csrf_value
        assert csrf is not None
        assert client.post(
            "/actions/create-assignment",
            data={"csrf": csrf},
        ).status_code == 503

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
        server=ServerSettings(
            host="0.0.0.0"  # noqa: S104 - verifies exposed-bind protection
        ),
    )

    with pytest.raises(ValueError, match="API token is required"):
        create_app(settings, database)


def test_rich_dashboard_assignment_and_agent_api_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_demo(tmp_path / "demo")
    assert result.config_path is not None
    token = "rich-dashboard-test-token"
    monkeypatch.setenv("ADAPTIVE_TUTOR_API_TOKEN", token)
    settings = load_settings(Path(result.config_path), require_file=True)
    database = Database(Path(result.database_path))

    class StubOrchestrator:
        def __init__(self) -> None:
            self.contexts: list[LearnerContext] = []
            self.failure: str | None = None

        def create_next_assignment(self, context: LearnerContext) -> dict[str, Any]:
            if self.failure:
                raise ValueError(self.failure)
            self.contexts.append(context)
            return {
                "existing": False,
                "id": "A-0042",
                "title": "Trace a bounded queue",
                "bundle": {"hidden_evaluator": "must not cross the API"},
            }

    orchestrator = StubOrchestrator()
    app = create_app(settings, database, orchestrator)  # type: ignore[arg-type]
    assignment_id = str(result.assignment["id"])

    with TestClient(app) as client:
        oversized_login = client.post(
            "/login",
            content="token=" + "x" * 5000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert oversized_login.status_code == 413

        login = client.post("/login", data={"token": token}, follow_redirects=False)
        assert login.status_code == 303
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert str(result.assignment["title"]) in dashboard.text
        assert "Recent change" in dashboard.text
        assert "Recent scores" in dashboard.text

        detail = client.get(f"/assignment/{assignment_id}")
        assert detail.status_code == 200
        assert str(result.assignment["title"]) in detail.text
        assert "hidden_evaluator" not in detail.text
        assert "Confidence" not in detail.text
        assert client.get("/assignment/A-9999").status_code == 404

        assert client.get("/api/v1/get_readiness").json()["domains"]
        active = client.get("/api/v1/get_active_assignment").json()["assignment"]
        assert active["id"] == assignment_id

        csrf = DashboardAuth(settings).csrf_value
        assert csrf is not None
        oversized_action = client.post(
            "/actions/create-assignment",
            content="csrf=" + "x" * 5000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert oversized_action.status_code == 413
        created_from_dashboard = client.post(
            "/actions/create-assignment",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert created_from_dashboard.status_code == 303
        assert len(orchestrator.contexts) == 1

        headers = {"Authorization": f"Bearer {token}"}
        created_from_api = client.post(
            "/api/v1/create_assignment",
            headers=headers,
            json={"available_minutes": 20, "energy": "low"},
        )
        assert created_from_api.status_code == 200
        assert created_from_api.json() == {
            "existing": False,
            "id": "A-0042",
            "title": "Trace a bounded queue",
        }
        assert orchestrator.contexts[-1].available_minutes == 20

        orchestrator.failure = "publication failed"
        failed_action = client.post(
            "/actions/create-assignment", data={"csrf": csrf}
        )
        assert failed_action.status_code == 502
        assert failed_action.json()["detail"] == "publication failed"

        logout = client.post("/logout", follow_redirects=False)
        assert logout.status_code == 303
        assert logout.headers["location"] == "/login"


def test_dashboard_auth_supports_explicit_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADAPTIVE_TUTOR_API_TOKEN", raising=False)
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        server=ServerSettings(allow_unauthenticated_loopback=True),
    )
    auth = DashboardAuth(settings)
    assert auth.session_value is None
    assert auth.csrf_value is None

    loopback = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )
    assert auth.authorize(loopback) == "loopback"
    with pytest.raises(HTTPException) as raised:
        auth.authorize(loopback, write=True)
    assert getattr(raised.value, "status_code", None) == 401

    remote = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("192.0.2.1", 1234),
        }
    )
    with pytest.raises(HTTPException) as raised:
        auth.authorize(remote)
    assert getattr(raised.value, "status_code", None) == 401
