from __future__ import annotations

import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "implementation" / "evidence" / "deployed-runtime.json"
CHECKER = runpy.run_path(str(ROOT / "scripts" / "check-operational-evidence"))


def _validate(payload: Any) -> list[str]:
    validator = CHECKER["validate"]
    assert callable(validator)
    return validator(payload)  # type: ignore[no-any-return]


def test_deployed_runtime_evidence_is_current_and_valid() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check-operational-evidence"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_deployed_runtime_evidence_rejects_failed_or_stale_claims() -> None:
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    failed = deepcopy(payload)
    failed["assertions"]["authorization_enforced"] = False
    assert "deployed-runtime proof contains a failed assertion" in _validate(failed)

    stale = deepcopy(payload)
    stale["source_digest"] = "sha256:" + "0" * 64
    assert (
        "deployed-runtime evidence is stale relative to product sources"
        in _validate(stale)
    )
