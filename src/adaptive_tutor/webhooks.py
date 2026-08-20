"""Fast authenticated webhook ingress that only persists and enqueues."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import TutorSettings
from .errors import SecurityError
from .jobs import EventStore
from .security import MAX_WEBHOOK_BYTES, verify_webhook_signature


def webhook_router(settings: TutorSettings, events: EventStore) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> JSONResponse:
        secret = settings.webhook_secret
        if not secret:
            raise HTTPException(status_code=503, detail="Webhook secret is not configured")
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
            if declared_size > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload is too large")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload is too large")
        payload_bytes = bytes(body)
        try:
            verify_webhook_signature(
                payload_bytes, request.headers.get("X-Hub-Signature-256"), secret
            )
        except SecurityError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        event_type = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        if not event_type or len(event_type) > 100:
            raise HTTPException(status_code=400, detail="Missing GitHub event type")
        try:
            payload: Any = json.loads(payload_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Webhook body must be an object")
        if event_type != "ping":
            expected = f"{settings.github.owner}/{settings.github.workspace_repo}".lower()
            actual = str((payload.get("repository") or {}).get("full_name", "")).lower()
            if not settings.github.owner:
                raise HTTPException(status_code=503, detail="GitHub owner is not configured")
            installation_repositories = {
                str(item.get("full_name", "")).lower()
                for item in payload.get("repositories", [])
                if isinstance(item, dict)
            }
            if actual != expected and not (
                event_type == "installation" and expected in installation_repositories
            ):
                raise HTTPException(status_code=403, detail="Event repository is outside scope")
        try:
            event_id, job_id, duplicate = events.ingest(
                event_type=event_type,
                delivery_id=delivery_id,
                payload=payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "accepted": True,
                "event_id": event_id,
                "job_id": job_id,
                "duplicate": duplicate,
            },
        )

    return router
