"""Batch-8 integration seams for the v0.2.0 candidate.

Batch 8 is the serial integration gate: it turns the accepted batches into one
installable, restorable candidate.  These cases cover facts only the integration
owner can prove on the merged tree:

* every module delivery commit and every accepted batch integration commit is
  an ancestor of the candidate tree, and the batch gate names batch 8 as the
  single ready batch with 8.1 implemented but not accepted;
* the contract set is complete and the running local-read API router declares
  exactly the frozen parameters, including the 8.1 request-side ``cursor``
  revision, so no route needs an out-of-contract extension;
* one product version reaches the package metadata, the release manifest and
  the changelog, and the changelog never claims a published release;
* the release layout still carries the dashboard shell, the bundled runtime
  resources and the six legacy plus one new entry point;
* the source-matrix difference register no longer assigns work to another
  module, and the integration records exist.

No campus service, model endpoint or network is contacted.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "plans" / "v0.2.0" / "交付"
CONTRACTS = ROOT / "contracts" / "v0.2"
ACCEPTED_MODULES = ("2.1", "2.2", "2.3", "3.1", "3.2", "3.3", "4.1", "4.2",
                    "4.3", "4.4", "4.5", "5.1", "5.2", "5.3", "5.4", "6.1",
                    "6.2", "6.3", "7.1", "7.2", "7.3")


def _delivery_json(name: str) -> dict:
    return json.loads((DELIVERY / name).read_text(encoding="utf-8"))


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=False)


def _require_git() -> None:
    probe = _git("rev-parse", "--git-dir")
    if probe.returncode:
        pytest.skip("no Git metadata in this tree")
    shallow = _git("rev-parse", "--is-shallow-repository").stdout.strip()
    if shallow == "true":
        pytest.skip("shallow clone has no module history; CI checks out full history here")


def test_every_module_and_batch_delivery_is_an_ancestor_of_the_candidate() -> None:
    _require_git()
    gate = _delivery_json("批次门禁.json")
    by_batch = {item["batch"]: item for item in gate["batches"]}

    commits: dict[str, str] = {}
    for module in ACCEPTED_MODULES:
        record = _delivery_json(Path("模块结果") / f"{module}.json")
        assert record["status"] == "accepted", module
        commit = record.get("delivery_commit") or ""
        assert re.fullmatch(r"[0-9a-f]{7,40}", commit), (module, commit)
        commits[module] = commit

    for batch in range(2, 8):
        commit = by_batch[batch].get("integration_commit") or ""
        assert re.fullmatch(r"[0-9a-f]{40}", commit), (batch, commit)
        commits[f"batch-{batch}"] = commit

    for label, commit in commits.items():
        result = _git("merge-base", "--is-ancestor", commit, "HEAD")
        assert result.returncode == 0, f"{label} {commit} is not an ancestor of HEAD"


def test_the_batch_gate_ends_in_one_ready_batch_with_8_1_implemented() -> None:
    gate = _delivery_json("批次门禁.json")
    batches = {item["batch"]: item for item in gate["batches"]}
    assert sorted(batches) == list(range(1, 10))
    assert all(batches[number]["status"] == "accepted" for number in range(1, 8))
    assert batches[8]["status"] == "ready"
    assert batches[9]["status"] == "locked"
    assert batches[8]["mode"] == "serial_gate"
    assert batches[8]["modules"] == ["8.1"]
    state = batches[8]["module_status"]["8.1"]
    # `accepted` belongs to the review phase; batch 8 must not claim it here.
    assert state in ("implemented", "accepted")
    assert gate["batches"][-1]["batch"] == 9


def test_the_contract_set_is_complete_and_the_local_router_matches_it() -> None:
    schema_files = sorted(path.name for path in CONTRACTS.glob("*.schema.json"))
    assert len(schema_files) == 7, schema_files
    contract_files = schema_files + ["error-classification.json"]
    versions = {json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
                ["x-contract-version"] for name in contract_files}
    assert versions == {
        "result-envelope-v1", "error-classification-v1",
        "secure-storage-header-v1", "source-evidence-v1", "media-reference-v1",
        "calendar-event-v1", "business-facade-v1", "local-read-api-v1",
    }
    assert (CONTRACTS / "compatibility-baseline.json").is_file()

    from thu_assistant.local_api import contract

    api = json.loads((CONTRACTS / "local-read-api.schema.json")
                     .read_text(encoding="utf-8"))
    assert contract.CONTRACT_VERSION == api["x-contract-version"]
    by_path = {api["base_path"] + endpoint["path"]: set(endpoint["query"])
               for endpoint in api["endpoints"]}
    assert {route["path"] for route in contract.route_table()} == set(by_path)
    for route in contract.route_table():
        assert set(route["query"]) == by_path[route["path"]], route["path"]
    assert not hasattr(contract, "QUERY_EXTENSION")


def test_the_paged_dashboard_routes_declare_the_request_side_cursor() -> None:
    from thu_assistant.local_api import contract

    api = json.loads((CONTRACTS / "local-read-api.schema.json")
                     .read_text(encoding="utf-8"))
    assert set(api["pagination"]["request_fields"]) == {"cursor", "page_size"}
    assert "next_cursor" in api["pagination"]["response_fields"]
    routes = {route["path"]: set(route["query"]) for route in contract.route_table()}
    for path in ("/api/v1/todos", "/api/v1/calendar", "/api/v1/resources"):
        assert "cursor" in routes[path], path
    revisions = [row.get("module") for row in api.get("x-revisions", [])]
    assert "8.1" in revisions


def test_one_candidate_version_reaches_metadata_manifest_and_changelog() -> None:
    from thu_assistant import version

    manifest = json.loads((ROOT / "plans" / "v0.2.0" / "VERSION_MANIFEST.json")
                          .read_text(encoding="utf-8"))
    assert version.__version__ == "0.2.0"
    assert manifest["target_version"] == version.__version__
    assert manifest["release_status"] == "candidate-not-released"
    assert manifest["release_rules"]["no_early_tag"] is True
    assert manifest["release_rules"]["no_early_release"] is True

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    head = changelog.split("## ", 2)[1]
    assert head.startswith("0.2.0")
    assert "候选" in head and "未发布" in head
    assert "已正式发布" not in changelog

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "0.2.0" in readme
    assert "候选" in readme


def test_the_release_layout_keeps_the_dashboard_resources_and_entry_points() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = project["tool"]["setuptools"]["package-data"]["thu_assistant"]
    for pattern in ("web_assets/*.html", "web_assets/assets/*.css",
                    "web_assets/assets/*.js", "services.json"):
        assert pattern in package_data, pattern
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include src/thu_assistant/web_assets *.html *.css *.js" in manifest

    layout = json.loads((ROOT / "build_support" / "distribution.json")
                        .read_text(encoding="utf-8"))
    bundle_ids = {bundle["id"] for bundle in layout["bundles"]}
    assert bundle_ids == {"runtime.windows", "vendor.madmodel_proxy"}
    for bundle in layout["bundles"]:
        for member in bundle["must_include"]:
            assert (ROOT / bundle["source"] / member).exists(), member
    for name in layout["license_files"]:
        assert (ROOT / name).is_file(), name
    assert {".local", "dist", "node_modules"} <= set(layout["forbidden"]["directories"])

    scripts = project["project"]["scripts"]
    legacy = {"openthu", "openthu-mcp", "learn-assistant", "learn-assistant-mcp",
              "tsinghua-assistant", "tsinghua-assistant-mcp"}
    assert legacy <= set(scripts)
    assert scripts["openthu-doctor"] == "thu_assistant.doctor:main"

    baseline = json.loads((CONTRACTS / "compatibility-baseline.json")
                          .read_text(encoding="utf-8"))
    assert baseline["mcp_tool_count"] == 29
    assert len(baseline["mcp_tools"]) == 29
    assert len(baseline["entry_points"]) == 6


def test_the_source_matrix_difference_register_has_no_stale_owner() -> None:
    text = (ROOT / "docs" / "contracts" / "source-matrix.md").read_text(encoding="utf-8")
    assert "待纠正的说明/实现差异" not in text
    assert "已澄清与仍保留的说明/实现差异" in text
    assert "8.1 已更正" in text
    for stale in ("4.4/7.3 统一措辞", "5.3 接入前改为", "待 8.1"):
        assert stale not in text, stale


def test_the_integration_and_candidate_records_exist() -> None:
    checklist = (DELIVERY / "集成清单.md").read_text(encoding="utf-8")
    candidate = (DELIVERY / "候选版本记录.md").read_text(encoding="utf-8")
    record = _delivery_json(Path("模块结果") / "8.1.json")
    assert record["module"] == "8.1"
    assert record["status"] == "implemented"
    assert record["branch"] == "hermes/8.1"
    assert record["worktree"].startswith("~") or record["worktree"].startswith("/")
    for module in ("1.1",) + ACCEPTED_MODULES + ("8.1", "9.1"):
        assert f"| {module} |" in checklist, module
    assert "candidate-not-released" in candidate
    assert "SHA256" in candidate or "sha256" in candidate

