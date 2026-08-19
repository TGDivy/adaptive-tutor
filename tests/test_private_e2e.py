from __future__ import annotations

import runpy
import stat
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = runpy.run_path(str(ROOT / "scripts" / "prove-private-e2e"))


def _driver(name: str) -> Any:
    return DRIVER[name]


def test_private_proof_requires_explicit_resource_confirmation() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/prove-private-e2e"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--confirm-private-resources" in result.stdout


def test_private_proof_sanitizes_registered_values_and_tokens() -> None:
    private_values = _driver("_PRIVATE_VALUES")
    private_values.add("dedicated-private-resource")
    token = "gh" + "o_" + "a" * 24

    sanitized = _driver("_sanitize")(
        f"owner=dedicated-private-resource token={token}"
    )

    assert sanitized == "owner=[private] token=[REDACTED]"


def test_runner_environment_rejects_credential_bearing_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = "http://" + "operator:" + "secret" + "@proxy.example.test"
    monkeypatch.setenv("HTTPS_PROXY", proxy)

    with pytest.raises(RuntimeError, match="credential-bearing proxies"):
        _driver("_runner_environment")(tmp_path)


def test_runner_archive_rejects_parent_traversal(tmp_path: Path) -> None:
    payload = BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        item = tarfile.TarInfo("../outside")
        item.size = 4
        archive.addfile(item, BytesIO(b"data"))

    with pytest.raises(RuntimeError, match="unsafe path"):
        _driver("_extract_runner")(payload.getvalue(), "runner.tar.gz", tmp_path / "runner")

    assert not (tmp_path / "outside").exists()


class _RunningProcess:
    returncode: int | None = None

    @staticmethod
    def poll() -> None:
        return None


def test_runner_handoff_accepts_only_owner_private_request(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner"
    trusted = runner_root / "_work" / "_temp" / "trusted"
    executable_path = trusted / "bin"
    executable_path.mkdir(parents=True, mode=0o700)
    trusted.chmod(0o700)
    executable_path.chmod(0o700)
    request = trusted / "stage-request"
    request.write_text("", encoding="utf-8")
    request.chmod(0o600)
    run_log = tmp_path / "runner.log"
    run_log.write_text("", encoding="utf-8")

    observed = _driver("_wait_for_stage_request")(
        runner_root, _RunningProcess(), run_log, timeout=1
    )

    assert observed == trusted
    assert not request.exists()


def test_runner_handoff_rejects_symlink_request(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner"
    trusted = runner_root / "_work" / "_temp" / "trusted"
    executable_path = trusted / "bin"
    executable_path.mkdir(parents=True, mode=0o700)
    trusted.chmod(0o700)
    executable_path.chmod(0o700)
    target = tmp_path / "request-target"
    target.write_text("", encoding="utf-8")
    request = trusted / "stage-request"
    request.symlink_to(target)
    run_log = tmp_path / "runner.log"
    run_log.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="owner-only file"):
        _driver("_wait_for_stage_request")(
            runner_root, _RunningProcess(), run_log, timeout=1
        )


def test_staged_runner_entrypoint_is_private_regular_file(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    (trusted / "bin").mkdir(parents=True, mode=0o700)

    destination = _driver("_stage_runner_tool")(trusted)
    info = destination.lstat()

    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert destination.read_bytes() == (ROOT / ".venv/bin/adaptive-tutor").read_bytes()
