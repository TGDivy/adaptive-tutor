from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from adaptive_tutor.config import GitHubSettings
from adaptive_tutor.errors import ConfigurationError, ExternalServiceError, SecurityError
from adaptive_tutor.github import GitHubAuth, GitHubClient, InstallationToken
from adaptive_tutor.security import MAX_ARTIFACT_BYTES, sha256_digest


class StaticAuth:
    def token(self) -> str:
        return "test-token"


class ScopedAppAuth(StaticAuth):
    def mode(self) -> str:
        return "github_app"

    def installation_scope(self) -> tuple[dict[str, str], str]:
        return (
            {
                "actions": "write",
                "checks": "read",
                "contents": "write",
                "issues": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            "selected",
        )


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
                    "artifacts": [{"id": 9, "name": "adaptive-tutor-evidence", "expired": False}]
                },
            )
        if request.url.path.endswith("/artifacts/9/zip"):
            return httpx.Response(200, content=zipped({"adaptive-tutor-evidence.json": evidence}))
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
                    "artifacts": [{"id": 9, "name": "adaptive-tutor-evidence", "expired": False}]
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
        ("head_sha", "not-a-commit"),
        ("event", "push"),
        ("display_title", "Adaptive Tutor | invalid"),
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
        "head_branch": "main",
        "head_sha": "f" * 40,
        "event": "workflow_dispatch",
        "display_title": (
            "Adaptive Tutor | A-0001 | " + "a" * 40 + " | " + "b" * 32 + " | " + "d" * 40
        ),
        "repository": {"id": 123, "full_name": "owner/learning-workspace"},
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
                    "id": 123,
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if "/actions/workflows/" in path:
            return httpx.Response(200, json={"id": 9, "path": workflow_path})
        if path.endswith("/actions/runs/77"):
            return httpx.Response(200, json=run)
        if "/contents/.github/workflows/adaptive-tutor-evaluate.yml" in path:
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b"trusted workflow\n").decode(),
                },
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    if field is None:
        assert client.verify_evaluator_run(77) == {
            "assignment_id": "A-0001",
            "commit_sha": "a" * 40,
            "dispatch_nonce": "b" * 32,
            "evaluator_ref": "d" * 40,
            "workflow_commit": "f" * 40,
            "workflow_digest": sha256_digest("trusted workflow\n"),
            "repository_id": 123,
        }
    else:
        with pytest.raises(SecurityError, match="Actions run"):
            client.verify_evaluator_run(77)


