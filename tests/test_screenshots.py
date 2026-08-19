from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_reproducible_product_screenshots_are_current_and_nonblank() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check-screenshots"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
