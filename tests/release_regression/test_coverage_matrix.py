"""Feature-to-case coverage matrix and the v0.1 compatibility baseline.

The matrix is a record of which executable case proves each module's claimed
behaviour, not a quality score.  This test keeps it honest:

* every planned module is present exactly once;
* every ``covered`` module names real test files that contain real tests;
* every referenced ``file::test`` node exists (no copied-away case);
* every 7.2 acceptance criterion maps to an executable case;
* no v0.1 test file was deleted and the 29-tool / legacy-query baseline still
  works, so a new optional feature cannot silently replace the old surface.
"""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
MATRIX = json.loads((TESTS / "release_regression" / "coverage_matrix.json")
                    .read_text(encoding="utf-8"))
PLAN_MODULES = ROOT / "plans" / "v0.2.0" / "模块"
BASELINE = json.loads((ROOT / "contracts" / "v0.2" / "compatibility-baseline.json")
                      .read_text(encoding="utf-8"))
REQUIRED_ACCEPTANCE = {
    "secret_marker_gate",
    "synthetic_codec_refused",
    "lost_upload_not_replayed",
    "cursor_scope_refused",
    "last_valid_record_recoverable",
    "missing_pages_never_mass_delete",
    "clean_ci_start",
    "optional_feature_off_keeps_legacy",
}


def _test_index() -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        index[relative] = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
    return index


def _node_exists(index: dict[str, set[str]], case: str) -> bool:
    file_name, _, test_name = case.partition("::")
    return test_name in index.get(file_name, set())


def test_matrix_covers_every_planned_module_once():
    planned = {match.group(1) for path in PLAN_MODULES.glob("*.md")
               for match in [re.match(r"^(\d+\.\d+)-", path.name)] if match}
    listed = [row["id"] for row in MATRIX["modules"]]
    assert len(listed) == len(set(listed))
    assert set(listed) == planned
    assert len(planned) == 24


def test_every_covered_module_has_executable_test_files():
    index = _test_index()
    for row in MATRIX["modules"]:
        if row["state"] != "covered":
            continue
        assert row["test_files"], row["id"]
        for file_name in row["test_files"]:
            assert file_name in index, (row["id"], file_name)
            assert index[file_name], (row["id"], file_name)


def test_every_referenced_feature_case_exists():
    index = _test_index()
    for row in MATRIX["modules"]:
        for feature in row["features"]:
            assert _node_exists(index, feature["case"]), (row["id"], feature["case"])
    for name, criterion in MATRIX["acceptance"].items():
        assert criterion["cases"], name
        for case in criterion["cases"]:
            assert _node_exists(index, case), (name, case)


def test_only_the_same_batch_dashboard_module_is_pending():
    pending = {row["id"] for row in MATRIX["modules"] if row["state"] == "pending_same_batch"}
    assert pending == {"7.1"}
    documentation = {row["id"] for row in MATRIX["modules"] if row["state"] == "documentation_only"}
    future = {row["id"] for row in MATRIX["modules"] if row["state"] == "future_gate"}
    assert documentation == {"7.3"}
    assert future == {"8.1", "9.1"}
    assert {row["id"] for row in MATRIX["modules"] if row["state"] == "covered"} == (
        set(row["id"] for row in MATRIX["modules"]) - pending - documentation - future)


def test_the_eight_release_acceptance_criteria_map_to_cases():
    assert set(MATRIX["acceptance"]) == REQUIRED_ACCEPTANCE
    for name, criterion in MATRIX["acceptance"].items():
        assert criterion["criterion"].strip(), name


def test_no_legacy_test_file_was_deleted():
    index = _test_index()
    assert len(MATRIX["legacy_test_files"]) >= 45
    for file_name in MATRIX["legacy_test_files"]:
        assert file_name in index, file_name
        assert index[file_name], file_name


def test_v01_tool_catalog_and_entry_points_are_intact():
    from thu_assistant import mcp_stdio

    names = [tool["name"] for tool in mcp_stdio.TOOLS]
    assert len(names) == BASELINE["mcp_tool_count"] == 29
    assert names == BASELINE["mcp_tools"]
    assert len(set(names)) == len(names)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    for entry in BASELINE["entry_points"]:
        assert entry in scripts, entry
    for name in BASELINE["import_paths"]:
        assert importlib.import_module(name) is not None, name


def test_v01_primary_query_still_works_without_optional_features():
    from learn_assistant.core import LearnAssistant
    from learn_assistant.providers.mock import MockProvider

    assistant = LearnAssistant(MockProvider())
    assert assistant.get_login_status()["status"]["authenticated"] is True
    assert assistant.list_courses()["count"] == 3
    assert assistant.list_assignments(course_id="course-geology")["count"] == 1
    files = assistant.list_course_files(course_id="course-geology")
    assert files["count"] == 2
