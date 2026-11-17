"""Batch-7 integration seams.

Each module proves itself in isolation.  These cases cover the seams only the
batch integration owner can prove after 7.1, 7.2 and 7.3 are reconnected to one
tree:

* the dashboard shell that 7.1 ships is actually reachable from the CLI (6.1)
  and included in the wheel/sdist package data (6.3);
* the dashboard reads only the frozen ``local-read-api-v1`` routes that 6.2
  serves, so the static shell and the local API cannot drift apart;
* the 7.2 release coverage matrix no longer reports the same-batch dashboard as
  pending, and every referenced case exists in the merged tree;
* the batch-7 module records, the ownership table and the batch gate agree, and
  an accepted gate names a full integration commit while batch 8 is the single
  ready batch;
* the 7.3 user-facing documents describe the wired dashboard instead of a
  pending 8.1 task, and keep the candidate-not-released status.

No test contacts a campus service, a model endpoint or the internet.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "plans" / "v0.2.0" / "交付"
BATCH7 = ("7.1", "7.2", "7.3")
ASSETS = ROOT / "src" / "thu_assistant" / "web_assets"


def _delivery_json(name: str) -> dict:
    return json.loads((DELIVERY / name).read_text(encoding="utf-8"))


def test_the_dashboard_shell_is_reachable_from_the_cli_and_packaged():
    entry = (ROOT / "src" / "thu_assistant" / "__main__.py").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = project["tool"]["setuptools"]["package-data"]["thu_assistant"]
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert (ASSETS / "index.html").is_file()
    assert (ASSETS / "assets" / "app.js").is_file()
    assert (ASSETS / "assets" / "app.css").is_file()
    # Both dashboard construction paths (`--check` and serve) hand the shell over.
    assert entry.count("static_dir=web_assets.static_dir()") == 2
    for pattern in ("web_assets/*.html", "web_assets/assets/*.css",
                    "web_assets/assets/*.js"):
        assert pattern in package_data, pattern
    assert "recursive-include src/thu_assistant/web_assets *.html *.css *.js" in manifest


def test_the_dashboard_uses_only_the_frozen_local_read_routes():
    from thu_assistant.local_api.contract import ROUTES

    model = (ASSETS / "assets" / "model.js").read_text(encoding="utf-8")
    api = (ASSETS / "assets" / "api.js").read_text(encoding="utf-8")

    block = re.search(r"const API = \{(.*?)\};", model, re.S)
    assert block, "model.js must declare the API route table"
    declared = set(re.findall(r"API_BASE \+ '(/[^']*)'", block.group(1)))
    # `/services/` and `/query/` are prefix templates completed at call time;
    # every other dashboard route must match a frozen template exactly.
    assert declared == {"/health", "/services", "/services/", "/query/",
                        "/todos", "/calendar", "/resources"}
    served = {route.template for route in ROUTES if "{" not in route.template}
    assert declared - {"/services/", "/query/"} == served
    assert {route.template for route in ROUTES if "{" in route.template} == {
        "/services/{service_id}", "/query/{service_id}/{operation}"}
    # The one network exit stays a same-origin, GET-only reader.
    assert "credentials: 'same-origin'" in api
    assert "redirect: 'error'" in api
    assert not re.search(r"method:\s*'(POST|PUT|PATCH|DELETE)'", api)


def test_the_coverage_matrix_no_longer_reports_the_dashboard_as_pending():
    matrix = json.loads(
        (ROOT / "tests" / "release_regression" / "coverage_matrix.json")
        .read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in matrix["modules"]}
    assert rows["7.1"]["state"] == "covered"
    assert rows["7.1"]["features"], "a covered module must map features to cases"
    assert "tests/test_web_assets.py" in rows["7.1"]["test_files"]
    assert not [row["id"] for row in matrix["modules"]
                if row["state"] == "pending_same_batch"]
    for file_name in rows["7.1"]["test_files"]:
        assert (ROOT / file_name).is_file(), file_name


def test_batch7_records_ownership_and_gate_agree():
    ownership = _delivery_json("模块负责表.json")
    rows = {row["id"]: row for row in ownership["modules"] if row["id"] in BATCH7}
    assert set(rows) == set(BATCH7)

    gate = _delivery_json("批次门禁.json")
    by_batch = {item["batch"]: item for item in gate["batches"]}
    batch = by_batch[7]

    for module in BATCH7:
        record = _delivery_json(Path("模块结果") / f"{module}.json")
        assert record["status"] == "accepted", module
        assert record["batch"] == 7, module
        assert rows[module]["status"] == "accepted", module
        assert record["delivery_commit"] == rows[module]["delivery_commit"], module
        assert rows[module]["accepted_at"], module

    assert batch["status"] in ("ready", "accepted")
    if batch["status"] == "accepted":
        assert all(state == "accepted" for state in batch["module_status"].values())
        assert re.fullmatch(r"[0-9a-f]{40}", batch["integration_commit"] or "")
        assert batch["accepted_at"]
        assert by_batch[8]["status"] in ("ready", "locked")
        if by_batch[8]["status"] == "ready":
            assert by_batch[9]["status"] == "locked"


def test_the_user_documents_describe_the_wired_dashboard_and_candidate_status():
    guide = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    capabilities = (ROOT / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    for text in (guide, capabilities):
        assert "随包安装" in text
        assert "LOCAL_API_UI_NOT_INSTALLED" in text  # documented failure mode
        assert "待 8.1 接线" not in text
        assert "还需要 8.1" not in text
        assert "接线完成前" not in text
    assert "候选" in changelog
    assert "已正式发布" not in changelog
