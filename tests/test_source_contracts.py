"""Validate the v0.2 source register, domain contracts and synthetic fixtures."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "contracts" / "v0.2" / "sources" / "validate.py"


def run_validator() -> dict:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload["errors"]
    return payload


def test_source_contracts_validate_with_required_counts() -> None:
    payload = run_validator()
    assert payload["sources"] == 29
    assert payload["models"] == 10
    assert payload["tasks"] == 8
    assert payload["readiness"] == 14
    assert payload["fixtures"] == 7
    assert payload["schema_samples"] == 9
    assert payload["rejection_samples"] >= 21


def test_blocked_source_conclusions_are_recorded() -> None:
    readiness = json.loads(
        (ROOT / "contracts" / "v0.2" / "sources" / "adapter-readiness.json").read_text(encoding="utf-8")
    )
    by_module = {row["module"]: row for row in readiness["modules"]}
    for module in ("4.1", "4.5", "5.1", "5.3"):
        assert by_module[module]["start_conclusion"] == "BLOCKED_SOURCE_EVIDENCE"
        assert by_module[module]["blocked_scope"]


def test_required_synthetic_fixtures_exist_and_are_clean() -> None:
    fixtures = ROOT / "tests" / "fixtures" / "sources"
    names = {
        "login-200-no-business.json",
        "same-title-courses.json",
        "missing-stable-id.json",
        "empty-vs-next-page-vs-failure.json",
        "submission-states.json",
        "renewal-eligible-failed.json",
        "model-catalog-mismatch.json",
    }
    assert {path.name for path in fixtures.glob("*.json")} == names
    for path in fixtures.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["synthetic"] is True
