"""Least-privilege GitHub App client and assignment/Actions operations."""

from __future__ import annotations

import base64
import binascii
import io
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import httpx
import jwt

from .config import GitHubSettings
from .errors import ConfigurationError, ExternalServiceError, SecurityError
from .security import MAX_ARTIFACT_BYTES, redact

_EVALUATOR_WORKFLOW = ".github/workflows/adaptive-tutor-evaluate.yml"
_EVALUATOR_RUN_TITLE = re.compile(
    r"Adaptive Tutor \| (A-(\d{4,12})) \| "
    r"(assignment/(\d{4,12})-[a-z0-9][a-z0-9-]{2,100}) \| ([0-9a-f]{40,64})"
)


@dataclass
class InstallationToken:
    value: str
    expires_at: datetime


class GitHubAuth:
    def __init__(self, settings: GitHubSettings) -> None:
        self.settings = settings
        self._installation_token: InstallationToken | None = None

    def token(self) -> str:
        if self.settings.app_id and self.settings.installation_id:
            return self._app_installation_token()
        from_environment = self._fallback_token()
        if from_environment:
            return from_environment
        raise ConfigurationError(
            "GitHub App credentials are not configured. Set app_id, installation_id, "
            "private_key_path, and the webhook secret."
        )

    def mode(self) -> str:
        return "github_app" if self.settings.app_id and self.settings.installation_id else "token"

    def _fallback_token(self) -> str | None:
        import os

        return os.environ.get(self.settings.token_env)

    def _app_installation_token(self) -> str:
        now = datetime.now(UTC)
        if self._installation_token and self._installation_token.expires_at > now + timedelta(
            minutes=2
        ):
            return self._installation_token.value
        if not self.settings.private_key_path or not self.settings.private_key_path.is_file():
            raise ConfigurationError("GitHub App private key file is missing")
        key = self.settings.private_key_path.read_text(encoding="utf-8")
        issued = int(time.time())
        app_jwt = jwt.encode(
            {"iat": issued - 60, "exp": issued + 540, "iss": str(self.settings.app_id)},
            key,
            algorithm="RS256",
        )
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f"{self.settings.api_url}/app/installations/{self.settings.installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if response.status_code != 201:
            raise ExternalServiceError(
                f"GitHub App token exchange failed ({response.status_code}): "
                f"{redact(response.text[:1000])}",
                retryable=response.status_code >= 500,
            )
        payload = response.json()
        token = InstallationToken(
            value=str(payload["token"]),
            expires_at=datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")),
        )
        self._installation_token = token
        return token.value


