"""Unix-socket service that keeps model credentials away from tutor state."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .codex import (
    MAX_GRADER_PROMPT_BYTES,
    CodexProcess,
    GraderFailure,
    GraderRequest,
    GraderResponse,
    GraderUsage,
)
from .config import CodexSettings
from .errors import ModelError, ModelSchemaError
from .security import redact

MAX_GRADER_REQUEST_BYTES = MAX_GRADER_PROMPT_BYTES + 64 * 1024


def create_grader_app(settings: CodexSettings) -> FastAPI:
    """Create a local-only grader API; deployment exposes it only on a Unix socket."""
    process = CodexProcess(settings)
    app = FastAPI(
        title="Adaptive Tutor isolated grader",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/grade")
    async def grade(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_GRADER_REQUEST_BYTES:
                return _failure(
                    413,
                    "schema_failure",
                    "Grader request exceeds the size limit",
                    retryable=False,
                )
        try:
            payload = GraderRequest.model_validate_json(bytes(body))
        except (ValidationError, ValueError) as exc:
            return _failure(
                422,
                "schema_failure",
                f"Grader request is invalid: {exc}",
                retryable=False,
            )
        if len(payload.prompt.encode("utf-8")) > MAX_GRADER_PROMPT_BYTES:
            return _failure(
                413,
                "schema_failure",
                "Grader prompt exceeds the size limit",
                retryable=False,
            )
        try:
            evaluation, usage = process.invoke(payload.prompt)
        except ModelSchemaError as exc:
            return _failure(422, "schema_failure", str(exc), retryable=exc.retryable)
        except ModelError as exc:
            return _failure(502, "model_failure", str(exc), retryable=exc.retryable)
        except Exception as exc:  # isolated service boundary
            return _failure(
                500,
                "model_failure",
                f"Grader process failed: {exc}",
                retryable=True,
            )
        response = GraderResponse(
            evaluation=evaluation,
            usage=GraderUsage.model_validate(usage),
        )
        return JSONResponse(content=response.model_dump(mode="json"))

    return app


def _failure(
    status_code: int,
    kind: str,
    detail: str,
    *,
    retryable: bool,
) -> JSONResponse:
    payload: dict[str, Any] = GraderFailure(
        kind=kind,
        detail=redact(detail)[:4000],
        retryable=retryable,
    ).model_dump()
    return JSONResponse(status_code=status_code, content=payload)
