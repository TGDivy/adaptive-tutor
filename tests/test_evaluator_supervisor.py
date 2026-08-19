from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adaptive_tutor import _evaluator_supervisor as supervisor


def pipe_with(value: bytes) -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, value)
    os.close(write_fd)
    return read_fd, write_fd


def run_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_pytest_main: Any,
) -> dict[str, Any]:
    status_read, status_write = os.pipe()
    nonce_read, _ = pipe_with(b"a" * 64)
    public_tests = tmp_path / "public"
    hidden_tests = tmp_path / "hidden"
    public_tests.mkdir()
    hidden_tests.mkdir()
    original_directory = Path.cwd()
    monkeypatch.setattr(supervisor.pytest, "main", fake_pytest_main)
    try:
        result = supervisor.main(
            [
                "--status-fd",
                str(status_write),
                "--nonce-fd",
                str(nonce_read),
                "--workdir",
                str(tmp_path),
                "--public-tests",
                str(public_tests),
                "--hidden-tests",
                str(hidden_tests),
            ]
        )
    finally:
        os.chdir(original_directory)
        os.close(status_write)
    assert result == 0
    try:
        return json.loads(os.read(status_read, 16 * 1024).decode())
    finally:
        os.close(status_read)


def test_supervisor_records_complete_controller_owned_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_main(arguments: list[str], *, plugins: list[object]) -> int:
        assert "xdist.plugin" in arguments
        assert "--numprocesses=1" in arguments
        recorder = plugins[0]
        assert isinstance(recorder, supervisor._ResultRecorder)
        recorder.pytest_xdist_node_collection_finished(object(), ["one", "two"])
        recorder.pytest_runtest_logreport(
            SimpleNamespace(when="setup", outcome="passed", nodeid="one")  # type: ignore[arg-type]
        )
        recorder.pytest_runtest_logreport(
            SimpleNamespace(when="call", outcome="passed", nodeid="one")  # type: ignore[arg-type]
        )
        recorder.pytest_runtest_logreport(
            SimpleNamespace(when="call", outcome="failed", nodeid="two")  # type: ignore[arg-type]
        )
        recorder.pytest_runtest_logreport(
            SimpleNamespace(when="teardown", outcome="failed", nodeid="two")  # type: ignore[arg-type]
        )
        recorder.pytest_testnodedown(object(), None)
        return 1

    record = run_supervisor(tmp_path, monkeypatch, fake_main)

    assert record == {
        "schema_version": "1.0",
        "nonce": "a" * 64,
        "exit_code": 1,
        "collected": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "internal_errors": 0,
        "worker_crashes": 0,
        "supervisor_error": False,
    }


def test_supervisor_attests_its_own_failure_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_main(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise RuntimeError("pytest startup failed")

    record = run_supervisor(tmp_path, monkeypatch, fake_main)

    assert record["exit_code"] == 3
    assert record["supervisor_error"] is True
    assert record["collected"] == 0


def test_result_recorder_tracks_skips_internal_errors_and_worker_crashes() -> None:
    recorder = supervisor._ResultRecorder()
    recorder.pytest_runtest_logreport(
        SimpleNamespace(when="setup", outcome="skipped", nodeid="one")  # type: ignore[arg-type]
    )
    recorder.pytest_internalerror("failure")
    recorder.pytest_testnodedown(object(), RuntimeError("worker stopped"))

    assert recorder.outcomes == {"one": "skipped"}
    assert recorder.internal_errors == 1
    assert recorder.worker_crashes == 1


@pytest.mark.parametrize("payload", [b"short", b"g" * 64, b"a" * 65, b"\xff" * 64])
def test_supervisor_rejects_invalid_nonce(payload: bytes) -> None:
    read_fd, _ = pipe_with(payload)
    try:
        with pytest.raises((UnicodeError, ValueError), match=r"nonce|ascii"):
            supervisor._read_nonce(read_fd)
    finally:
        os.close(read_fd)
