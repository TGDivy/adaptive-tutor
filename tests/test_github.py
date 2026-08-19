from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator

import httpx
import pytest

from adaptive_tutor.config import GitHubSettings
from adaptive_tutor.errors import ExternalServiceError, SecurityError
from adaptive_tutor.github import GitHubClient
from adaptive_tutor.security import MAX_ARTIFACT_BYTES


class StaticAuth:
    def token(self) -> str:
        return "test-token"


class ChunkedBytes(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        remaining = MAX_ARTIFACT_BYTES + 1
        chunk = b"x" * (256 * 1024)
        while remaining:
            value = chunk[:remaining]
            remaining -= len(value)
            yield value


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


def test_download_evidence_rejects_oversized_chunked_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {
                            "id": 9,
                            "name": "adaptive-tutor-evidence",
                            "expired": False,
                            "size_in_bytes": 0,
                        }
                    ]
                },
            )
        return httpx.Response(200, stream=ChunkedBytes())

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SecurityError, match="size limit"):
        client.download_evidence(123)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        (None, None),
        ("workflow_id", 10),
        ("path", ".github/workflows/other.yml"),
        ("head_branch", "assignment/other"),
        ("head_sha", "b" * 40),
        ("event", "pull_request"),
        ("repository", {"full_name": "other/repository"}),
        ("head_repository", {"full_name": "other/repository"}),
    ],
)
def test_evaluator_run_requires_complete_trusted_provenance(
    field: str | None, bad_value: object
) -> None:
    workflow_path = ".github/workflows/adaptive-tutor-evaluate.yml"
    run = {
        "workflow_id": 9,
        "path": workflow_path,
        "head_branch": "assignment/0001-example",
        "head_sha": "a" * 40,
        "event": "push",
        "repository": {"full_name": "owner/learning-workspace"},
        "head_repository": {"full_name": "owner/learning-workspace"},
    }
    if field is not None:
        run[field] = bad_value

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "private": True,
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if "/actions/workflows/" in path:
            return httpx.Response(200, json={"id": 9, "path": workflow_path})
        if path.endswith("/actions/runs/77"):
            return httpx.Response(200, json=run)
        if "/contents/.github/workflows/adaptive-tutor-evaluate.yml" in path:
            return httpx.Response(200, json={"sha": "trusted-workflow-sha"})
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    if field is None:
        client.verify_evaluator_run(
            77, branch="assignment/0001-example", commit_sha="a" * 40
        )
    else:
        with pytest.raises(SecurityError, match="provenance"):
            client.verify_evaluator_run(
                77, branch="assignment/0001-example", commit_sha="a" * 40
            )


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


def test_get_file_accepts_github_wrapped_base64() -> None:
    encoded = "aGVsbG8s\nIHdvcmxkCg==\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/learning-workspace/contents/README.md"
        assert request.url.params["ref"] == "a" * 40
        return httpx.Response(
            200,
            json={"type": "file", "encoding": "base64", "content": encoded},
        )

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.get_file("README.md", "a" * 40) == "hello, world\n"
    finally:
        client.close()


def test_get_file_rejects_malformed_base64() -> None:
    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"type": "file", "encoding": "base64", "content": "%%%"},
            )
        ),
    )
    try:
        with pytest.raises(ExternalServiceError, match="invalid Base64 or UTF-8 content"):
            client.get_file("README.md", "a" * 40)
    finally:
        client.close()
