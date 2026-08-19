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
    token_env: str = "ADAPTIVE_TUTOR_GITHUB_TOKEN"
    webhook_secret_env: str = "ADAPTIVE_TUTOR_WEBHOOK_SECRET"

    @field_validator("api_url")
    @classmethod
    def public_github_only(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if normalized != "https://api.github.com":
            raise ValueError("only github.com is supported by this public build")
        return normalized


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
    api_token_env: str = "ADAPTIVE_TUTOR_API_TOKEN"
    allow_unauthenticated_loopback: bool = True
    workers: int = Field(default=2, ge=1, le=16)
    lease_seconds: int = Field(default=900, ge=30, le=7200)


class TutorSettings(StrictModel):
    data_dir: Path = DEFAULT_DATA_DIR
    database_path: Path | None = None
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
        return settings
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        settings = TutorSettings.model_validate(raw)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"Invalid configuration at {config_path}: {exc}") from exc
    settings.ensure_runtime_dirs()
    return settings


def write_initial_config(path: Path | None = None, *, force: bool = False) -> tuple[Path, str]:
    config_path = (path or DEFAULT_CONFIG_PATH).expanduser()
    if config_path.exists() and not force:
        raise ConfigurationError(f"Configuration already exists at {config_path}; use --force")
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    api_token = secrets.token_urlsafe(32)
    payload = {
        "data_dir": str(DEFAULT_DATA_DIR),
        "active_curriculum": "systems-foundations",
        "active_profile": "generalist",
        "learner_id": "default",
        "github": {
            "owner": "",
            "workspace_repo": "learning-workspace",
            "curriculum_repo": "private-curricula",
            "api_url": "https://api.github.com",
            "token_env": "ADAPTIVE_TUTOR_GITHUB_TOKEN",
            "webhook_secret_env": "ADAPTIVE_TUTOR_WEBHOOK_SECRET",
        },
        "codex": {"command": "codex", "enabled": True, "sandbox": "read-only"},
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "api_token_env": "ADAPTIVE_TUTOR_API_TOKEN",
            "allow_unauthenticated_loopback": True,
        },
    }
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, api_token
