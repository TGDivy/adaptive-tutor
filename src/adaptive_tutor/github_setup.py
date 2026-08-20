"""Authenticated GitHub repository and App bootstrap for guided setup."""

from __future__ import annotations

import base64
import binascii
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
from .runner import EVALUATOR_KIT_FILES, evaluator_kit_digest
from .security import redact, sha256_digest
from .time import iso_now, utc_now
from .trusted_bundles import TrustedBundleStore

if TYPE_CHECKING:
    from .setup import SetupRun

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_APP_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
_MANIFEST_CODE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_PUBLIC_REPOSITORY = "TGDivy/adaptive-tutor"
_EVALUATOR_WORKFLOW_SOURCE = "deploy/workspace/adaptive-tutor-evaluate.yml"
_SETUP_PROBE_WORKFLOW_SOURCE = "deploy/workspace/adaptive-tutor-setup-probe.yml"
_EVALUATOR_WORKFLOW_PATH = ".github/workflows/adaptive-tutor-evaluate.yml"
_SETUP_PROBE_WORKFLOW_PATH = ".github/workflows/adaptive-tutor-setup-probe.yml"
_EVALUATOR_KEY_PATH = ".adaptive-tutor/evaluator-signing.pub"


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


@dataclass(frozen=True)
class PublicEvaluatorSource:
    revision: str
    workflow: str
    setup_probe_workflow: str
    kit_digest: str


