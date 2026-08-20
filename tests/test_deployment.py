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
    grader = payload["services"]["grader"]
    proxy = payload["services"]["proxy"]

    assert tutor["ports"] == ["127.0.0.1:${TUTOR_PORT:-8765}:8765"]
    assert tutor["read_only"] is True
    assert tutor["cap_drop"] == ["ALL"]
    assert tutor["user"] == "${TUTOR_UID:-1000}:${TUTOR_GID:-1000}"
    assert "worker.env" not in str(tutor["env_file"])
    assert "worker.env" in str(worker["env_file"])
    assert "grader.env" not in str(worker["env_file"])
    assert "grader.env" in str(grader["env_file"])
    assert all(
        not str(volume).endswith(":/var/lib/adaptive-tutor")
        for volume in grader["volumes"]
    )
    assert all(
        not str(volume).endswith(":/etc/adaptive-tutor:ro")
        for volume in grader["volumes"]
    )
    worker_socket_mounts = [
        str(volume)
        for volume in worker["volumes"]
        if ":/run/adaptive-tutor-grader" in str(volume)
    ]
    grader_socket_mounts = [
        str(volume)
        for volume in grader["volumes"]
        if ":/run/adaptive-tutor-grader" in str(volume)
    ]
    assert len(worker_socket_mounts) == 1
    assert worker_socket_mounts[0].endswith(":/run/adaptive-tutor-grader:ro")
    assert len(grader_socket_mounts) == 1
    assert grader_socket_mounts[0].endswith(":/run/adaptive-tutor-grader")
    assert "/var/lib/adaptive-tutor-grader" in str(grader["volumes"])
    assert "codex" not in str(worker["volumes"])
    assert worker["profiles"] == ["remote"]
    assert grader["profiles"] == ["remote"]
    assert tutor["environment"]["ADAPTIVE_TUTOR_SOURCE_REVISION"].startswith(
        "${SOURCE_REVISION:?"
    )
    assert worker["environment"]["ADAPTIVE_TUTOR_SOURCE_REVISION"].startswith(
        "${SOURCE_REVISION:?"
    )
    assert proxy["profiles"] == ["live"]
    assert "@sha256:" in proxy["image"]
    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["cap_add"] == ["NET_BIND_SERVICE"]
    assert proxy["ports"] == ["80:80", "443:443", "443:443/udp"]


def test_systemd_units_restart_and_harden_services() -> None:
    unit_root = ROOT / "deploy" / "systemd"
    for name in ("adaptive-tutor.service", "adaptive-tutor-worker.service"):
        content = (unit_root / name).read_text(encoding="utf-8")
        assert "Restart=on-failure" in content
        assert "NoNewPrivileges=true" in content
        assert "ProtectSystem=strict" in content
        assert "CapabilityBoundingSet=\n" in content
        assert "ReadWritePaths=/var/lib/adaptive-tutor" in content
    grader = (unit_root / "adaptive-tutor-grader.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in grader
    assert "User=adaptive-tutor-grader" in grader
    assert "Group=adaptive-tutor-grader" in grader
    assert "SupplementaryGroups=adaptive-tutor-grader-socket" in grader
    assert "EnvironmentFile=-/etc/adaptive-tutor-grader/grader.env" in grader
    assert "RuntimeDirectoryMode=0750" in grader
    assert "--socket-group adaptive-tutor-grader-socket" in grader
    assert (
        "InaccessiblePaths=/var/lib/adaptive-tutor /etc/adaptive-tutor "
        "/etc/adaptive-tutor-grader" in grader
    )
    assert "ReadWritePaths=/var/lib/adaptive-tutor-grader" in grader
    worker = (unit_root / "adaptive-tutor-worker.service").read_text(encoding="utf-8")
    assert "Requires=adaptive-tutor.service adaptive-tutor-grader.service" in worker
    assert "ADAPTIVE_TUTOR_GRADER_SOCKET=" in worker
    assert "SupplementaryGroups=adaptive-tutor-grader-socket" in worker
    assert "ReadWritePaths=/var/lib/adaptive-tutor\n" in worker
    assert "/run/adaptive-tutor-grader" not in next(
        line for line in worker.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "InaccessiblePaths=/var/lib/adaptive-tutor-grader /etc/adaptive-tutor-grader" in worker
    tutor = (unit_root / "adaptive-tutor.service").read_text(encoding="utf-8")
    backup = (unit_root / "adaptive-tutor-backup.service").read_text(encoding="utf-8")
    for content in (tutor, backup):
        assert "SupplementaryGroups=adaptive-tutor-grader-socket" not in content
        assert (
            "InaccessiblePaths=/var/lib/adaptive-tutor-grader "
            "/etc/adaptive-tutor-grader -/run/adaptive-tutor-grader" in content
        )
    timer = (unit_root / "adaptive-tutor-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer


def test_container_virtualenv_uses_its_runtime_path() -> None:
    content = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "UV_PROJECT_ENVIRONMENT=/opt/adaptive-tutor" in content
    assert "COPY --from=builder /opt/adaptive-tutor /opt/adaptive-tutor" in content
    assert "COPY --from=builder /build/.venv" not in content
