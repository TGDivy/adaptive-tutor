"""Authenticated private dashboard and machine-readable personal-agent API."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from .assignments import AssignmentService
from .config import TutorSettings, load_settings
from .db import Database
from .errors import TutorError
from .github_setup import GitHubAppSetupService
from .jobs import EventStore, JobQueue
from .models import LearnerContext
from .orchestrator import TutorOrchestrator
from .reporting import ReportService
from .setup import LiveSetupExecutor, SetupService
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

    @property
    def csrf_value(self) -> str | None:
        token = self.settings.api_token
        if not token:
            return None
        return hmac.new(
            token.encode(), b"adaptive-tutor-dashboard-csrf", hashlib.sha256
        ).hexdigest()

    def authorize(self, request: Request, *, write: bool = False) -> str:
        token = self.settings.api_token
        authorization = request.headers.get("Authorization", "")
        bearer = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
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

    def authorize_browser_write(self, request: Request, csrf: str) -> None:
        if self.authorize(request) != "session":
            raise HTTPException(status_code=401, detail="Dashboard session required")
        expected = self.csrf_value
        if not expected or not csrf or not hmac.compare_digest(expected, csrf):
            raise HTTPException(status_code=403, detail="Invalid form token")


def create_app(
    settings: TutorSettings,
    database: Database,
    orchestrator: TutorOrchestrator | None = None,
    *,
    config_path: Path | None = None,
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

    def refresh_setup_settings() -> None:
        if config_path is None:
            raise HTTPException(status_code=503, detail="Server configuration path is unavailable")
        try:
            refreshed = load_settings(config_path, require_file=True)
        except (TutorError, ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if refreshed.database_path != settings.database_path:
            raise HTTPException(
                status_code=409,
                detail="The configured database changed; restart the tutor service",
            )
        settings.github = refreshed.github
        settings.codex = refreshed.codex
        settings.active_curriculum = refreshed.active_curriculum
        settings.active_profile = refreshed.active_profile
        settings.learner_id = refreshed.learner_id
        settings.curriculum_paths = refreshed.curriculum_paths

    def review_projection(assignment_id: str | None = None) -> dict[str, Any] | None:
        return status_service.review(
            settings.learner_id,
            assignment_id,
            github_owner=settings.github.owner,
            workspace_repo=settings.github.workspace_repo,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self' https://github.com"
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

    @app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_status_page(request: Request) -> Any:
        try:
            read_auth(request)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=303)
        refresh_setup_settings()
        run = SetupService(database).current()
        if run is None:
            raise HTTPException(status_code=404, detail="Guided setup has not been started")
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"setup": run, "csrf_token": auth.csrf_value},
        )

    @app.post("/setup/resume", include_in_schema=False)
    async def resume_setup_from_browser(request: Request) -> RedirectResponse:
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="Form request is too large")
        form = parse_qs(body.decode("utf-8", errors="replace"))
        auth.authorize_browser_write(request, form.get("csrf", [""])[0])
        if config_path is None:
            raise HTTPException(status_code=503, detail="Server configuration path is unavailable")
        try:
            refresh_setup_settings()
            SetupService(database).resume(
                LiveSetupExecutor(settings, database, config_path=config_path)
            )
        except (TutorError, ValueError, OSError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return RedirectResponse(url="/setup", status_code=303)

    @app.get("/setup/github-app", response_class=HTMLResponse, include_in_schema=False)
    def github_app_setup_page(request: Request) -> Any:
        try:
            read_auth(request)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=303)
        refresh_setup_settings()
        assert config_path is not None  # refresh_setup_settings checks this invariant
        run = SetupService(database).current()
        if run is None:
            raise HTTPException(status_code=404, detail="Guided setup has not been started")
        service = GitHubAppSetupService(settings, database, config_path)
        try:
            launch = service.start(run)
        except (TutorError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            service.close()
        return templates.TemplateResponse(
            request,
            "github_app_setup.html",
            {"setup": run, "launch": launch},
        )

    @app.get("/setup/github-app/callback", include_in_schema=False)
    def github_app_manifest_callback(code: str, state: str) -> RedirectResponse:
        refresh_setup_settings()
        assert config_path is not None  # refresh_setup_settings checks this invariant
        run = SetupService(database).current()
        if run is None:
            raise HTTPException(status_code=404, detail="Guided setup has not been started")
        service = GitHubAppSetupService(settings, database, config_path)
        try:
            installation_url = service.complete_manifest(run, code=code, state=state)
            refresh_setup_settings()
        except (TutorError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            service.close()
        return RedirectResponse(url=installation_url, status_code=303)

    @app.get("/setup/github-app/installed", include_in_schema=False)
    def github_app_installed_callback(
        installation_id: int,
        state: str,
        setup_action: str | None = None,
    ) -> RedirectResponse:
        if setup_action not in {None, "install", "update"}:
            raise HTTPException(status_code=400, detail="GitHub installation action is invalid")
        refresh_setup_settings()
        assert config_path is not None  # refresh_setup_settings checks this invariant
        run = SetupService(database).current()
        if run is None:
            raise HTTPException(status_code=404, detail="Guided setup has not been started")
        service = GitHubAppSetupService(settings, database, config_path)
        try:
            service.complete_installation(
                run,
                installation_id=installation_id,
                state=state,
            )
            refresh_setup_settings()
            SetupService(database).resume(
                LiveSetupExecutor(settings, database, config_path=config_path)
            )
        except (TutorError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            service.close()
        return RedirectResponse(url="/setup", status_code=303)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request) -> Any:
        try:
            read_auth(request)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=303)
        snapshot = status_service.get_status(
            settings.learner_id, settings.active_curriculum
        ).model_dump(mode="json")
        action = _assignment_action(snapshot.get("active_assignment"), settings, database)
        assessed = sum(item["assessed_concept_count"] for item in snapshot["readiness"])
        setup_required = snapshot["setup_status"] in {"not_started", "in_progress"}
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "status": snapshot,
                "weekly": _period_progress(database, settings.learner_id, 7),
                "monthly": _period_progress(database, settings.learner_id, 30),
                "reports": reports.recent(settings.learner_id, limit=4),
                "assignment_action": action,
                "assignment_state": _learner_assignment_state(
                    (snapshot.get("active_assignment") or {}).get("status"),
                    (snapshot.get("active_assignment") or {}).get("publication_error"),
                ),
                "assessed_concepts": assessed,
                "setup_required": setup_required,
                "setup_started": snapshot["setup_status"] == "in_progress",
                "csrf_token": auth.csrf_value,
                "can_create": (
                    not setup_required
                    and orchestrator is not None
                    and auth.csrf_value is not None
                ),
            },
        )

    @app.post("/actions/create-assignment", include_in_schema=False)
    async def create_assignment_from_dashboard(request: Request) -> RedirectResponse:
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(status_code=413, detail="Form request is too large")
        form = parse_qs(body.decode("utf-8", errors="replace"))
        auth.authorize_browser_write(request, form.get("csrf", [""])[0])
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="GitHub orchestration is unavailable")
        try:
            orchestrator.create_next_assignment(LearnerContext())
        except (TutorError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return RedirectResponse(url="/", status_code=303)

    @app.get(
        "/assignment/{assignment_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def assignment_detail(request: Request, assignment_id: str) -> Any:
        try:
            read_auth(request)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=303)
        active = AssignmentService(database).active(settings.learner_id)
        if active is None or active["id"] != assignment_id:
            raise HTTPException(status_code=404, detail="Active assignment not found")
        bundle = active.pop("bundle")
        public_files = [
            {"path": item.path, "role": item.role}
            for item in bundle.files
            if item.role not in {"reference", "evaluator", "instructions"}
        ]
        instructions = next(
            (item.content for item in bundle.files if item.role == "instructions"), ""
        )
        return templates.TemplateResponse(
            request,
            "assignment.html",
            {
                "assignment": active,
                "bundle": bundle.model_dump(mode="json", exclude={"hidden_evaluator"}),
                "instructions_html": _render_assignment_instructions(
                    instructions, str(active["title"])
                ),
                "public_files": public_files,
                "assignment_action": _assignment_action(active, settings, database),
                "assignment_state": _learner_assignment_state(
                    str(active["status"]), active.get("publication_error")
                ),
                "workspace_path": _demo_workspace(database),
                "csrf_token": auth.csrf_value,
            },
        )

    @app.get(
        "/review/{assignment_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def review_detail(request: Request, assignment_id: str) -> Any:
        try:
            read_auth(request)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=303)
        projection = review_projection(assignment_id)
        if projection is None:
            raise HTTPException(status_code=404, detail="Completed review not found")
        return templates.TemplateResponse(
            request,
            "review.html",
            {"projection": projection},
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

    @app.get("/api/v1/get_review", dependencies=[Depends(read_auth)])
    def get_review(assignment_id: str | None = None) -> dict[str, Any]:
        projection = review_projection(assignment_id)
        if projection is None:
            raise HTTPException(status_code=404, detail="Completed review not found")
        return projection

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


def _assignment_action(
    assignment: dict[str, Any] | None,
    settings: TutorSettings,
    database: Database,
) -> dict[str, str] | None:
    if assignment is None:
        return None
    pull_number = assignment.get("pull_number")
    if pull_number and settings.github.owner:
        return {
            "label": "Open pull request",
            "url": (
                f"https://github.com/{settings.github.owner}/"
                f"{settings.github.workspace_repo}/pull/{pull_number}"
            ),
            "external": "true",
            "method": "get",
        }
    if assignment.get("status") == "validated" and _demo_workspace(database) is None:
        return {
            "label": "Retry publication",
            "url": "/actions/create-assignment",
            "external": "false",
            "method": "post",
        }
    return {
        "label": (
            "Start assignment"
            if assignment.get("status") in {"validated", "published"}
            else "Open follow-up"
            if assignment.get("status") == "follow_up"
            else "Open assignment"
        ),
        "url": f"/assignment/{assignment['id']}",
        "external": "false",
        "method": "get",
    }


def _demo_workspace(database: Database) -> str | None:
    row = database.fetch_one("SELECT value_json FROM configuration WHERE key='demo_workspace_path'")
    if row is None:
        return None
    try:
        value = json.loads(str(row["value_json"]))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) and value else None


def _learner_assignment_state(value: str | None, publication_error: object = None) -> str:
    if publication_error:
        return "Setup action required"
    return {
        "validated": "Ready to start",
        "published": "Ready to start",
        "submitted": "Checks running",
        "reviewing": "Review in progress",
        "follow_up": "Follow-up ready",
    }.get(value or "", "No active assignment")


def _render_assignment_instructions(source: str, title: str) -> Markup:
    renderer = MarkdownIt("commonmark", {"html": False})
    tokens = renderer.parse(source)
    if (
        len(tokens) >= 3
        and tokens[0].type == "heading_open"
        and tokens[0].tag == "h1"
        and tokens[1].type == "inline"
        and tokens[1].content.strip().casefold() == title.strip().casefold()
        and tokens[2].type == "heading_close"
    ):
        tokens = tokens[3:]
    # Raw HTML is disabled above; Markup prevents Jinja from escaping renderer output twice.
    return Markup(  # noqa: S704
        renderer.renderer.render(tokens, renderer.options, {})
    )