def test_evaluator_dispatch_uses_trusted_default_branch_and_typed_inputs() -> None:
    observed: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "private": True,
                    "id": 123,
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if request.url.path.endswith("/adaptive-tutor-evaluate.yml/dispatches"):
            observed.update(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    client.dispatch_evaluator(
        assignment_id="A-0001",
        branch="assignment/0001-example",
        commit_sha="a" * 40,
        dispatch_nonce="b" * 32,
        manifest_digest="sha256:" + "c" * 64,
        evaluator_ref="d" * 40,
        evaluator_kit_digest="sha256:" + "e" * 64,
    )
    assert observed == {
        "ref": "main",
        "inputs": {
            "assignment_id": "A-0001",
            "branch": "assignment/0001-example",
            "commit_sha": "a" * 40,
            "dispatch_nonce": "b" * 32,
            "manifest_digest": "sha256:" + "c" * 64,
            "evaluator_ref": "d" * 40,
            "evaluator_kit_digest": "sha256:" + "e" * 64,
        },
    }


def test_setup_probe_dispatch_and_run_require_hosted_workflow_provenance() -> None:
    nonce = "b" * 32
    workflow_path = ".github/workflows/adaptive-tutor-setup-probe.yml"
    dispatched: dict[str, Any] = {}
    run = {
        "id": 77,
        "workflow_id": 9,
        "path": workflow_path,
        "head_branch": "main",
        "head_sha": "f" * 40,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "display_title": f"Adaptive Tutor Setup | {nonce}",
        "repository": {"id": 123, "full_name": "owner/learning-workspace"},
        "head_repository": {"full_name": "owner/learning-workspace"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "private": True,
                    "id": 123,
                    "full_name": "owner/learning-workspace",
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if path.endswith("/adaptive-tutor-setup-probe.yml/dispatches"):
            dispatched.update(json.loads(request.content))
            return httpx.Response(204)
        if path.endswith("/adaptive-tutor-setup-probe.yml/runs"):
            return httpx.Response(200, json={"workflow_runs": [run]})
        if "/actions/workflows/" in path and path.endswith("/adaptive-tutor-setup-probe.yml"):
            return httpx.Response(200, json={"id": 9, "path": workflow_path})
        if path.endswith("/actions/runs/77"):
            return httpx.Response(200, json=run)
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        client.dispatch_setup_probe(nonce=nonce, evaluator_key_id="a" * 16)
        assert dispatched == {
            "ref": "main",
            "inputs": {"nonce": nonce, "evaluator_key_id": "a" * 16},
        }
        expected = {
            "run_id": 77,
            "status": "completed",
            "conclusion": "success",
            "workflow_commit": "f" * 40,
            "repository_id": 123,
        }
        assert client.find_setup_probe_run(nonce) == expected
        assert client.get_setup_probe_run(77, nonce=nonce) == expected
    finally:
        client.close()


def test_setup_probe_artifact_requires_one_safe_named_contract() -> None:
    evidence = b'{"schema_version":"1.0"}'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {"id": 19, "name": "adaptive-tutor-setup-probe", "expired": False}
                    ]
                },
            )
        if request.url.path.endswith("/artifacts/19/zip"):
            return httpx.Response(
                200,
                content=zipped({"adaptive-tutor-setup-probe.json": evidence}),
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.download_setup_probe_evidence(77) == evidence
    finally:
        client.close()


def test_evaluator_control_is_bound_to_repository_workflow_and_key() -> None:
    workflow = "name: protected evaluator\n"
    public_key = "ed25519:" + "ab" * 32 + "\n"
    key_id = hashlib.sha256(bytes.fromhex("ab" * 32)).hexdigest()[:16]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "full_name": "owner/learning-workspace",
                    "private": True,
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "f" * 40}})
        if path.endswith("/contents/.github/workflows/adaptive-tutor-evaluate.yml"):
            content = workflow
        elif path.endswith("/contents/.adaptive-tutor/evaluator-signing.pub"):
            content = public_key
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode(),
            },
        )

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    assert client.verify_evaluator_control(
        expected_repository_id=123,
        expected_workflow_digest=sha256_digest(workflow),
        expected_key_id=key_id,
    ) == {
        "repository_id": 123,
        "repository_full_name": "owner/learning-workspace",
        "default_branch": "main",
        "workflow_commit": "f" * 40,
        "workflow_digest": sha256_digest(workflow),
        "evaluator_key_id": key_id,
    }
    with pytest.raises(SecurityError, match="workflow differs"):
        client.verify_evaluator_control(
            expected_repository_id=123,
            expected_workflow_digest="sha256:" + "0" * 64,
            expected_key_id=key_id,
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


def test_github_app_scope_is_limited_to_one_workspace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "full_name": "owner/learning-workspace",
                    "private": True,
                    "permissions": {"push": True},
                },
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [{"id": 123, "full_name": "owner/learning-workspace"}],
                },
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=ScopedAppAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        scope = client.verify_app_installation_scope()
        assert scope["repository_id"] == 123
        assert scope["repository_selection"] == "selected"
    finally:
        client.close()


def test_first_assignment_pull_request_requires_exact_open_head() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/learning-workspace/pulls/42":
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "state": "open",
                    "head": {
                        "ref": "assignment/0001-example",
                        "sha": "a" * 40,
                        "repo": {"full_name": "owner/learning-workspace"},
                    },
                    "base": {"repo": {"full_name": "owner/learning-workspace"}},
                },
            )
        if request.url.path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "private": True,
                    "full_name": "owner/learning-workspace",
                    "permissions": {"push": True},
                },
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        pull = client.verify_assignment_pull_request(
            42, branch="assignment/0001-example", head_sha="a" * 40
        )
        assert pull["number"] == 42
        with pytest.raises(SecurityError, match="provenance"):
            client.verify_assignment_pull_request(
                42, branch="assignment/0001-example", head_sha="b" * 40
            )
    finally:
        client.close()


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


