from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_contract_passes() -> None:
    result = subprocess.run(
        ["./scripts/check-deployment"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_compose_is_loopback_rootless_and_credential_separated() -> None:
    payload = yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    tutor = payload["services"]["tutor"]
    worker = payload["services"]["worker"]

    assert tutor["ports"] == ["127.0.0.1:${TUTOR_PORT:-8765}:8765"]
    assert tutor["read_only"] is True
    assert tutor["cap_drop"] == ["ALL"]
    assert tutor["user"] == "${TUTOR_UID:-1000}:${TUTOR_GID:-1000}"
    assert "worker.env" not in str(tutor["env_file"])
    assert "worker.env" in str(worker["env_file"])
    assert worker["profiles"] == ["remote"]


def test_systemd_units_restart_and_harden_services() -> None:
    unit_root = ROOT / "deploy" / "systemd"
    for name in ("adaptive-tutor.service", "adaptive-tutor-worker.service"):
        content = (unit_root / name).read_text(encoding="utf-8")
        assert "Restart=on-failure" in content
        assert "NoNewPrivileges=true" in content
        assert "ProtectSystem=strict" in content
        assert "CapabilityBoundingSet=\n" in content
        assert "ReadWritePaths=/var/lib/adaptive-tutor" in content
    timer = (unit_root / "adaptive-tutor-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer


def test_container_virtualenv_uses_its_runtime_path() -> None:
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "UV_PROJECT_ENVIRONMENT=/opt/adaptive-tutor" in content
    assert "COPY --from=builder /opt/adaptive-tutor /opt/adaptive-tutor" in content
    assert "COPY --from=builder /build/.venv" not in content
