"""Configuration loading with secret-by-reference defaults."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConfigurationError

APP_NAME = "adaptive-tutor"
DEFAULT_CONFIG_PATH = Path(user_config_dir(APP_NAME)) / "config.yaml"
DEFAULT_DATA_DIR = Path(user_data_dir(APP_NAME))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class GitHubSettings(StrictModel):
    owner: str = ""
    workspace_repo: str = "learning-workspace"
    curriculum_repo: str = "private-curricula"
    api_url: str = "https://api.github.com"
    app_id: int | None = None
    installation_id: int | None = None
    private_key_path: Path | None = None
    token_env: str = "ADAPTIVE_TUTOR_GITHUB_TOKEN"  # noqa: S105 - environment name
    webhook_secret_env: str = "ADAPTIVE_TUTOR_WEBHOOK_SECRET"  # noqa: S105
    webhook_url: str | None = None

    @field_validator("api_url")
    @classmethod
    def public_github_only(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if normalized != "https://api.github.com":
            raise ValueError("only github.com is supported by this public build")
        return normalized

    @field_validator("webhook_url")
    @classmethod
    def secure_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("https://"):
            raise ValueError("webhook_url must use HTTPS")
        return value.rstrip("/")


class CodexSettings(StrictModel):
    command: str = "codex"
    model: str | None = None
    timeout_seconds: int = Field(default=600, ge=30, le=3600)
    enabled: bool = True
    sandbox: str = "read-only"
    usd_per_million_input_tokens: float = Field(default=0.0, ge=0)
    usd_per_million_output_tokens: float = Field(default=0.0, ge=0)

    @field_validator("sandbox")
    @classmethod
    def read_only_worker(cls, value: str) -> str:
        if value != "read-only":
            raise ValueError("qualitative grading workers must use the read-only sandbox")
        return value


class ServerSettings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    api_token_env: str = "ADAPTIVE_TUTOR_API_TOKEN"  # noqa: S105 - environment name
    allow_unauthenticated_loopback: bool = False
    workers: int = Field(default=2, ge=1, le=16)
    lease_seconds: int = Field(default=900, ge=30, le=7200)


class TutorSettings(StrictModel):
    data_dir: Path = DEFAULT_DATA_DIR
    database_path: Path | None = None
    secrets_file: Path | None = None
    curriculum_paths: list[Path] = Field(default_factory=list)
    active_curriculum: str = "systems-foundations"
    active_profile: str = "generalist"
    learner_id: str = "default"
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)

    def model_post_init(self, __context: Any) -> None:
        self.data_dir = self.data_dir.expanduser().resolve()
        if self.database_path is None:
            self.database_path = self.data_dir / "tutor.sqlite3"
        else:
            self.database_path = self.database_path.expanduser().resolve()
        if self.secrets_file is None:
            self.secrets_file = self.data_dir / "secrets.env"
        else:
            self.secrets_file = self.secrets_file.expanduser().resolve()
        self.curriculum_paths = [path.expanduser().resolve() for path in self.curriculum_paths]

    @property
    def github_token(self) -> str | None:
        return os.environ.get(self.github.token_env)

    @property
    def webhook_secret(self) -> str | None:
        return os.environ.get(self.github.webhook_secret_env)

    @property
    def api_token(self) -> str | None:
        return os.environ.get(self.server.api_token_env)

    def ensure_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def load_settings(path: Path | None = None, *, require_file: bool = False) -> TutorSettings:
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if not config_path.exists():
        if require_file:
            raise ConfigurationError(
                f"Configuration not found at {config_path}. Run 'adaptive-tutor init'."
            )
        settings = TutorSettings()
        settings.ensure_runtime_dirs()
        if settings.secrets_file and settings.secrets_file.is_file():
            _load_secrets_file(settings.secrets_file)
        return settings
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        settings = TutorSettings.model_validate(raw)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration at {config_path}: {exc}") from exc
    settings.ensure_runtime_dirs()
    if settings.secrets_file and settings.secrets_file.is_file():
        _load_secrets_file(settings.secrets_file)
    return settings


def write_initial_config(
    path: Path | None = None,
    *,
    force: bool = False,
    data_dir: Path | None = None,
    github_owner: str = "",
    workspace_repo: str = "learning-workspace",
    curriculum_repo: str = "private-curricula",
    app_id: int | None = None,
    installation_id: int | None = None,
    private_key_path: Path | None = None,
    webhook_url: str | None = None,
    server_host: str = "127.0.0.1",
) -> tuple[Path, Path]:
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    target_data_dir = (data_dir or DEFAULT_DATA_DIR).expanduser().resolve()
    secrets_path = target_data_dir / "secrets.env"
    if config_path.exists() and not force:
        raise ConfigurationError(f"Configuration already exists at {config_path}; use --force")
    if secrets_path.exists() and not force:
        raise ConfigurationError(f"Secrets already exist at {secrets_path}; use --force")
    payload = {
        "data_dir": str(target_data_dir),
        "active_curriculum": "systems-foundations",
        "active_profile": "generalist",
        "learner_id": "default",
        "github": {
            "owner": github_owner,
            "workspace_repo": workspace_repo,
            "curriculum_repo": curriculum_repo,
            "api_url": "https://api.github.com",
            "token_env": "ADAPTIVE_TUTOR_GITHUB_TOKEN",
            "webhook_secret_env": "ADAPTIVE_TUTOR_WEBHOOK_SECRET",
            "app_id": app_id,
            "installation_id": installation_id,
            "private_key_path": str(private_key_path.expanduser().resolve())
            if private_key_path
            else None,
            "webhook_url": webhook_url,
        },
        "codex": {"command": "codex", "enabled": True, "sandbox": "read-only"},
        "server": {
            "host": server_host,
            "port": 8765,
            "api_token_env": "ADAPTIVE_TUTOR_API_TOKEN",
            "allow_unauthenticated_loopback": False,
        },
    }
    TutorSettings.model_validate(payload)
    api_token = secrets.token_urlsafe(32)
    webhook_secret = secrets.token_urlsafe(48)
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.parent.chmod(0o700)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    secrets_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets_path.parent.chmod(0o700)
    secrets_path.write_text(
        "\n".join(
            (
                f"ADAPTIVE_TUTOR_API_TOKEN={api_token}",
                f"ADAPTIVE_TUTOR_WEBHOOK_SECRET={webhook_secret}",
                "",
            )
        ),
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)
    return config_path, secrets_path


def _load_secrets_file(path: Path) -> None:
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"Invalid secrets file line {line_number} in {path}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name.startswith("ADAPTIVE_TUTOR_") or not name.replace("_", "").isalnum():
            raise ConfigurationError(f"Invalid secret variable name on line {line_number}")
        os.environ.setdefault(name, value.strip())
