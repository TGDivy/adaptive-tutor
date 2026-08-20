"""Actionable installation, integration, and service diagnostics."""

from __future__ import annotations

import hmac
import shutil
import stat
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import httpx

from .codex import grader_health
from .config import TutorSettings
from .db import Database
from .github import GitHubAuth, GitHubClient
from .security import sha256_digest
from .setup import HostedSetupProbeEvidence
from .time import parse_time, utc_now


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

    def run(self, *, online: bool = True, live: bool = False) -> list[DoctorCheck]:
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
        checks.append(self._service(live=live))
        if live:
            checks.extend(
                [
                    self._setup_completion(),
                    self._public_tls(),
                    self._webhook_round_trip(),
                    self._codex_canary(),
                    self._worker_health(),
                ]
            )
            if self.settings.github.owner:
                checks.extend(self._github_live())
            else:
                checks.append(
                    DoctorCheck(
                        "Live GitHub integration",
                        "fail",
                        "Repository owner is not configured",
                        "Resume guided setup before running the live doctor.",
                    )
                )
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
                "Codex grader",
                "warn",
                "Qualitative grading is disabled",
                "Enable codex after installing and authenticating the CLI.",
            )
        socket_path = self.settings.codex.socket_path
        if socket_path is None:
            return DoctorCheck(
                "Codex grader",
                "fail",
                "The isolated grader socket is not configured",
                "Start the isolated grader and set ADAPTIVE_TUTOR_GRADER_SOCKET.",
            )
        healthy, detail = grader_health(socket_path)
        if not healthy:
            return DoctorCheck(
                "Codex grader",
                "fail",
                detail,
                "Check the grader service, socket mount, model credential, and logs.",
            )
        return DoctorCheck("Codex grader", "pass", detail)

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

    def _service(self, *, live: bool = False) -> DoctorCheck:
        if live and self.settings.github.webhook_url:
            url = self.settings.github.webhook_url + "/readyz"
        else:
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

    def _setup_completion(self) -> DoctorCheck:
        run = self.database.fetch_one(
            "SELECT id, status FROM setup_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        if run is None or run["status"] != "ready":
            return DoctorCheck(
                "Guided setup",
                "fail",
                "Guided setup has not completed",
                "Run adaptive-tutor setup status, then adaptive-tutor setup resume.",
            )
        incomplete = self.database.fetch_one(
            "SELECT COUNT(*) count FROM setup_steps WHERE run_id=? AND status!='complete'",
            (run["id"],),
        )
        if incomplete and int(incomplete["count"]):
            return DoctorCheck(
                "Guided setup",
                "fail",
                "Setup is marked ready but contains incomplete steps",
                "Inspect setup state and restore a consistent backup.",
            )
        return DoctorCheck("Guided setup", "pass", "all live setup steps are complete")

    def _public_tls(self) -> DoctorCheck:
        public_url = self.settings.github.webhook_url
        if not public_url:
            return DoctorCheck(
                "Public TLS",
                "fail",
                "Public HTTPS URL is not configured",
                "Set github.webhook_url through guided setup.",
            )
        try:
            response = httpx.get(
                public_url + "/readyz", timeout=5, follow_redirects=False
            )
        except (httpx.HTTPError, OSError) as exc:
            return DoctorCheck(
                "Public TLS",
                "fail",
                f"Public HTTPS readiness is unreachable ({exc})",
                "Check DNS, the TLS certificate, firewall, and reverse proxy.",
            )
        if response.status_code != 200:
            return DoctorCheck(
                "Public TLS",
                "fail",
                f"Public HTTPS readiness returned {response.status_code}",
                "Route the public HTTPS endpoint to the tutor service readiness endpoint.",
            )
        return DoctorCheck("Public TLS", "pass", f"valid HTTPS at {public_url}")

    def _ready_setup(self) -> dict[str, object] | None:
        return self.database.fetch_one(
            "SELECT * FROM setup_runs WHERE status='ready' ORDER BY completed_at DESC LIMIT 1"
        )

    def _webhook_round_trip(self) -> DoctorCheck:
        run = self._ready_setup()
        if run is None:
            return DoctorCheck("Webhook round trip", "fail", "No completed setup run exists")
        event = self.database.fetch_one(
            """
            SELECT id, event_type, received_at FROM events
            WHERE source='github' AND event_type IN ('ping', 'installation')
              AND received_at >= ? ORDER BY received_at DESC LIMIT 1
            """,
            (run["created_at"],),
        )
        if event is None:
            return DoctorCheck(
                "Webhook round trip",
                "fail",
                "No signed setup webhook is in durable event storage",
                "Redeliver the GitHub App ping and resume setup.",
            )
        return DoctorCheck(
            "Webhook round trip",
            "pass",
            f"signed {event['event_type']} delivery persisted as {event['id']}",
        )

    def _codex_canary(self) -> DoctorCheck:
        run = self._ready_setup()
        if run is None:
            return DoctorCheck("Codex canary", "fail", "No completed setup run exists")
        invocation = self.database.fetch_one(
            """
            SELECT id FROM model_invocations
            WHERE purpose='setup_canary' AND status='succeeded' AND started_at >= ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (run["created_at"],),
        )
        if invocation is None:
            return DoctorCheck(
                "Codex canary",
                "fail",
                "No schema-valid isolated setup canary is recorded",
                "Start the grader and resume guided setup.",
            )
        return DoctorCheck(
            "Codex canary", "pass", f"schema-valid invocation {invocation['id']}"
        )

    def _worker_health(self) -> DoctorCheck:
        worker = self.database.fetch_one(
            """
            SELECT worker_id, heartbeat_at FROM worker_heartbeats
            WHERE status='running' ORDER BY heartbeat_at DESC LIMIT 1
            """
        )
        heartbeat = parse_time(str(worker["heartbeat_at"])) if worker else None
        maximum_age = timedelta(seconds=max(10, min(self.settings.server.lease_seconds // 3, 60)))
        if heartbeat is None or utc_now() - heartbeat > maximum_age:
            return DoctorCheck(
                "Worker health",
                "fail",
                "No fresh persistent-worker heartbeat is recorded",
                "Start or restart the worker service and inspect its logs.",
            )
        assert worker is not None  # heartbeat cannot exist without its selected row
        return DoctorCheck(
            "Worker health", "pass", f"{worker['worker_id']} heartbeat is fresh"
        )

    def _github_live(self) -> list[DoctorCheck]:
        client = GitHubClient(self.settings.github)
        checks: list[DoctorCheck] = []
        try:
            checks.append(self._github_app_scope(client))
            checks.append(self._evaluator_controls(client))
            checks.append(self._hosted_ci_artifact(client))
            checks.append(self._first_assignment(client))
        finally:
            client.close()
        return checks

    def _github_app_scope(self, client: GitHubClient) -> DoctorCheck:
        try:
            scope = client.verify_app_installation_scope()
        except Exception as exc:  # diagnostic boundary
            return DoctorCheck(
                "GitHub App scope",
                "fail",
                str(exc),
                "Reinstall the App with only the documented permissions and workspace selected.",
            )
        return DoctorCheck(
            "GitHub App scope",
            "pass",
            f"least-privilege App is limited to {scope['repository_full_name']}",
        )

    def _evaluator_controls(self, client: GitHubClient) -> DoctorCheck:
        control = self.database.fetch_one(
            "SELECT * FROM evaluator_control_planes ORDER BY configured_at DESC LIMIT 1"
        )
        if control is None:
            return DoctorCheck(
                "Protected evaluator controls",
                "fail",
                "No verified evaluator control plane is recorded",
                "Resume guided setup to install protected evaluator controls.",
            )
        try:
            observed = client.verify_evaluator_control(
                expected_repository_id=int(control["repository_id"]),
                expected_workflow_digest=str(control["workflow_digest"]),
                expected_key_id=str(control["evaluator_key_id"]),
            )
        except Exception as exc:  # diagnostic boundary
            return DoctorCheck(
                "Protected evaluator controls",
                "fail",
                str(exc),
                "Restore the protected workflow/key or perform a verified control rotation.",
            )
        if (
            str(observed["repository_full_name"]).casefold()
            != str(control["repository_full_name"]).casefold()
            or str(observed["default_branch"]) != str(control["default_branch"])
            or self.settings.github.evaluator_ref != str(control["evaluator_ref"])
        ):
            return DoctorCheck(
                "Protected evaluator controls",
                "fail",
                "Remote control identity differs from durable setup state",
                "Restore the setup-pinned repository, branch, and evaluator revision.",
            )
        return DoctorCheck(
            "Protected evaluator controls",
            "pass",
            f"workflow {control['workflow_digest']} and key {control['evaluator_key_id']}",
        )

    def _hosted_ci_artifact(self, client: GitHubClient) -> DoctorCheck:
        run = self._ready_setup()
        if run is None:
            return DoctorCheck("Hosted CI artifact", "fail", "No completed setup run exists")
        probe = self.database.fetch_one(
            """
            SELECT * FROM hosted_setup_probes
            WHERE setup_run_id=? AND status='passed'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (run["id"],),
        )
        if probe is None or probe.get("actions_run_id") is None:
            return DoctorCheck(
                "Hosted CI artifact",
                "fail",
                "No passed GitHub-hosted setup probe is recorded",
                "Resume guided setup to run the credential-free hosted probe.",
            )
        try:
            observed = client.get_setup_probe_run(
                int(probe["actions_run_id"]), nonce=str(probe["nonce"])
            )
            artifact = client.download_setup_probe_evidence(int(probe["actions_run_id"]))
            evidence = HostedSetupProbeEvidence.model_validate_json(artifact)
        except Exception as exc:  # diagnostic boundary
            return DoctorCheck(
                "Hosted CI artifact",
                "fail",
                str(exc),
                "Inspect the setup Actions run and resume setup to produce fresh evidence.",
            )
        expected = {
            "nonce": str(probe["nonce"]),
            "repository_id": int(probe["repository_id"]),
            "workflow_commit": str(probe["workflow_commit"]),
            "workflow_digest": str(probe["workflow_digest"]),
            "evaluator_key_id": str(probe["evaluator_key_id"]),
        }
        valid = (
            str(observed["status"]) == "completed"
            and str(observed["conclusion"]) == "success"
            and all(getattr(evidence, name) == value for name, value in expected.items())
            and hmac.compare_digest(sha256_digest(artifact), str(probe["artifact_digest"] or ""))
        )
        if not valid:
            return DoctorCheck(
                "Hosted CI artifact",
                "fail",
                "Hosted probe provenance or artifact digest differs from setup state",
                "Treat the hosted controls as changed and investigate before grading.",
            )
        return DoctorCheck(
            "Hosted CI artifact",
            "pass",
            f"Actions run {probe['actions_run_id']} artifact is current and verified",
        )

    def _first_assignment(self, client: GitHubClient) -> DoctorCheck:
        assignment = self.database.fetch_one(
            """
            SELECT id, branch_name, pull_number, head_sha FROM assignments
            WHERE learner_id=? AND pull_number IS NOT NULL
            ORDER BY created_at LIMIT 1
            """,
            (self.settings.learner_id,),
        )
        if (
            assignment is None
            or not assignment.get("branch_name")
            or not assignment.get("head_sha")
        ):
            return DoctorCheck(
                "First assignment PR",
                "fail",
                "No published first assignment is recorded",
                "Resume guided setup to publish the first assignment.",
            )
        try:
            client.verify_assignment_pull_request(
                int(assignment["pull_number"]),
                branch=str(assignment["branch_name"]),
                head_sha=str(assignment["head_sha"]),
            )
        except Exception as exc:  # diagnostic boundary
            return DoctorCheck(
                "First assignment PR",
                "fail",
                str(exc),
                "Restore or republish the setup-created assignment pull request.",
            )
        return DoctorCheck(
            "First assignment PR",
            "pass",
            f"{assignment['id']} is live as pull request #{assignment['pull_number']}",
        )