class GitHubClient:
    def __init__(
        self,
        settings: GitHubSettings,
        *,
        auth: GitHubAuth | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or GitHubAuth(settings)
        self.client = httpx.Client(
            base_url=settings.api_url,
            timeout=30,
            follow_redirects=True,
            transport=transport,
        )

    @property
    def repository_path(self) -> str:
        if not self.settings.owner or not self.settings.workspace_repo:
            raise ConfigurationError("GitHub owner and workspace repository are required")
        return f"/repos/{self.settings.owner}/{self.settings.workspace_repo}"

    def close(self) -> None:
        self.client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.auth.token()}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "adaptive-tutor",
            }
        )
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"GitHub request failed: {exc}", retryable=True) from exc
        if response.status_code not in expected:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise ExternalServiceError(
                f"GitHub {method} {path} returned {response.status_code}: "
                f"{redact(response.text[:1500])}",
                retryable=retryable,
            )
        return response

    def repository(self) -> dict[str, Any]:
        return dict(self._request("GET", self.repository_path).json())

    def verify_private_repository(self) -> dict[str, Any]:
        payload = self.repository()
        if not payload.get("private"):
            raise SecurityError("Learning workspace must be private")
        permissions = payload.get("permissions") or {}
        if not permissions.get("push"):
            raise SecurityError("Integration lacks required repository content permission")
        return payload

    def create_or_verify_webhook(self, callback_url: str, secret: str) -> int:
        hooks = self._request("GET", f"{self.repository_path}/hooks").json()
        for hook in hooks:
            if hook.get("config", {}).get("url") == callback_url:
                self._request(
                    "PATCH",
                    f"{self.repository_path}/hooks/{hook['id']}",
                    expected=(200,),
                    json={
                        "active": True,
                        "events": [
                            "push",
                            "pull_request",
                            "workflow_run",
                            "check_suite",
                            "issue_comment",
                        ],
                        "config": {
                            "url": callback_url,
                            "content_type": "json",
                            "insecure_ssl": "0",
                            "secret": secret,
                        },
                    },
                )
                return int(hook["id"])
        response = self._request(
            "POST",
            f"{self.repository_path}/hooks",
            expected=(201,),
            json={
                "name": "web",
                "active": True,
                "events": [
                    "push",
                    "pull_request",
                    "workflow_run",
                    "check_suite",
                    "issue_comment",
                ],
                "config": {
                    "url": callback_url,
                    "content_type": "json",
                    "insecure_ssl": "0",
                    "secret": secret,
                },
            },
        )
        return int(response.json()["id"])

    def webhook_status(self, callback_url: str) -> dict[str, Any] | None:
        hooks = self._request("GET", f"{self.repository_path}/hooks").json()
        for hook in hooks:
            if hook.get("config", {}).get("url") == callback_url:
                return {
                    "id": int(hook["id"]),
                    "active": bool(hook.get("active")),
                    "events": list(hook.get("events") or []),
                    "last_response": dict(hook.get("last_response") or {}),
                }
        return None

    def publish_assignment(
        self,
        *,
        branch: str,
        title: str,
        body: str,
        files: dict[str, str],
    ) -> dict[str, Any]:
        repository = self.verify_private_repository()
        default_branch = str(repository["default_branch"])
        existing_ref = self._request(
            "GET",
            f"{self.repository_path}/git/ref/heads/{branch}",
            expected=(200, 404),
        )
        if existing_ref.status_code == 200:
            head_sha = str(existing_ref.json()["object"]["sha"])
            expected_manifest = files.get(".adaptive-tutor/assignment.json")
            if expected_manifest is None:
                raise SecurityError("Assignment publication is missing its trusted binding")
            try:
                observed_manifest = self.get_file(
                    ".adaptive-tutor/assignment.json",
                    head_sha,
                )
            except ExternalServiceError as exc:
                raise SecurityError(
                    "Existing assignment branch has no verifiable tutor binding"
                ) from exc
            if observed_manifest != expected_manifest:
                raise SecurityError("Existing assignment branch conflicts with tutor state")
            pulls = self._request(
                "GET",
                f"{self.repository_path}/pulls",
                params={"head": f"{self.settings.owner}:{branch}", "state": "all"},
            ).json()
            if pulls:
                return {
                    "pull_number": int(pulls[0]["number"]),
                    "url": str(pulls[0]["html_url"]),
                    "head_sha": head_sha,
                    "branch": branch,
                }
            pull = self._request(
                "POST",
                f"{self.repository_path}/pulls",
                expected=(201,),
                json={"title": title, "body": body, "head": branch, "base": default_branch},
            ).json()
            return {
                "pull_number": int(pull["number"]),
                "url": str(pull["html_url"]),
                "head_sha": head_sha,
                "branch": branch,
            }
        base_ref = self._request(
            "GET", f"{self.repository_path}/git/ref/heads/{default_branch}"
        ).json()
        base_sha = str(base_ref["object"]["sha"])
        base_commit = self._request(
            "GET", f"{self.repository_path}/git/commits/{base_sha}"
        ).json()
        entries = []
        for path, content in sorted(files.items()):
            _validate_repository_path(path)
            blob = self._request(
                "POST",
                f"{self.repository_path}/git/blobs",
                expected=(201,),
                json={"content": content, "encoding": "utf-8"},
            ).json()
            entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )
        tree = self._request(
            "POST",
            f"{self.repository_path}/git/trees",
            expected=(201,),
            json={"base_tree": base_commit["tree"]["sha"], "tree": entries},
        ).json()
        commit = self._request(
            "POST",
            f"{self.repository_path}/git/commits",
            expected=(201,),
            json={
                "message": f"tutor: create {title}",
                "tree": tree["sha"],
                "parents": [base_sha],
            },
        ).json()
        self._request(
            "POST",
            f"{self.repository_path}/git/refs",
            expected=(201,),
            json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
        )
        pull = self._request(
            "POST",
            f"{self.repository_path}/pulls",
            expected=(201,),
            json={"title": title, "body": body, "head": branch, "base": default_branch},
        ).json()
        return {
            "pull_number": int(pull["number"]),
            "url": str(pull["html_url"]),
            "head_sha": str(commit["sha"]),
            "branch": branch,
        }

    def dispatch_evaluator(
        self,
        *,
        assignment_id: str,
        branch: str,
        commit_sha: str,
        workflow_path: str = _EVALUATOR_WORKFLOW,
    ) -> None:
        _validate_evaluator_identity(assignment_id, branch, commit_sha)
        repository = self.verify_private_repository()
        self._request(
            "POST",
            f"{self.repository_path}/actions/workflows/{workflow_path}/dispatches",
            expected=(204,),
            json={
                "ref": str(repository["default_branch"]),
                "inputs": {
                    "assignment_id": assignment_id,
                    "branch": branch,
                    "commit_sha": commit_sha,
                },
            },
        )

    def get_file(self, path: str, ref: str) -> str:
        _validate_repository_path(path)
        payload = self._request(
            "GET", f"{self.repository_path}/contents/{path}", params={"ref": ref}
        ).json()
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            raise ExternalServiceError(f"Unexpected GitHub content response for {path}")
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise ExternalServiceError(f"GitHub content is missing for {path}")
        try:
            normalized = "".join(encoded.split())
            return base64.b64decode(normalized, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ExternalServiceError(
                f"GitHub returned invalid Base64 or UTF-8 content for {path}"
            ) from exc

    def download_evidence(
        self, run_id: int, *, artifact_name: str = "adaptive-tutor-evidence"
    ) -> bytes:
        artifacts = self._request(
            "GET", f"{self.repository_path}/actions/runs/{run_id}/artifacts"
        ).json()
        matches = [
            item
            for item in artifacts.get("artifacts", [])
            if item.get("name") == artifact_name and not item.get("expired")
        ]
        if len(matches) != 1:
            raise ExternalServiceError(
                f"Expected one non-expired {artifact_name} artifact, found {len(matches)}",
                retryable=True,
            )
        metadata_size = int(matches[0].get("size_in_bytes") or 0)
        if metadata_size > MAX_ARTIFACT_BYTES:
            raise SecurityError("Actions artifact exceeds the size limit")
        archive = self._download_limited(
            f"{self.repository_path}/actions/artifacts/{matches[0]['id']}/zip",
            MAX_ARTIFACT_BYTES,
        )
        return _read_evidence_zip(archive)

    def verify_evaluator_run(
        self,
        run_id: int,
        *,
        workflow_path: str = _EVALUATOR_WORKFLOW,
    ) -> dict[str, str]:
        repository = self.verify_private_repository()
        workflow = self._request(
            "GET", f"{self.repository_path}/actions/workflows/{workflow_path}"
        ).json()
        run = self._request("GET", f"{self.repository_path}/actions/runs/{run_id}").json()
        expected_repository = f"{self.settings.owner}/{self.settings.workspace_repo}".lower()
        observed_repository = str((run.get("repository") or {}).get("full_name", "")).lower()
        head_repository = str((run.get("head_repository") or {}).get("full_name", "")).lower()
        default_branch = str(repository["default_branch"])
        if (
            int(run.get("workflow_id") or 0) != int(workflow["id"])
            or str(run.get("path")) != workflow_path
            or str(run.get("head_branch")) != default_branch
            or str(run.get("event")) != "workflow_dispatch"
            or observed_repository != expected_repository
            or head_repository != expected_repository
        ):
            raise SecurityError("Actions run provenance does not match the trusted evaluator")
        title_match = _EVALUATOR_RUN_TITLE.fullmatch(str(run.get("display_title", "")))
        if title_match is None or title_match.group(2) != title_match.group(4):
            raise SecurityError("Actions run has an invalid evaluator identity")
        assignment_id = title_match.group(1)
        branch = title_match.group(3)
        commit_sha = title_match.group(5)
        _validate_evaluator_identity(assignment_id, branch, commit_sha)
        workflow_commit = str(run.get("head_sha", ""))
        if re.fullmatch(r"[0-9a-f]{40,64}", workflow_commit) is None:
            raise SecurityError("Actions run has an invalid trusted workflow commit")
        trusted = self._request(
            "GET",
            f"{self.repository_path}/contents/{workflow_path}",
            params={"ref": default_branch},
        ).json()
        executed = self._request(
            "GET",
            f"{self.repository_path}/contents/{workflow_path}",
            params={"ref": workflow_commit},
        ).json()
        if trusted.get("sha") != executed.get("sha"):
            raise SecurityError("Actions run did not use the trusted default-branch workflow")
        return {
            "assignment_id": assignment_id,
            "branch": branch,
            "commit_sha": commit_sha,
            "workflow_commit": workflow_commit,
        }

    def _download_limited(self, path: str, maximum: int) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.auth.token()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "adaptive-tutor",
        }
        try:
            with self.client.stream("GET", path, headers=headers) as response:
                if response.status_code != 200:
                    raise ExternalServiceError(
                        f"GitHub GET {path} returned {response.status_code}: "
                        f"{redact(response.read().decode(errors='replace')[:1500])}",
                        retryable=response.status_code in {408, 429}
                        or response.status_code >= 500,
                    )
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > maximum:
                    raise SecurityError("Actions artifact exceeds the size limit")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise SecurityError("Actions artifact exceeds the size limit")
                return bytes(content)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"GitHub request failed: {exc}", retryable=True) from exc

    def post_review(self, pull_number: int, body: str, *, commit_sha: str | None = None) -> int:
        payload: dict[str, Any] = {"body": body, "event": "COMMENT"}
        if commit_sha:
            payload["commit_id"] = commit_sha
        response = self._request(
            "POST",
            f"{self.repository_path}/pulls/{pull_number}/reviews",
            expected=(200,),
            json=payload,
        )
        return int(response.json()["id"])

    def ensure_review(
        self,
        pull_number: int,
        body: str,
        *,
        marker: str,
        commit_sha: str | None = None,
    ) -> int:
        reviews = self._request(
            "GET",
            f"{self.repository_path}/pulls/{pull_number}/reviews",
            params={"per_page": 100},
        ).json()
        for review in reviews:
            if marker in str(review.get("body") or ""):
                return int(review["id"])
        return self.post_review(pull_number, body, commit_sha=commit_sha)

    def post_comment(self, issue_number: int, body: str) -> int:
        response = self._request(
            "POST",
            f"{self.repository_path}/issues/{issue_number}/comments",
            expected=(201,),
            json={"body": body},
        )
        return int(response.json()["id"])

    def ensure_comment(self, issue_number: int, body: str, *, marker: str) -> int:
        comments = self._request(
            "GET",
            f"{self.repository_path}/issues/{issue_number}/comments",
            params={"per_page": 100},
        ).json()
        for comment in comments:
            if marker in str(comment.get("body") or ""):
                return int(comment["id"])
        return self.post_comment(issue_number, body)