def test_github_auth_uses_fallback_and_cached_installation_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADAPTIVE_TUTOR_GITHUB_TOKEN", "development-token")
    fallback = GitHubAuth(GitHubSettings())
    assert fallback.mode() == "token"
    assert fallback.token() == "development-token"

    monkeypatch.delenv("ADAPTIVE_TUTOR_GITHUB_TOKEN")
    with pytest.raises(ConfigurationError, match="credentials are not configured"):
        fallback.token()

    app_auth = GitHubAuth(GitHubSettings(app_id=11, installation_id=22))
    assert app_auth.mode() == "github_app"
    with pytest.raises(ConfigurationError, match="private key file is missing"):
        app_auth.token()

    app_auth._installation_token = InstallationToken(
        value="cached-installation-token",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert app_auth.token() == "cached-installation-token"


@pytest.mark.parametrize("status_code", [201, 503])
def test_github_app_token_exchange(
    status_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "github-app.pem"
    key_path.write_text("test-private-key", encoding="utf-8")
    observed: dict[str, Any] = {}

    class TokenClient:
        def __init__(self, **options: Any) -> None:
            observed["options"] = options

        def __enter__(self) -> TokenClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, **options: Any) -> httpx.Response:
            observed["url"] = url
            observed["request"] = options
            if status_code == 201:
                return httpx.Response(
                    201,
                    json={
                        "token": "installation-token",
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                )
            return httpx.Response(503, text="temporary outage secret-value")

    monkeypatch.setattr("adaptive_tutor.github.jwt.encode", lambda *_args, **_kwargs: "jwt")
    monkeypatch.setattr("adaptive_tutor.github.httpx.Client", TokenClient)
    auth = GitHubAuth(
        GitHubSettings(
            app_id=11,
            installation_id=22,
            private_key_path=key_path,
        )
    )

    if status_code == 201:
        assert auth.token() == "installation-token"
        assert auth.token() == "installation-token"
        assert observed["url"] == ("https://api.github.com/app/installations/22/access_tokens")
        request = observed["request"]
        assert request["headers"]["Authorization"] == "Bearer jwt"
    else:
        with pytest.raises(ExternalServiceError, match="token exchange failed") as raised:
            auth.token()
        assert raised.value.retryable is True


def test_publish_assignment_creates_git_objects_and_pull_request() -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []
    blob_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal blob_number
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, path, payload))
        if path == "/repos/owner/learning-workspace":
            return httpx.Response(
                200,
                json={
                    "private": True,
                    "default_branch": "main",
                    "permissions": {"push": True},
                },
            )
        if path.endswith("/git/ref/heads/tutor/a-0042"):
            return httpx.Response(404)
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if path.endswith("/git/commits/base-sha"):
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path.endswith("/git/blobs"):
            blob_number += 1
            return httpx.Response(201, json={"sha": f"blob-{blob_number}"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "new-tree"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "new-commit"})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={"number": 42, "html_url": "https://github.example/pull/42"},
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.publish_assignment(
            branch="tutor/a-0042",
            title="A-0042: Trace a bounded queue",
            body="Assignment instructions",
            files={"src/main.py": "pass\n", "README.md": "# Assignment\n"},
        )
    finally:
        client.close()

    assert result == {
        "pull_number": 42,
        "url": "https://github.example/pull/42",
        "head_sha": "new-commit",
        "branch": "tutor/a-0042",
    }
    tree_request = next(item for item in requests if item[1].endswith("/git/trees"))
    assert tree_request[2] == {
        "base_tree": "base-tree",
        "tree": [
            {"path": "README.md", "mode": "100644", "type": "blob", "sha": "blob-1"},
            {
                "path": "src/main.py",
                "mode": "100644",
                "type": "blob",
                "sha": "blob-2",
            },
        ],
    }


