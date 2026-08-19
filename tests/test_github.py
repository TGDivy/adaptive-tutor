from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from adaptive_tutor.config import GitHubSettings
from adaptive_tutor.errors import SecurityError
from adaptive_tutor.github import GitHubClient


class StaticAuth:
    def token(self) -> str:
        return "test-token"


def zipped(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return stream.getvalue()


def test_download_evidence_accepts_one_safe_contract() -> None:
    evidence = json.dumps({"schema_version": "1.0"}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {"id": 9, "name": "adaptive-tutor-evidence", "expired": False}
                    ]
                },
            )
        if request.url.path.endswith("/artifacts/9/zip"):
            return httpx.Response(
                200, content=zipped({"adaptive-tutor-evidence.json": evidence})
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    assert client.download_evidence(123) == evidence


def test_download_evidence_rejects_zip_traversal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {"id": 9, "name": "adaptive-tutor-evidence", "expired": False}
                    ]
                },
            )
        return httpx.Response(
            200,
            content=zipped(
                {
                    "../escape": b"bad",
                    "adaptive-tutor-evidence.json": b"{}",
                }
            ),
        )

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SecurityError, match="unsafe path"):
        client.download_evidence(123)


def test_repository_scope_must_be_private_and_writable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"private": False, "permissions": {"push": True}})

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SecurityError, match="must be private"):
        client.verify_private_repository()


def test_webhook_status_reports_matching_hook_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/learning-workspace/hooks"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 17,
                    "active": True,
                    "events": ["push", "pull_request"],
                    "config": {"url": "https://tutor.example.test/webhooks/github"},
                    "last_response": {"code": 200, "status": "active"},
                }
            ],
        )

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        status = client.webhook_status("https://tutor.example.test/webhooks/github")
        assert status == {
            "id": 17,
            "active": True,
            "events": ["push", "pull_request"],
            "last_response": {"code": 200, "status": "active"},
        }
        assert client.webhook_status("https://other.example.test/webhooks/github") is None
    finally:
        client.close()
