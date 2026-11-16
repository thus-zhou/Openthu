"""Run the frozen v0.2 contract, JSON Schema and ledger validator in CI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v020_contracts_validate_and_reject_samples() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "contracts" / "v0.2" / "validate.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["contracts"] == 8
    assert payload["compatibility_baselines"] == 1
    assert payload["schema_samples"] >= 9
    assert payload["rejection_samples"] >= 35
    assert payload["extra_samples"] >= 8
