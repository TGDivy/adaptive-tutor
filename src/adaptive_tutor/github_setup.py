"""Authenticated GitHub repository and App bootstrap for guided setup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .config import (
    GitHubSettings,
    TutorSettings,
    update_setup_config,
    upsert_secret,
)
from .db import Database
from .errors import ConfigurationError, ExternalServiceError, SecurityError
from .github import GitHubClient
from .security import redact
from .time import iso_now, utc_now

if TYPE_CHECKING:
    from .setup import SetupRun

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_APP_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_MANIFEST_CODE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


@dataclass(frozen=True)
class BootstrapRepository:
    owner: str
    owner_type: str
    name: str
    repository_id: int
    full_name: str
    html_url: str


@dataclass(frozen=True)
class GitHubAppManifestLaunch:
    action_url: str
    manifest_json: str


class GitHubCLIBootstrap:
    """Use the operator's authenticated GitHub CLI only during trusted setup."""

    def __init__(self, executable: str | None = None) -> None:
        resolved = executable or shutil.which("gh")
        if not resolved:
            raise ConfigurationError("GitHub CLI is required for guided setup")
        self.executable = str(Path(resolved).resolve())

    def ensure_private_repository(self, owner: str, repository: str) -> BootstrapRepository:
        self._run(["auth", "status", "--hostname", "github.com"])
        normalized_owner = owner.strip()
        if not normalized_owner:
            normalized_owner = self._run(["api", "user", "--jq", ".login"]).stdout.strip()
        _validate_owner(normalized_owner)
        _validate_repository(repository)
        owner_type = self._run(["api", f"users/{normalized_owner}", "--jq", ".type"]).stdout.strip()
        if owner_type not in {"User", "Organization"}:
            raise ConfigurationError("GitHub repository owner must be a user or organization")
        full_name = f"{normalized_owner}/{repository}"
        viewed = self._run(["api", f"repos/{full_name}"], allow_failure=True)
        if viewed.returncode != 0:
            self._run(
                [
                    "repo",
                    "create",
                    full_name,
                    "--private",
                    "--add-readme",
                    "--disable-wiki",
                    "--description",
                    "Private Adaptive Tutor learning workspace",
                ],
                timeout=60,
            )
            viewed = self._run(["api", f"repos/{full_name}"])
        try:
            payload = json.loads(viewed.stdout)
            repository_id = int(payload["id"])
            observed_name = str(payload["full_name"])
            is_private = payload["private"] is True
            html_url = str(payload["html_url"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExternalServiceError("GitHub returned invalid repository metadata") from exc
        if observed_name.casefold() != full_name.casefold():
            raise SecurityError("GitHub repository identity does not match setup configuration")
        if not is_private:
            raise SecurityError("Learning workspace must be private")
        return BootstrapRepository(
            owner=normalized_owner,
            owner_type=owner_type,
            name=repository,
            repository_id=repository_id,
            full_name=observed_name,
            html_url=html_url,
        )

    def _run(
        self,
        arguments: list[str],
        *,
        allow_failure: bool = False,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["GH_HOST"] = "github.com"
        result = subprocess.run(  # noqa: S603 - fixed executable and bounded argument list
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        if result.returncode and not allow_failure:
            detail = redact((result.stderr or result.stdout).strip())[-1200:]
            raise ExternalServiceError(
                f"GitHub CLI {' '.join(arguments[:2])} failed: {detail or 'unknown error'}"
            )
        return result


class GitHubAppSetupService:
    """Create and consume short-lived GitHub App manifest/install browser state."""

    def __init__(
        self,
        settings: TutorSettings,
        database: Database,
        config_path: Path,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.config_path = config_path.expanduser().resolve()
        self.transport = transport
        self.client: httpx.Client | None = None

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def start(self, run: SetupRun) -> GitHubAppManifestLaunch:
        step = _run_step(run, "github_app")
        if step.status not in {"waiting_user", "failed_retryable"}:
            raise ConfigurationError("GitHub App setup is not the current guided-setup step")
        owner = self.settings.github.owner
        _validate_owner(owner)
        repository_step = _run_step(run, "github_repository")
        owner_type = str(repository_step.external_ids.get("owner_type", ""))
        if owner_type not in {"User", "Organization"}:
            raise ConfigurationError("GitHub repository owner type was not verified")
        state = secrets.token_urlsafe(32)
        now = iso_now()
        expires_at = (utc_now() + timedelta(minutes=15)).isoformat(timespec="seconds")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE github_app_setup_sessions
                SET status='cancelled', completed_at=?, updated_at=?
                WHERE status='active'
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO github_app_setup_sessions(
                    id, run_id, phase, status, state_digest,
                    created_at, expires_at, updated_at
                ) VALUES (?, ?, 'manifest', 'active', ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), run.id, _state_digest(state), now, expires_at, now),
            )
        manifest = {
            "name": f"Adaptive Tutor {run.id[:8]}",
            "url": run.public_url,
            "redirect_url": run.public_url + "/setup/github-app/callback",
            "setup_url": run.public_url + "/setup/github-app/installed",
            "hook_attributes": {
                "url": run.public_url + "/webhooks/github",
                "active": True,
            },
            "public": False,
            "request_oauth_on_install": False,
            "default_permissions": {
                "actions": "write",
                "checks": "read",
                "contents": "write",
                "issues": "write",
                "metadata": "read",
                "pull_requests": "write",
            },
            "default_events": [
                "check_suite",
                "issue_comment",
                "pull_request",
                "push",
                "workflow_run",
            ],
        }
        if owner_type == "Organization":
            target = f"https://github.com/organizations/{quote(owner)}/settings/apps/new"
        else:
            target = "https://github.com/settings/apps/new"
        return GitHubAppManifestLaunch(
            action_url=target + "?" + urlencode({"state": state}),
            manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        )

    def complete_manifest(self, run: SetupRun, *, code: str, state: str) -> str:
        if _MANIFEST_CODE.fullmatch(code) is None:
            raise ConfigurationError("GitHub App manifest code is invalid")
        session = self._active_session(run.id, state, phase="manifest")
        response = self._http_client().post(f"/app-manifests/{code}/conversions")
        if response.status_code != 201:
            raise ExternalServiceError(
                f"GitHub App manifest conversion failed ({response.status_code}): "
                f"{redact(response.text[:1000])}"
            )
        try:
            payload = response.json()
            app_id = int(payload["id"])
            app_slug = str(payload["slug"])
            pem = str(payload["pem"])
            webhook_secret = str(payload["webhook_secret"])
            app_owner = str(payload["owner"]["login"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("GitHub App conversion response is incomplete") from exc
        if app_id <= 0 or _APP_SLUG.fullmatch(app_slug) is None:
            raise SecurityError("GitHub App conversion returned an invalid identity")
        if app_owner.casefold() != self.settings.github.owner.casefold():
            raise SecurityError("GitHub App was created under the wrong owner")
        if len(webhook_secret) < 20 or len(webhook_secret) > 500:
            raise SecurityError("GitHub App webhook secret has an invalid size")
        try:
            private_key = serialization.load_pem_private_key(pem.encode(), password=None)
        except (TypeError, ValueError) as exc:
            raise SecurityError("GitHub App private key is invalid") from exc
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise SecurityError("GitHub App private key must be RSA")
        key_path = self.settings.data_dir / "github-app.pem"
        _write_owner_only(key_path, pem)
        if self.settings.secrets_file is None:  # pragma: no cover - normalized by settings
            raise ConfigurationError("Adaptive Tutor secrets file is not configured")
        upsert_secret(
            self.settings.secrets_file,
            self.settings.github.webhook_secret_env,
            webhook_secret,
        )
        updated = update_setup_config(
            self.config_path,
            public_url=run.public_url,
            app_id=app_id,
            private_key_path=key_path,
        )
        self.settings.github = updated.github
        installation_state = secrets.token_urlsafe(32)
        now = iso_now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE github_app_setup_sessions
                SET phase='installation', state_digest=?, app_id=?, app_slug=?, updated_at=?
                WHERE id=? AND status='active' AND phase='manifest'
                """,
                (
                    _state_digest(installation_state),
                    app_id,
                    app_slug,
                    now,
                    str(session["id"]),
                ),
            ).rowcount
            if changed != 1:  # pragma: no cover - state was selected immediately above
                raise RuntimeError("GitHub App setup state changed concurrently")
        return f"https://github.com/apps/{quote(app_slug)}/installations/new?" + urlencode(
            {"state": installation_state}
        )

    def complete_installation(
        self,
        run: SetupRun,
        *,
        installation_id: int,
        state: str,
    ) -> dict[str, Any]:
        if installation_id <= 0:
            raise ConfigurationError("GitHub App installation identifier is invalid")
        session = self._active_session(run.id, state, phase="installation")
        app_id = int(session["app_id"] or 0)
        if app_id <= 0:
            raise SecurityError("GitHub App setup has no converted App identity")
        candidate_github = self.settings.github.model_copy(
            update={"app_id": app_id, "installation_id": installation_id}
        )
        client = GitHubClient(candidate_github)
        try:
            repository = client.verify_private_repository()
        finally:
            client.close()
        expected_repository_id = int(
            _run_step(run, "github_repository").external_ids.get("repository_id", 0)
        )
        if int(repository.get("id") or 0) != expected_repository_id:
            raise SecurityError("GitHub App installation does not include the setup workspace")
        updated = update_setup_config(
            self.config_path,
            public_url=run.public_url,
            app_id=app_id,
            installation_id=installation_id,
            private_key_path=self.settings.data_dir / "github-app.pem",
        )
        self.settings.github = updated.github
        now = iso_now()
        changed = self.database.execute(
            """
            UPDATE github_app_setup_sessions
            SET status='complete', completed_at=?, updated_at=?
            WHERE id=? AND status='active' AND phase='installation'
            """,
            (now, now, str(session["id"])),
        )
        if changed != 1:  # pragma: no cover - state was selected immediately above
            raise RuntimeError("GitHub App installation state changed concurrently")
        return repository

    def _active_session(self, run_id: str, state: str, *, phase: str) -> dict[str, Any]:
        if len(state) < 32 or len(state) > 200:
            raise ConfigurationError("GitHub setup state is invalid or expired")
        row = self.database.fetch_one(
            """
            SELECT * FROM github_app_setup_sessions
            WHERE run_id=? AND state_digest=? AND phase=? AND status='active'
              AND expires_at >= ?
            """,
            (run_id, _state_digest(state), phase, iso_now()),
        )
        if row is None:
            raise ConfigurationError("GitHub setup state is invalid or expired")
        return row

    def _http_client(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(
                base_url="https://api.github.com",
                timeout=30,
                follow_redirects=False,
                transport=self.transport,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "adaptive-tutor-setup",
                },
            )
        return self.client


def _run_step(run: SetupRun, name: str) -> Any:
    step = next((item for item in run.steps if item.name == name), None)
    if step is None:
        raise ConfigurationError(f"Guided setup has no {name} step")
    return step


def _state_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_owner(value: str) -> None:
    if _OWNER.fullmatch(value) is None:
        raise ConfigurationError("GitHub owner is invalid")


def _validate_repository(value: str) -> None:
    if _REPOSITORY.fullmatch(value) is None or value in {".", ".."}:
        raise ConfigurationError("GitHub workspace repository name is invalid")


def _write_owner_only(path: Path, value: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    if target.is_symlink():
        raise ConfigurationError("GitHub App private key cannot be a symlink")
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=target.name + ".",
        delete=False,
    ) as temporary:
        temporary.write(value)
        if not value.endswith("\n"):
            temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(target)
    target.chmod(0o600)


__all__ = [
    "BootstrapRepository",
    "GitHubAppManifestLaunch",
    "GitHubAppSetupService",
    "GitHubCLIBootstrap",
    "GitHubSettings",
]