@dataclass(frozen=True)
class InstalledEvaluatorControls:
    repository_id: int
    repository_full_name: str
    default_branch: str
    workflow_commit: str
    branch_protected: bool


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

    def public_evaluator_source(self, revision: str) -> PublicEvaluatorSource:
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ConfigurationError("Public evaluator revision must be an exact commit")
        self._run(["api", f"repos/{_PUBLIC_REPOSITORY}/commits/{revision}"])
        workflow = self._public_file(revision, _EVALUATOR_WORKFLOW_SOURCE)
        setup_probe_workflow = self._public_file(revision, _SETUP_PROBE_WORKFLOW_SOURCE)
        digest = hashlib.sha256()
        for name in EVALUATOR_KIT_FILES:
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(self._public_file(revision, f"src/adaptive_tutor/{name}").encode())
            digest.update(b"\0")
        return PublicEvaluatorSource(
            revision=revision,
            workflow=workflow,
            setup_probe_workflow=setup_probe_workflow,
            kit_digest="sha256:" + digest.hexdigest(),
        )

    def install_evaluator_controls(
        self,
        *,
        owner: str,
        repository: str,
        workflow: str,
        setup_probe_workflow: str,
        verification_key: str,
    ) -> InstalledEvaluatorControls:
        _validate_owner(owner)
        _validate_repository(repository)
        if re.fullmatch(r"ed25519:[0-9a-f]{64}\n?", verification_key) is None:
            raise SecurityError("Evaluator verification key is invalid")
        full_name = f"{owner}/{repository}"
        metadata = self._api_json(["api", f"repos/{full_name}"])
        if metadata.get("private") is not True:
            raise SecurityError("Learning workspace must remain private")
        repository_id = int(metadata.get("id") or 0)
        default_branch = str(metadata.get("default_branch") or "")
        if repository_id <= 0 or not default_branch:
            raise ExternalServiceError("GitHub workspace metadata is incomplete")
        self._put_repository_file(
            full_name,
            default_branch,
            _EVALUATOR_WORKFLOW_PATH,
            workflow,
            "Install protected Adaptive Tutor evaluator workflow",
        )
        self._put_repository_file(
            full_name,
            default_branch,
            _SETUP_PROBE_WORKFLOW_PATH,
            setup_probe_workflow,
            "Install protected Adaptive Tutor hosted setup probe",
        )
        self._put_repository_file(
            full_name,
            default_branch,
            _EVALUATOR_KEY_PATH,
            verification_key,
            "Install protected Adaptive Tutor verification key",
        )
        self._protect_default_branch(full_name, default_branch)
        reference = self._api_json(
            ["api", f"repos/{full_name}/git/ref/heads/{quote(default_branch, safe='')}"]
        )
        workflow_commit = str((reference.get("object") or {}).get("sha") or "")
        if re.fullmatch(r"[0-9a-f]{40,64}", workflow_commit) is None:
            raise SecurityError("Protected evaluator workflow commit is invalid")
        self._verify_default_branch_protection(full_name, default_branch)
        return InstalledEvaluatorControls(
            repository_id=repository_id,
            repository_full_name=str(metadata.get("full_name") or full_name),
            default_branch=default_branch,
            workflow_commit=workflow_commit,
            branch_protected=True,
        )

    def _public_file(self, revision: str, path: str) -> str:
        result = self._run(
            [
                "api",
                "--method",
                "GET",
                f"repos/{_PUBLIC_REPOSITORY}/contents/{path}",
                "-f",
                f"ref={revision}",
                "-H",
                "Accept: application/vnd.github.raw+json",
            ]
        )
        return result.stdout

    def _put_repository_file(
        self,
        full_name: str,
        branch: str,
        path: str,
        content: str,
        message: str,
    ) -> None:
        endpoint = f"repos/{full_name}/contents/{path}"
        existing = self._run(
            ["api", "--method", "GET", endpoint, "-f", f"ref={branch}"],
            allow_failure=True,
        )
        existing_sha: str | None = None
        if existing.returncode == 0:
            try:
                payload = json.loads(existing.stdout)
                existing_sha = str(payload["sha"])
                encoded = str(payload["content"])
                observed = base64.b64decode("".join(encoded.split()), validate=True).decode()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
                raise ExternalServiceError(
                    f"GitHub returned invalid content metadata for {path}"
                ) from exc
            if observed == content:
                return
        request: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing_sha:
            request["sha"] = existing_sha
        self._run(
            ["api", "--method", "PUT", endpoint, "--input", "-"],
            input_text=json.dumps(request, sort_keys=True),
            timeout=60,
        )

    def _protect_default_branch(self, full_name: str, branch: str) -> None:
        protection = {
            "required_status_checks": None,
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
                "required_approving_review_count": 1,
                "require_last_push_approval": True,
            },
            "restrictions": None,
            "required_linear_history": True,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "block_creations": False,
            "required_conversation_resolution": True,
            "lock_branch": False,
            "allow_fork_syncing": False,
        }
        self._run(
            [
                "api",
                "--method",
                "PUT",
                f"repos/{full_name}/branches/{quote(branch, safe='')}/protection",
                "--input",
                "-",
            ],
            input_text=json.dumps(protection, sort_keys=True),
        )

    def _verify_default_branch_protection(self, full_name: str, branch: str) -> None:
        protection = self._api_json(
            ["api", f"repos/{full_name}/branches/{quote(branch, safe='')}/protection"]
        )
        reviews = protection.get("required_pull_request_reviews") or {}
        enforce_admins = protection.get("enforce_admins") or {}
        force_pushes = protection.get("allow_force_pushes") or {}
        deletions = protection.get("allow_deletions") or {}
        if (
            int(reviews.get("required_approving_review_count") or 0) < 1
            or enforce_admins.get("enabled") is not True
            or force_pushes.get("enabled") is True
            or deletions.get("enabled") is True
        ):
            raise SecurityError("Workspace default-branch protection is incomplete")

    def _api_json(self, arguments: list[str]) -> dict[str, Any]:
        result = self._run(arguments)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalServiceError("GitHub CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ExternalServiceError("GitHub CLI returned an unexpected response")
        return payload

    def _run(
        self,
        arguments: list[str],
        *,
        allow_failure: bool = False,
        timeout: int = 30,
        input_text: str | None = None,
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
            input=input_text,
        )
        if result.returncode and not allow_failure:
            detail = redact((result.stderr or result.stdout).strip())[-1200:]
            raise ExternalServiceError(
                f"GitHub CLI {' '.join(arguments[:2])} failed: {detail or 'unknown error'}"
            )
        return result


class EvaluatorControlProvisioner:
    """Install, verify, and durably bind the hosted evaluator control plane."""

    def __init__(
        self,
        settings: TutorSettings,
        database: Database,
        config_path: Path,
        *,
        bootstrap: GitHubCLIBootstrap | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.config_path = config_path.expanduser().resolve()
        self.bootstrap = bootstrap

    def ensure(self, run: SetupRun) -> dict[str, Any]:
        bootstrap = self.bootstrap or GitHubCLIBootstrap()
        revision = _resolve_evaluator_revision(self.settings)
        source = bootstrap.public_evaluator_source(revision)
        local_kit_digest = evaluator_kit_digest()
        if source.kit_digest != local_kit_digest:
            raise SecurityError("Installed evaluator sources do not match the pinned public commit")
        if self.settings.github.evaluator_ref != revision:
            updated = update_setup_config(
                self.config_path,
                public_url=run.public_url,
                evaluator_ref=revision,
            )
            self.settings.github = updated.github
        verification_key = TrustedBundleStore(self.settings.data_dir).public_verification_key()
        key_bytes = bytes.fromhex(verification_key.strip().removeprefix("ed25519:"))
        key_id = hashlib.sha256(key_bytes).hexdigest()[:16]
        workflow_digest = sha256_digest(source.workflow)
        installed = bootstrap.install_evaluator_controls(
            owner=self.settings.github.owner,
            repository=self.settings.github.workspace_repo,
            workflow=source.workflow,
            setup_probe_workflow=source.setup_probe_workflow,
            verification_key=verification_key,
        )
        expected_repository_id = int(
            _run_step(run, "github_repository").external_ids.get("repository_id", 0)
        )
        if installed.repository_id != expected_repository_id:
            raise SecurityError("Evaluator controls were installed in the wrong repository")
        github = GitHubClient(self.settings.github)
        try:
            verified = github.verify_evaluator_control(
                expected_repository_id=installed.repository_id,
                expected_workflow_digest=workflow_digest,
                expected_key_id=key_id,
            )
        finally:
            github.close()
        if (
            verified["workflow_commit"] != installed.workflow_commit
            or verified["default_branch"] != installed.default_branch
            or str(verified["repository_full_name"]).casefold()
            != installed.repository_full_name.casefold()
        ):
            raise SecurityError("Evaluator controls changed during setup verification")
        values = {
            "repository_id": installed.repository_id,
            "repository_full_name": installed.repository_full_name,
            "default_branch": installed.default_branch,
            "workflow_path": _EVALUATOR_WORKFLOW_PATH,
            "workflow_commit": installed.workflow_commit,
            "workflow_digest": workflow_digest,
            "evaluator_ref": revision,
            "evaluator_kit_digest": local_kit_digest,
            "evaluator_key_id": key_id,
        }
        self._persist(values)
        stored = self.database.fetch_one(
            "SELECT * FROM evaluator_control_planes WHERE repository_id=?",
            (installed.repository_id,),
        )
        if stored is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("Evaluator control plane was not persisted")
        return stored

    def _persist(self, values: dict[str, Any]) -> None:
        rows = self.database.fetch_all("SELECT * FROM evaluator_control_planes")
        if any(int(row["repository_id"]) != int(values["repository_id"]) for row in rows):
            raise SecurityError("A different workspace control plane is already configured")
        existing = rows[0] if rows else None
        identity_fields = (
            "repository_full_name",
            "default_branch",
            "workflow_path",
            "workflow_digest",
            "evaluator_ref",
            "evaluator_kit_digest",
            "evaluator_key_id",
        )
        changed = existing is not None and any(
            str(existing[name]) != str(values[name]) for name in identity_fields
        )
        assignment_count = self.database.fetch_one("SELECT COUNT(*) count FROM assignments")
        if changed and assignment_count and int(assignment_count["count"]) > 0:
            raise SecurityError(
                "Evaluator controls cannot rotate after assignments exist; use a verified rotation"
            )
        now = iso_now()
        configured_at = str(existing["configured_at"]) if existing else now
        self.database.execute(
            """
            INSERT INTO evaluator_control_planes(
                repository_id, repository_full_name, default_branch, workflow_path,
                workflow_commit, workflow_digest, evaluator_ref, evaluator_kit_digest,
                evaluator_key_id, configured_at, verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository_id) DO UPDATE SET
                repository_full_name=excluded.repository_full_name,
                default_branch=excluded.default_branch,
                workflow_path=excluded.workflow_path,
                workflow_commit=excluded.workflow_commit,
                workflow_digest=excluded.workflow_digest,
                evaluator_ref=excluded.evaluator_ref,
                evaluator_kit_digest=excluded.evaluator_kit_digest,
                evaluator_key_id=excluded.evaluator_key_id,
                configured_at=excluded.configured_at,
                verified_at=excluded.verified_at
            """,
            (
                values["repository_id"],
                values["repository_full_name"],
                values["default_branch"],
                values["workflow_path"],
                values["workflow_commit"],
                values["workflow_digest"],
                values["evaluator_ref"],
                values["evaluator_kit_digest"],
                values["evaluator_key_id"],
                configured_at,
                now,
            ),
        )


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


def _resolve_evaluator_revision(settings: TutorSettings) -> str:
    if settings.github.evaluator_ref:
        return settings.github.evaluator_ref
    environment_revision = os.environ.get("ADAPTIVE_TUTOR_SOURCE_REVISION", "").strip()
    if environment_revision:
        if re.fullmatch(r"[0-9a-f]{40}", environment_revision) is None:
            raise ConfigurationError(
                "ADAPTIVE_TUTOR_SOURCE_REVISION must be an exact 40-character commit"
            )
        return environment_revision
    git = shutil.which("git")
    if git:
        for parent in Path(__file__).resolve().parents:
            if not (parent / ".git").exists():
                continue
            result = subprocess.run(  # noqa: S603 - fixed git executable and package parent
                [str(Path(git).resolve()), "-C", str(parent), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            revision = result.stdout.strip()
            if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", revision):
                return revision
    raise ConfigurationError(
        "Cannot identify the installed public source commit; set --evaluator-ref or "
        "ADAPTIVE_TUTOR_SOURCE_REVISION"
    )


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
    "EvaluatorControlProvisioner",
    "GitHubAppManifestLaunch",
    "GitHubAppSetupService",
    "GitHubCLIBootstrap",
    "GitHubSettings",
    "InstalledEvaluatorControls",
    "PublicEvaluatorSource",
]