def _validate_evaluator_identity(assignment_id: str, branch: str, commit_sha: str) -> None:
    match = _EVALUATOR_RUN_TITLE.fullmatch(
        f"Adaptive Tutor | {assignment_id} | {branch} | {commit_sha}"
    )
    if match is None or match.group(2) != match.group(4):
        raise SecurityError("Invalid assignment identity for evaluator dispatch")


def _validate_repository_path(path: str) -> None:
    candidate = PurePosixPath(path)
    unsafe_part = any(part in {"", ".", ".."} for part in candidate.parts)
    if candidate.is_absolute() or not path or unsafe_part:
        raise SecurityError(f"Unsafe repository path: {path}")


def _read_evidence_zip(archive: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = zipped.infolist()
            if len(members) > 20 or sum(item.file_size for item in members) > MAX_ARTIFACT_BYTES:
                raise SecurityError("Actions artifact expands beyond the size limit")
            names = [PurePosixPath(item.filename) for item in members]
            if any(path.is_absolute() or ".." in path.parts for path in names):
                raise SecurityError("Actions artifact contains an unsafe path")
            matches = [
                member for member in members if member.filename == "adaptive-tutor-evidence.json"
            ]
            if len(matches) != 1:
                raise SecurityError("Actions artifact must contain one evidence contract")
            return zipped.read(matches[0])
    except zipfile.BadZipFile as exc:
        raise SecurityError("Actions evidence artifact is not a valid zip file") from exc
