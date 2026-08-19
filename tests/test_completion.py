from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHECKER = runpy.run_path(str(ROOT / "scripts" / "check-completion"))
ValidateLedger = Callable[..., tuple[list[str], list[str]]]


def _validate() -> ValidateLedger:
    value = CHECKER["validate_ledger"]
    assert callable(value)
    return value  # type: ignore[return-value]


def test_completion_ledger_has_tracked_independent_evidence() -> None:
    payload = json.loads(
        (ROOT / "implementation" / "completion.json").read_text(encoding="utf-8")
    )
    tracked_files = CHECKER["_tracked_files"]
    assert callable(tracked_files)
    tracked = tracked_files()

    failures, incomplete = _validate()(payload, root=ROOT, tracked_files=tracked)

    assert failures == []
    assert incomplete == [
        "screenshots",
        "controlled_end_to_end",
        "private_repositories",
    ]


def test_completion_ledger_rejects_weak_or_unsafe_evidence(tmp_path: Path) -> None:
    requirements: list[dict[str, Any]] = []
    for identifier in CHECKER["EXPECTED_REQUIREMENTS"]:
        requirements.append(
            {
                "id": identifier,
                "status": "pending",
                "evidence": [],
            }
        )
    requirements[0] = {
        "id": "cli",
        "status": "complete",
        "evidence": ["../outside.py"],
    }
    payload = {"schema_version": "1.0", "requirements": requirements}

    failures, incomplete = _validate()(payload, root=tmp_path, tracked_files={"outside.py"})

    assert "cli: unsafe evidence path '../outside.py'" in failures
    assert "cli: completed requirements need independent evidence" in failures
    assert "cli: completed requirement has no verification evidence" in failures
    assert "cli" not in incomplete
