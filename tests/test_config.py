from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from adaptive_tutor.config import (
    load_settings,
    update_setup_config,
    upsert_secret,
    write_initial_config,
)
from adaptive_tutor.errors import ConfigurationError


def test_initial_config_keeps_generated_secrets_out_of_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ADAPTIVE_TUTOR_API_TOKEN",
        "ADAPTIVE_TUTOR_WEBHOOK_SECRET",
        "ADAPTIVE_TUTOR_GITHUB_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config" / "config.yaml"
    data_dir = tmp_path / "state"

    written_config, secrets_path = write_initial_config(
        config_path,
        data_dir=data_dir,
        github_owner="example-owner",
        app_id=123,
        installation_id=456,
        webhook_url="https://tutor.example.test/",
        server_host="0.0.0.0",  # noqa: S104 - validates container configuration output
    )

    assert written_config == config_path
    assert secrets_path == data_dir / "secrets.env"
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["github"]["owner"] == "example-owner"
    assert raw["github"]["webhook_url"] == "https://tutor.example.test/"
    assert raw["server"]["allow_unauthenticated_loopback"] is False
    assert raw["server"]["host"] == "0.0.0.0"  # noqa: S104 - expected container bind
    assert raw["codex"]["enabled"] is False
    secrets_text = secrets_path.read_text(encoding="utf-8")
    assert "ADAPTIVE_TUTOR_API_TOKEN=" in secrets_text
    assert "ADAPTIVE_TUTOR_WEBHOOK_SECRET=" in secrets_text
    assert secrets_text not in config_path.read_text(encoding="utf-8")

    settings = load_settings(config_path, require_file=True)
    assert settings.api_token
    assert settings.webhook_secret
    assert settings.github.webhook_url == "https://tutor.example.test"

    socket_path = tmp_path / "grader" / "grader.sock"
    monkeypatch.setenv("ADAPTIVE_TUTOR_GRADER_SOCKET", str(socket_path))
    overridden = load_settings(config_path, require_file=True)
    assert overridden.codex.socket_path == socket_path.resolve()


def test_initial_config_refuses_overwrite_without_force(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "state"
    _, secrets_path = write_initial_config(config_path, data_dir=data_dir)
    original = secrets_path.read_text(encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Configuration already exists"):
        write_initial_config(config_path, data_dir=data_dir)
    assert secrets_path.read_text(encoding="utf-8") == original

    write_initial_config(config_path, data_dir=data_dir, force=True)
    assert secrets_path.read_text(encoding="utf-8") != original


def test_initial_config_validates_before_writing(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "state"

    with pytest.raises(ValueError, match="HTTPS"):
        write_initial_config(
            config_path,
            data_dir=data_dir,
            webhook_url="http://insecure.example.test",
        )

    assert not config_path.exists()
    assert not (data_dir / "secrets.env").exists()


def test_setup_config_and_secret_updates_are_atomic_and_private(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "config.yaml"
    data_dir = tmp_path / "state"
    _, secrets_path = write_initial_config(config_path, data_dir=data_dir)

    settings = update_setup_config(
        config_path,
        public_url="https://tutor.example.test/",
        github_owner="example-owner",
        workspace_repo="learning-workspace",
        app_id=123,
        installation_id=456,
        private_key_path=data_dir / "github-app.pem",
        codex_enabled=True,
        evaluator_ref="a" * 40,
    )
    upsert_secret(secrets_path, "ADAPTIVE_TUTOR_WEBHOOK_SECRET", "rotated-webhook-value")

    assert settings.github.webhook_url == "https://tutor.example.test"
    assert settings.github.owner == "example-owner"
    assert settings.github.app_id == 123
    assert settings.github.installation_id == 456
    assert settings.github.evaluator_ref == "a" * 40
    assert settings.codex.enabled is True
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    assert secrets_path.read_text(encoding="utf-8").count("ADAPTIVE_TUTOR_WEBHOOK_SECRET=") == 1
    assert "rotated-webhook-value" in secrets_path.read_text(encoding="utf-8")
    assert "rotated-webhook-value" not in config_path.read_text(encoding="utf-8")
