from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_documentation_contract_passes() -> None:
    result = subprocess.run(
        ["./scripts/check-docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_spec_is_included_from_one_binding_source() -> None:
    page = (ROOT / "docs" / "specification.md").read_text(encoding="utf-8")
    assert '--8<-- "SPEC.md"' in page
    assert "Do not reinterpret" not in page
