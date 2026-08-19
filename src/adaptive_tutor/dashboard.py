"""Authenticated private dashboard and machine-readable personal-agent API."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import TutorSettings
from .db import Database
from .jobs import EventStore, JobQueue
from .models import LearnerContext
from .orchestrator import TutorOrchestrator
from .reporting import ReportService
from .state import StatusService
from .webhooks import webhook_router


class DashboardAuth:
    def __init__(self, settings: TutorSettings) -> None:
        self.settings = settings

    @property
    def session_value(self) -> str | None:
        token = self.settings.api_token
        if not token:
            return None
        return hmac.new(
            token.encode(), b"adaptive-tutor-dashboard-session", hashlib.sha256
        ).hexdigest()

    def authorize(self, request: Request, *, write: bool = False) -> str:
        token = self.settings.api_token
        authorization = request.headers.get("Authorization", "")
        bearer = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else ""
        )
        if token and bearer and hmac.compare_digest(token, bearer):
            return "bearer"
        if write:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="State-changing API calls require a bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        session = request.cookies.get("adaptive_tutor_session", "")
        if self.session_value and session and hmac.compare_digest(self.session_value, session):
            return "session"
        client_host = request.client.host if request.client else ""
        if self.settings.server.allow_unauthenticated_loopback and client_host in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            return "loopback"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(
    settings: TutorSettings,
    database: Database,
    orchestrator: TutorOrchestrator | None = None,
) -> FastAPI:
    if settings.server.host not in {"127.0.0.1", "::1", "localhost"} and not settings.api_token:
        raise ValueError("A dashboard API token is required when binding beyond loopback")
    database.migrate()
    app = FastAPI(
        title="Adaptive Tutor",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    package_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=package_root / "templates")
    app.mount("/static", StaticFiles(directory=package_root / "static"), name="static")
    auth = DashboardAuth(settings)
    status_service = StatusService(database)
    reports = ReportService(database)
    app.include_router(webhook_router(settings, EventStore(database, JobQueue(database))))

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def read_auth(request: Request) -> str:
        return auth.authorize(request)

    def write_auth(request: Request) -> str:
        return auth.authorize(request, write=True)

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readiness_probe() -> JSONResponse:
        healthy, _ = database.integrity_check()
        code = 200 if healthy and database.migration_versions() else 503
        return JSONResponse(status_code=code, content={"ready": code == 200})

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request) -> Any:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login(request: Request) -> Any:
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="Login request is too large")
        form = parse_qs(body.decode("utf-8", errors="replace"))
        supplied = form.get("token", [""])[0]
        configured = settings.api_token
        if not configured or not hmac.compare_digest(configured, supplied):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "That access token was not accepted."},
                status_code=401,
            )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "adaptive_tutor_session",
            auth.session_value or "",
            httponly=True,
            secure=settings.server.host not in {"127.0.0.1", "::1", "localhost"},
            samesite="strict",
            max_age=12 * 60 * 60,
        )
        return response

    @app.post("/logout", include_in_schema=False)
    def logout() -> RedirectResponse:
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("adaptive_tutor_session")
        return response

    @app.get(
        "/",
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(read_auth)],
    )
    def dashboard(request: Request) -> Any:
        snapshot = status_service.get_status(
            settings.learner_id, settings.active_curriculum
        ).model_dump(mode="json")
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "status": snapshot,
                "weekly": _period_progress(database, settings.learner_id, 7),
                "monthly": _period_progress(database, settings.learner_id, 30),
                "reports": reports.recent(settings.learner_id, limit=4),
            },
        )

    @app.get("/api/v1/get_status", dependencies=[Depends(read_auth)])
    def get_status() -> dict[str, Any]:
        return status_service.get_status(
            settings.learner_id, settings.active_curriculum
        ).model_dump(mode="json")

    @app.get("/api/v1/get_readiness", dependencies=[Depends(read_auth)])
    def get_readiness() -> dict[str, Any]:
        snapshot = status_service.get_status(settings.learner_id, settings.active_curriculum)
        return {"curriculum": settings.active_curriculum, "domains": snapshot.readiness}

    @app.get("/api/v1/get_active_assignment", dependencies=[Depends(read_auth)])
    def get_active_assignment() -> dict[str, Any]:
        active = status_service.get_status(
            settings.learner_id, settings.active_curriculum
        ).active_assignment
        return {"assignment": active}

    @app.post("/api/v1/create_assignment", dependencies=[Depends(write_auth)])
    def create_assignment(context: LearnerContext) -> dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="GitHub orchestration is unavailable")
        return cast(dict[str, Any], _json_safe(orchestrator.create_next_assignment(context)))

    @app.post("/api/v1/generate_report", dependencies=[Depends(write_auth)])
    def generate_report(period: str = "weekly") -> dict[str, Any]:
        if period not in {"weekly", "monthly"}:
            raise HTTPException(status_code=422, detail="period must be weekly or monthly")
        report = reports.generate(
            settings.learner_id,
            settings.active_curriculum,
            period,  # type: ignore[arg-type]
        )
        return {
            "id": report.id,
            "period": report.period_type,
            "data": report.data,
            "markdown": report.markdown,
        }

    @app.post("/api/v1/pause", dependencies=[Depends(write_auth)])
    def pause() -> dict[str, bool]:
        status_service.set_paused(True)
        return {"paused": True}

    @app.post("/api/v1/resume", dependencies=[Depends(write_auth)])
    def resume() -> dict[str, bool]:
        status_service.set_paused(False)
        return {"paused": False}

    return app


def _period_progress(database: Database, learner_id: str, days: int) -> dict[str, Any]:
    modifier = f"-{days} days"
    return database.fetch_one(
        """
        SELECT COUNT(DISTINCT a.id) assignments,
               COUNT(DISTINCT at.id) attempts,
               COALESCE(ROUND(AVG(q.overall_score), 1), 0) average_score
        FROM assignments a
        LEFT JOIN attempts at ON at.assignment_id=a.id
        LEFT JOIN qualitative_evaluations q ON q.attempt_id=at.id
        WHERE a.learner_id=? AND a.created_at >= datetime('now', ?)
        """,
        (learner_id, modifier),
    ) or {"assignments": 0, "attempts": 0, "average_score": 0}


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if key != "bundle"}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
