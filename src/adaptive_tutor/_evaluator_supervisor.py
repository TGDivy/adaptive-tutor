"""Trusted pytest controller used inside the ephemeral evaluator sandbox."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest


class _ResultRecorder:
    """Collect bounded outcomes in the controller, which never imports learner code."""

    def __init__(self) -> None:
        self.collected = 0
        self.outcomes: dict[str, str] = {}
        self.internal_errors = 0
        self.worker_crashes = 0

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: object, ids: Sequence[str]) -> None:
        del node
        self.collected = len(ids)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if (report.when == "setup" and report.outcome != "passed") or report.when == "call":
            self.outcomes[report.nodeid] = report.outcome
        elif report.when == "teardown" and report.outcome == "failed":
            self.outcomes[report.nodeid] = "failed"

    def pytest_internalerror(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.internal_errors += 1

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: object, error: object | None) -> None:
        del node
        if error is not None:
            self.worker_crashes += 1


def _write_record(status_fd: int, record: dict[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    view = memoryview(payload)
    while view:
        written = os.write(status_fd, view)
        view = view[written:]


def _read_nonce(nonce_fd: int) -> str:
    payload = bytearray()
    while len(payload) <= 64:
        chunk = os.read(nonce_fd, 65 - len(payload))
        if not chunk:
            break
        payload.extend(chunk)
    nonce = payload.decode("ascii", errors="strict")
    if not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("invalid supervisor nonce")
    return nonce


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--status-fd", required=True, type=int)
    parser.add_argument("--nonce-fd", required=True, type=int)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--public-tests", required=True, type=Path)
    parser.add_argument("--hidden-tests", required=True, type=Path)
    arguments = parser.parse_args(argv)

    # xdist workers must not inherit the only channel that can attest completion.
    os.set_inheritable(arguments.status_fd, False)
    os.set_inheritable(arguments.nonce_fd, False)
    nonce = _read_nonce(arguments.nonce_fd)
    os.close(arguments.nonce_fd)
    os.chdir(arguments.workdir)
    recorder = _ResultRecorder()
    supervisor_error = False
    try:
        exit_code = int(
            pytest.main(
                [
                    "-p",
                    "xdist.plugin",
                    "-p",
                    "no:cacheprovider",
                    "--capture=fd",
                    "--tb=no",
                    "--strict-config",
                    "--strict-markers",
                    "--max-worker-restart=0",
                    "--numprocesses=1",
                    "--dist=load",
                    "--basetemp=/tmp/pytest",
                    "-q",
                    str(arguments.public_tests),
                    str(arguments.hidden_tests),
                ],
                plugins=[recorder],
            )
        )
    except BaseException:
        exit_code = 3
        supervisor_error = True

    counts = {name: 0 for name in ("passed", "failed", "skipped")}
    for outcome in recorder.outcomes.values():
        if outcome in counts:
            counts[outcome] += 1
        else:
            recorder.internal_errors += 1
    _write_record(
        arguments.status_fd,
        {
            "schema_version": "1.0",
            "nonce": nonce,
            "exit_code": exit_code,
            "collected": recorder.collected,
            **counts,
            "internal_errors": recorder.internal_errors,
            "worker_crashes": recorder.worker_crashes,
            "supervisor_error": supervisor_error,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
