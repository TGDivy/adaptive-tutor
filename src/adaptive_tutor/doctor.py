"""Actionable installation, integration, and service diagnostics."""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from typing import Literal

import httpx

from .config import TutorSettings
from .db import Database
from .github import GitHubAuth, GitHubClient


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str
    fix: str | None = None


class Doctor:
    def __init__(self, settings: TutorSettings, database: Database) -> None:
        self.settings = settings
        self.database = database

    def run(self, *, online: bool = True) -> list[DoctorCheck]:
        checks = [
            self._database(),
            self._configuration(),
            self._filesystem(),
            self._codex(),
            self._tooling(),
            self._github_configuration(),
        ]
        if online and self.settings.github.owner:
            checks.extend(self._github_online())
        else:
            checks.append(
                DoctorCheck(
                    "GitHub connectivity",
                    "warn",
                    "Not checked" if not online else "Repository owner is not configured",
                    "Set github.owner and GitHub App fields, then rerun doctor.",
                )
            )
        checks.append(self._service())
        return checks

    def _database(self) -> DoctorCheck:
        try:
            applied = self.database.migrate()
            healthy, detail = self.database.integrity_check()
        except Exception as exc:  # diagnostic boundary
            return DoctorCheck(
                "Database",
                "fail",
                str(exc),
                "Check database_path permissions or restore a verified backup.",
            )
        versions = self.database.migration_versions()
        if not healthy:
            return DoctorCheck(
                "Database",
                "fail",
                detail,
                "Stop the service and restore a verified backup.",
            )
        suffix = f"; applied {', '.join(applied)}" if applied else ""
        return DoctorCheck("Database", "pass", f"integrity ok; migrations {versions}{suffix}")

    def _configuration(self) -> DoctorCheck:
        rows = self.database.fetch_one(
            "SELECT COUNT(*) count FROM curricula WHERE id=?",
            (self.settings.active_curriculum,),
        )
        if not rows or not rows["count"]:
            return DoctorCheck(
                "Configuration",
                "fail",
                f"Active curriculum '{self.settings.active_curriculum}' is not loaded",
                "Run adaptive-tutor init or load the configured curriculum package.",
            )
        return DoctorCheck(
            "Configuration",
            "pass",
            f"curriculum {self.settings.active_curriculum}; profile {self.settings.active_profile}",
        )

    def _filesystem(self) -> DoctorCheck:
        paths = [self.settings.data_dir]
        if self.settings.database_path and self.settings.database_path.exists():
            paths.append(self.settings.database_path)
        if self.settings.secrets_file and self.settings.secrets_file.exists():
            paths.append(self.settings.secrets_file)
        too_open = [
            str(path)
            for path in paths
            if stat.S_IMODE(path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        ]
        if too_open:
            return DoctorCheck(
                "Filesystem permissions",
                "fail",
                "Private state is accessible to group/other: " + ", ".join(too_open),
                "Set directories to 0700 and state/secret files to 0600.",
            )
        return DoctorCheck("Filesystem permissions", "pass", "private state is owner-only")

    def _codex(self) -> DoctorCheck:
        if not self.settings.codex.enabled:
            return DoctorCheck(
                "Codex CLI",
                "warn",
                "Qualitative grading is disabled",
                "Enable codex after installing and authenticating the CLI.",
            )
        if not shutil.which(self.settings.codex.command):
            return DoctorCheck(
                "Codex CLI",
                "fail",
                f"'{self.settings.codex.command}' is not on PATH",
                "Install Codex CLI and complete its authentication flow.",
            )
        return DoctorCheck("Codex CLI", "pass", "available for short-lived grading workers")

    @staticmethod
    def _tooling() -> DoctorCheck:
        required = ["git", "python"]
        optional = ["cc", "c++", "docker"]
        missing_required = [name for name in required if not shutil.which(name)]
        available_optional = [name for name in optional if shutil.which(name)]
        if missing_required:
            return DoctorCheck(
                "Compiler/tooling",
                "fail",
                "Missing: " + ", ".join(missing_required),
                "Install Git and Python; add curriculum-specific compilers as needed.",
            )
        return DoctorCheck(
            "Compiler/tooling",
            "pass",
            "Git/Python available; optional: " + (", ".join(available_optional) or "none"),
        )

    def _github_configuration(self) -> DoctorCheck:
        auth = GitHubAuth(self.settings.github)
        if auth.mode() != "github_app":
            if self.settings.github_token:
                return DoctorCheck(
                    "GitHub App configuration",
                    "warn",
                    "A development token is configured instead of a least-privilege GitHub App",
                    "Configure app_id, installation_id, private_key_path, and webhook_url.",
                )
            return DoctorCheck(
                "GitHub App configuration",
                "warn",
                "Not configured; local demo remains available",
                "Follow the GitHub App setup guide before enabling remote assignments.",
            )
        key_path = self.settings.github.private_key_path
        if not key_path or not key_path.is_file():
            return DoctorCheck(
                "GitHub App configuration",
                "fail",
                "Private key file is missing",
                "Download the App key to an owner-only file and update private_key_path.",
            )
        if not self.settings.webhook_secret:
            return DoctorCheck(
                "GitHub App configuration",
                "fail",
                "Webhook secret is missing",
                f"Set {self.settings.github.webhook_secret_env} in the secrets file.",
            )
        return DoctorCheck("GitHub App configuration", "pass", "installation credentials present")

    def _github_online(self) -> list[DoctorCheck]:
        client = GitHubClient(self.settings.github)
        try:
            repository = client.verify_private_repository()
            connectivity = DoctorCheck(
                "GitHub connectivity",
                "pass",
                f"{repository['full_name']} is private and writable",
            )
            if not self.settings.github.webhook_url:
                webhook = DoctorCheck(
                    "Webhook configuration",
                    "warn",
                    "webhook_url is not configured",
                    "Set the public HTTPS webhook URL and register it with the GitHub App.",
                )
            else:
                callback = self.settings.github.webhook_url + "/webhooks/github"
                found = client.webhook_status(callback)
                required = {
                    "push",
                    "pull_request",
                    "workflow_run",
                    "check_suite",
                    "issue_comment",
                }
                if found and found["active"] and required.issubset(set(found["events"])):
                    webhook = DoctorCheck(
                        "Webhook configuration", "pass", f"active hook {found['id']}"
                    )
                else:
                    webhook = DoctorCheck(
                        "Webhook configuration",
                        "fail",
                        "Required active webhook/events were not found",
                        "Run the webhook setup command after the HTTPS endpoint is reachable.",
                    )
            return [connectivity, webhook]
        except Exception as exc:  # diagnostic boundary
            return [
                DoctorCheck(
                    "GitHub connectivity",
                    "fail",
                    str(exc),
                    "Verify App installation, repository selection, network, and permissions.",
                )
            ]
        finally:
            client.close()

    def _service(self) -> DoctorCheck:
        url = f"http://{self.settings.server.host}:{self.settings.server.port}/readyz"
        try:
            response = httpx.get(url, timeout=1.5)
        except (httpx.HTTPError, OSError) as exc:
            return DoctorCheck(
                "Service health",
                "warn",
                f"Service is not currently reachable ({exc})",
                "Start it with the documented systemd or Docker Compose command.",
            )
        if response.status_code == 200:
            return DoctorCheck("Service health", "pass", "readiness endpoint is healthy")
        return DoctorCheck(
            "Service health",
            "fail",
            f"Readiness endpoint returned {response.status_code}",
            "Inspect service logs and database health.",
        )
