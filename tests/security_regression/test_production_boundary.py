"""Production boundaries: no synthetic codec, no silent widening, safe CI.

A synthetic test codec is useful precisely because it cannot become a product
path.  These cases prove that the production entry points fail closed without a
real key, that disabling an optional feature leaves the frozen 29-tool catalog
and the original mock query working, and that the CI definition stays
reproducible and does not execute untrusted pull-request code with secrets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest

from thu_assistant import actions
from thu_assistant.action_store import ActionStore
from thu_assistant.bridge import CampusError
from thu_assistant.secure_store import SecureKeyNotConfigured, open_store_from_environment

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BASELINE = json.loads((ROOT / "contracts" / "v0.2" / "compatibility-baseline.json")
                      .read_text(encoding="utf-8"))


def test_production_entry_cannot_enable_the_synthetic_codec(tmp_path, monkeypatch):
    source_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (ROOT / "src").rglob("*.py"))
    for token in ("synthetic_store", "OPENTHU_SYNTHETIC", "THU_SYNTHETIC",
                  "ALLOW_SYNTHETIC"):
        assert token not in source_text, token
    monkeypatch.setenv("OPENTHU_ALLOW_SYNTHETIC", "1")
    monkeypatch.setenv("THU_ALLOW_SYNTHETIC", "1")
    with pytest.raises(SecureKeyNotConfigured):
        open_store_from_environment({})
    assert not (tmp_path / "plaintext.dpapi").exists()
    if os.name != "nt":
        with pytest.raises(CampusError, match="ACTION_LOG_ENCRYPT_FAILED"):
            ActionStore(tmp_path / "actions").write({"operation_id": "a" * 32,
                                                     "value": "synthetic"})
        assert not list((tmp_path / "actions").glob("*.dpapi"))


def test_disabling_optional_features_keeps_the_legacy_catalog_and_query(monkeypatch):
    for name in ("THU_LIBRARY_WRITE", "THU_LIBRARY_READ", "THU_SPACE_READ",
                 "THU_LIBRARY_RENEW", "THU_SPORTS_WRITE", "THU_SPORTS_READ",
                 "THU_LEARN_SUBMIT"):
        monkeypatch.setenv(name, "0")
    assert actions.action_enabled("library") is False
    assert actions.sports_enabled() is False
    from thu_assistant.submissions import enabled as submissions_enabled
    assert submissions_enabled() is False

    from thu_assistant import mcp_stdio
    names = [tool["name"] for tool in mcp_stdio.TOOLS]
    assert len(names) == BASELINE["mcp_tool_count"] == 29
    assert names == BASELINE["mcp_tools"]

    from learn_assistant.core import LearnAssistant
    from learn_assistant.providers.mock import MockProvider
    assistant = LearnAssistant(MockProvider())
    assert assistant.get_login_status()["status"]["authenticated"] is True
    assert assistant.list_courses()["count"] == 3
    assert assistant.list_assignments(course_id="course-geology")["count"] == 1


def test_optional_feature_flags_default_to_existing_behaviour(monkeypatch):
    for name in ("THU_LIBRARY_WRITE", "THU_LIBRARY_READ", "THU_SPACE_READ",
                 "THU_SPORTS_WRITE", "THU_SPORTS_READ"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("THU_LEARN_SUBMIT", raising=False)
    assert actions.action_enabled("library") is True
    assert actions.sports_enabled() is True
    from thu_assistant.submissions import enabled as submissions_enabled
    assert submissions_enabled() is False


def test_ci_workflow_is_reproducible_and_avoids_untrusted_pr_target():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    assert "permissions:\n  contents: read" in text
    assert "ubuntu-24.04" in text and "windows-2025" in text
    assert "python-version: ['3.12', '3.13']" in text
    assert "node-version: '24'" in text
    assert "timeout-minutes: 60" in text
    assert "timeout-minutes: 40" in text
    assert "python -m playwright install --with-deps chromium" in text
    assert "python -m playwright install chromium" in text
    assert "scripts/regression_report.py" in text
    assert "scripts/package_release.py" in text
    assert "${{ secrets." not in text
    assert "persist-credentials: false" in text
    action_lines = [line.strip() for line in text.splitlines() if "uses: actions/" in line]
    assert action_lines
    for line in action_lines:
        assert re.search(r"@[0-9a-f]{40}(\s|$)", line), line


def test_ci_uploads_only_safe_reports_not_raw_logs():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact" in text
    paths = re.findall(r"^\s*path:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    assert paths, "workflow has no upload path"
    for path in paths:
        assert path.startswith("artifacts/"), path
    assert "artifacts/regression-report-*.json" in text
    assert "artifacts/release-gate.json" in text
    assert "artifacts/junit.xml" not in paths
    assert ".local" not in " ".join(paths)


def test_ci_gate_runs_privacy_dependency_and_candidate_checks():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python scripts/dev.py check" in text
    assert "python scripts/package_release.py build" in text
    assert "release-gate" in text
    assert "--require-dist" in text
    assert "--require-python-matrix" in text
    assert "--markers tests/security_regression/marker_set.json" in text
    assert "download-artifact" in text


def test_workflow_job_names_are_platform_stable():
    text = WORKFLOW.read_text(encoding="utf-8")
    # The batch gate matches these two names exactly.
    assert "format('test ({0})', matrix.os)" in text
    assert "format('test ({0}, {1})', matrix.os, matrix.python-version)" in text
    assert re.search(r"name:\s+\$\{\{ matrix\.python-version", text)
