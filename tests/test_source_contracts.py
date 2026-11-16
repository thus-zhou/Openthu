"""Validate the v0.2 source register, domain contracts and synthetic fixtures."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "contracts" / "v0.2" / "sources" / "validate.py"


def load_validator_module():
    spec = importlib.util.spec_from_file_location("source_validator", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert payload["rejection_samples"] >= 24


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


def test_attachment_map_and_readiness_cover_every_source() -> None:
    registry = json.loads(
        (ROOT / "contracts" / "v0.2" / "sources" / "source-registry.json").read_text(encoding="utf-8")
    )
    by_id = {row["source_id"]: row for row in registry["sources"]}
    assert all(row.get("owner") for row in registry["sources"])
    files = by_id["learn.course_files"]
    fields = {
        mapping.get("canonical_field")
        for mapping in files["field_mappings"]
        if isinstance(mapping, dict)
    }
    assert {
        "attachment.source_record_id",
        "attachment.display_name",
        "attachment.attachment_uid",
        "attachment.download_state",
        "attachment.identity_state",
        "attachment.source_evidence_id",
    } <= fields
    readiness = json.loads(
        (ROOT / "contracts" / "v0.2" / "sources" / "adapter-readiness.json").read_text(encoding="utf-8")
    )
    by_module = {row["module"]: row for row in readiness["modules"]}
    assert "visitors.form" in by_module["4.4"]["source_ids"]
    referenced = {
        source
        for row in readiness["modules"]
        for source in row.get("source_ids", [])
    }
    assert set(by_id) <= referenced


def test_semantic_guards_cover_cross_field_states() -> None:
    module = load_validator_module()
    renewal = {
        "loan_uid": "library.loans:ORDER:SYNTHETIC-LOAN-1",
        "renewal_uid": "library.renewal:RENEWAL:library.loans:ORDER:SYNTHETIC-LOAN-1",
        "execution_state": "FAILED",
        "due_date_before": "2026-12-01T23:59:00+08:00",
        "due_date_after": "2026-12-05T23:59:00+08:00",
        "verified": False,
    }
    assert module.renewal_semantic_errors(renewal)
    submission = {
        "state": "SUBMITTED",
        "is_final": True,
        "result_observed": False,
        "remote_submission_id": None,
    }
    assert module.submission_semantic_errors(submission)
    image = {"capability_state": "AVAILABLE", "evidence_level": "OFFLINE_CONTRACT"}
    assert module.model_image_semantic_errors(image)