@pytest.mark.parametrize("existing_pull", [True, False])
def test_publish_assignment_resumes_an_existing_branch(existing_pull: bool) -> None:
    manifest = '{"id":"A-0042","evaluator_binding":"sha256:test"}\n'

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
        if path.endswith("/git/ref/heads/tutor/a-0042"):
            return httpx.Response(200, json={"object": {"sha": "existing-sha"}})
        if path.endswith("/contents/.adaptive-tutor/assignment.json"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(manifest.encode()).decode(),
                },
            )
        if request.method == "GET" and path.endswith("/pulls"):
            assert request.url.params["head"] == "owner:tutor/a-0042"
            return httpx.Response(
                200,
                json=(
                    [{"number": 41, "html_url": "https://github.example/pull/41"}]
                    if existing_pull
                    else []
                ),
            )
        if request.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={"number": 42, "html_url": "https://github.example/pull/42"},
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.publish_assignment(
            branch="tutor/a-0042",
            title="Assignment",
            body="Body",
            files={".adaptive-tutor/assignment.json": manifest},
        )
    finally:
        client.close()
    assert result["pull_number"] == (41 if existing_pull else 42)
    assert result["head_sha"] == "existing-sha"


def test_publish_assignment_rejects_a_precreated_conflicting_branch() -> None:
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
        if path.endswith("/git/ref/heads/assignment/0042-example"):
            return httpx.Response(200, json={"object": {"sha": "attacker-sha"}})
        if path.endswith("/contents/.adaptive-tutor/assignment.json"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(b'{"id":"attacker"}\n').decode(),
                },
            )
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SecurityError, match="conflicts with tutor state"):
        client.publish_assignment(
            branch="assignment/0042-example",
            title="Assignment",
            body="Body",
            files={".adaptive-tutor/assignment.json": '{"id":"A-0042"}\n'},
        )


def test_webhook_review_and_comment_writes_are_idempotent() -> None:
    hooks: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = [{"id": 8, "body": "review-marker"}]
    comments: list[dict[str, Any]] = [{"id": 9, "body": "comment-marker"}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        if path.endswith("/hooks") and request.method == "GET":
            return httpx.Response(200, json=hooks)
        if path.endswith("/hooks") and request.method == "POST":
            hooks.append({"id": 17, "config": payload["config"]})
            return httpx.Response(201, json={"id": 17})
        if path.endswith("/hooks/17"):
            assert payload["active"] is True
            return httpx.Response(200, json={"id": 17})
        if path.endswith("/pulls/42/reviews") and request.method == "GET":
            return httpx.Response(200, json=reviews)
        if path.endswith("/pulls/42/reviews") and request.method == "POST":
            assert payload["event"] == "COMMENT"
            assert payload["commit_id"] == "a" * 40
            return httpx.Response(200, json={"id": 18})
        if path.endswith("/issues/42/comments") and request.method == "GET":
            return httpx.Response(200, json=comments)
        if path.endswith("/issues/42/comments") and request.method == "POST":
            return httpx.Response(201, json={"id": 19})
        return httpx.Response(404)

    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.create_or_verify_webhook("https://tutor.example/hook", "secret") == 17
        assert client.create_or_verify_webhook("https://tutor.example/hook", "secret") == 17
        assert client.ensure_review(42, "new", marker="review-marker") == 8
        reviews.clear()
        assert (
            client.ensure_review(
                42,
                "new review",
                marker="review-marker",
                commit_sha="a" * 40,
            )
            == 18
        )
        assert client.ensure_comment(42, "new", marker="comment-marker") == 9
        comments.clear()
        assert client.ensure_comment(42, "new comment", marker="comment-marker") == 19
    finally:
        client.close()


def test_github_request_failures_and_unsafe_content_are_classified() -> None:
    unavailable = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(httpx.ReadError("down"))),
    )
    try:
        with pytest.raises(ExternalServiceError, match="request failed") as raised:
            unavailable.repository()
        assert raised.value.retryable is True
    finally:
        unavailable.close()

    throttled = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, text="rate limit secret-value")
        ),
    )
    try:
        with pytest.raises(ExternalServiceError, match="returned 429") as raised:
            throttled.repository()
        assert raised.value.retryable is True
        with pytest.raises(SecurityError, match="Unsafe repository path"):
            throttled.get_file("../secret", "main")
    finally:
        throttled.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "dir", "encoding": "base64", "content": "eA=="},
        {"type": "file", "encoding": "base64"},
    ],
)
def test_get_file_rejects_unexpected_content_contract(payload: dict[str, Any]) -> None:
    client = GitHubClient(
        GitHubSettings(owner="owner"),
        auth=StaticAuth(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(ExternalServiceError, match=r"Unexpected|missing"):
            client.get_file("README.md", "main")
    finally:
        client.close()
