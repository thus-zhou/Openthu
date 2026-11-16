"""Run the frozen v0.2 contract, JSON Schema and ledger validator in CI."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MODULES_BY_BATCH = {
    1: ["1.1"],
    2: ["2.1", "2.2", "2.3"],
    3: ["3.1", "3.2", "3.3"],
    4: ["4.1", "4.2", "4.3", "4.4", "4.5"],
    5: ["5.1", "5.2", "5.3", "5.4"],
    6: ["6.1", "6.2", "6.3"],
    7: ["7.1", "7.2", "7.3"],
    8: ["8.1"],
    9: ["9.1"],
}


def _validator_module():
    spec = importlib.util.spec_from_file_location(
        "openthu_v020_validate", ROOT / "contracts" / "v0.2" / "validate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _batches(statuses):
    return [
        {
            "batch": number,
            "status": status,
            "mode": "serial_gate" if number in (1, 8, 9) else "parallel_eligible",
            "modules": MODULES_BY_BATCH[number],
        }
        for number, status in enumerate(statuses, 1)
    ]


def _gate_errors(statuses):
    return _validator_module().batch_state_errors(_batches(statuses), {1, 8, 9})


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


def test_gate_accepts_monotonic_batch_prefix() -> None:
    statuses = ["accepted", "accepted", "ready"] + ["locked"] * 6
    assert _gate_errors(statuses) == []


def test_gate_rejects_two_ready_batches() -> None:
    statuses = ["accepted", "ready", "ready"] + ["locked"] * 6
    assert _gate_errors(statuses), "two ready batches must be rejected"


def test_gate_rejects_skipped_accepted_batch() -> None:
    statuses = ["accepted", "locked", "accepted", "ready"] + ["locked"] * 5
    assert _gate_errors(statuses), "an accepted batch after a locked gap must be rejected"


def test_gate_rejects_ready_after_unaccepted_predecessor() -> None:
    statuses = ["accepted", "locked", "ready"] + ["locked"] * 6
    assert _gate_errors(statuses), "ready after an unaccepted predecessor must be rejected"


def test_gate_rejects_accepted_after_ready() -> None:
    statuses = ["ready", "accepted"] + ["locked"] * 7
    assert _gate_errors(statuses), "accepted after ready must be rejected"
