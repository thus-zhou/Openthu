"""Verification report generator and release-gate behaviour (module 7.2).

The report is the machine-readable handoff for the integration batch: source
commit, contracts, platform and CI identity, scope, totals, skip reasons,
side-effect counters and module-attributed failures with a minimal reproduction.
These tests use synthetic JUnit XML, synthetic reports and synthetic archives;
they never contact a campus service or the internet.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "regression_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location("regression_report_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = load_report_module()
CONTRACT_DIGEST = REPORT.contract_set_digest(REPORT.contract_inventory())


def marker(kind: str) -> str:
    if kind == "path":
        return "/" + "home/" + "synthetic-release-user" + "/private/report.txt"
    if kind == "token":
        return "ghp_" + "Z" * 36
    if kind == "host":
        return ".".join(["10", "42", "0", "9"])
    return "SYNTHETIC-MARKER"


MARKERS = {"path": marker("path"), "token": marker("token"), "host": marker("host")}


def write_junit(path: Path, cases: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        attrs = (f'classname="{case.get("classname", "tests.test_local_api")}" '
                 f'name="{case["name"]}" file="{case.get("file", "tests/test_local_api.py")}" '
                 f'time="{case.get("time", 0.01)}"')
        body = ""
        if case.get("status") == "skipped":
            body = f'<skipped message="{case.get("reason", "")}"/>'
        elif case.get("status") == "error":
            body = (f'<error type="{case.get("type", "RuntimeError")}" '
                    f'message="{case.get("message", "RuntimeError: boom")}">'
                    f'{case.get("message", "")}</error>')
        elif case.get("status") == "failed":
            body = (f'<failure message="{case.get("message", "AssertionError: failed")}">'
                    f'{case.get("message", "")}</failure>')
        rows.append(f"<testcase {attrs}>{body}</testcase>")
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest">'
        + "".join(rows) + "</testsuite></testsuites>\n", encoding="utf-8")
    return path


def build(tmp_path: Path, cases: list[dict], *, platform: str = "linux",
          commit: str = "deadbeef", dev_results=None, enforce_floor: bool = False,
          floor=None, ci=None) -> dict:
    junit = write_junit(tmp_path / f"junit-{platform}.xml", cases)
    return REPORT.build_report(REPORT.parse_junit(junit), platform=platform, commit=commit,
                               dev_results=dev_results, enforce_floor=enforce_floor,
                               floor=floor, ci={} if ci is None else ci)


def passed(name="test_ok", file="tests/test_local_api.py"):
    return {"name": name, "file": file, "status": "passed"}


def failed(name="test_bad", file="tests/test_local_api.py", message="AssertionError: boom"):
    return {"name": name, "file": file, "status": "failed", "message": message}


def errored(name="test_error", file="tests/test_local_api.py"):
    return {"name": name, "file": file, "status": "error", "type": "RuntimeError",
            "message": "RuntimeError: setup failed"}


def skipped(name="test_skip", file="tests/test_local_api.py", reason="POSIX permission bits"):
    return {"name": name, "file": file, "status": "skipped", "reason": reason}


def report_doc(platform="linux", commit="abc", *, system=None, ci_runner=None,
               python="3.12", product="0.2.0", digest=CONTRACT_DIGEST, dirty=False,
               status="PASS", gaps=None, totals=None):
    ci_block = {"runner_os": ci_runner if ci_runner is not None else platform.title(),
                "runner_arch": "X64", "run_id": "1", "workflow": "Offline checks"}
    return {
        "schema": REPORT.REPORT_SCHEMA,
        "source": {"commit": commit, "branch": "hermes/7.2", "dirty": dirty},
        "product_version": product,
        "contracts": REPORT.contract_inventory(),
        "contract_set_sha256": digest,
        "platform": {"name": platform, "system": system or platform.title(),
                     "python": python, "machine": "x86_64", "node": "v24.0.0",
                     "ci": ci_block},
        "totals": totals or {"passed": 1700, "failed": 0, "skipped": 0,
                             "errors": 0, "total": 1700},
        "node": {"passed": 66, "failed": 0, "skipped": 0},
        "modules": [],
        "skip_reasons": [],
        "gaps": gaps or [],
        "failures": [],
        "side_effects": {"live_campus_calls": 0, "model_calls": 0,
                         "business_submissions": 0},
        "side_effect_source": "dev-results",
        "status": status,
    }


def write_reports(directory: Path, *, include_linux_313: bool = True, **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    rows = [("linux.json", "linux", {}), ("windows.json", "windows", {})]
    if include_linux_313:
        rows.append(("linux-313.json", "linux", {"python": "3.13"}))
    for name, platform, defaults in rows:
        options = dict(defaults)
        options.update(overrides.get(name, {}))
        REPORT.write_report(report_doc(platform, **options), directory / name)
    return directory


# ---------------------------------------------------------------------------
# Report fields and safe text
# ---------------------------------------------------------------------------

def test_report_includes_commit_contracts_platform_totals_and_side_effects(tmp_path):
    report = build(tmp_path, [passed(), passed("test_two"), skipped()])
    assert report["schema"] == REPORT.REPORT_SCHEMA
    assert report["source"]["commit"] == "deadbeef"
    assert report["platform"]["name"] == "linux"
    assert report["platform"]["system"] == "Linux"
    assert isinstance(report["platform"]["ci"], dict)
    assert report["platform"]["python"]
    assert report["product_version"] == "0.2.0"
    assert report["contracts"]["result-envelope.schema.json"] == "result-envelope-v1"
    assert report["contract_set_sha256"]
    assert report["totals"] == {"passed": 2, "failed": 0, "skipped": 1, "errors": 0,
                                "total": 3}
    assert report["skip_reasons"] == [
        {"reason": "POSIX permission bits", "count": 1, "class": "platform"}]
    assert report["side_effects"] == {
        "live_campus_calls": 0, "model_calls": 0, "business_submissions": 0}
    assert report["side_effect_source"] == "absent"
    assert report["status"] == "PASS"


def test_failure_message_is_reduced_to_a_class_and_repro(tmp_path):
    value = marker("path") + " " + marker("token") + " " + marker("host")
    report = build(tmp_path, [failed(message=f"AssertionError: {value}")])
    text = json.dumps(report, ensure_ascii=False)
    assert value not in text
    assert marker("path") not in text
    assert marker("token") not in text
    assert report["status"] == "FAIL"
    failure = report["failures"][0]
    assert failure["nodeid"] == "tests/test_local_api.py::test_bad"
    assert failure["module"] == "6.2"
    assert failure["error_class"] == "AssertionError"
    assert failure["minimal_repro"].startswith("python -m pytest")
    assert report["modules"][0]["id"] == "6.2"


def test_parameterised_nodeids_and_absolute_files_are_sanitised(tmp_path):
    junit = write_junit(tmp_path / "leak.xml", [{
        "name": "test_reads[" + "/" + "home/alice/private/token.txt]",
        "file": "/Users/alice/project/tests/test_local_api.py",
        "status": "failed",
    }])
    cases = REPORT.parse_junit(junit)
    assert cases[0]["file"] == "tests/unknown.py"
    assert "/home/alice" not in cases[0]["nodeid"]
    assert "/Users/alice" not in json.dumps(cases)
    assert "[REDACTED]" in cases[0]["name"]
    report = REPORT.build_report(cases, platform="linux", commit="deadbeef",
                                 enforce_floor=False, ci={})
    assert "/home/alice" not in json.dumps(report, ensure_ascii=False)
    assert "/Users/alice" not in json.dumps(report, ensure_ascii=False)


def test_explicit_skip_registry_does_not_guess_from_substrings():
    assert REPORT.classify_skip("Windows DPAPI only") == "platform"
    assert REPORT.classify_skip("macOS only behaviour") == "platform"
    assert REPORT.classify_skip("we will look at this on another platform later") == "unexplained"
    assert REPORT.classify_skip("Pillow build cannot write animated WebP") == "environment"
    assert REPORT.classify_skip("node is unavailable") == "environment"
    assert REPORT.classify_skip("platform: custom reason") == "platform"
    assert REPORT.classify_skip("env: missing tool") == "environment"


def test_unexplained_skip_fails_and_environment_gap_is_incomplete(tmp_path):
    unexplained = build(tmp_path / "u", [skipped(reason="some new mystery")])
    assert unexplained["status"] == "FAIL"
    assert unexplained["gaps"][0]["kind"] == "unexplained_skip"
    environment = build(tmp_path / "e", [skipped(reason="Node.js is not installed")])
    assert environment["status"] == "INCOMPLETE"
    assert environment["gaps"][0]["kind"] == "environment_skip"
    platform_only = build(tmp_path / "p", [skipped(reason="Windows DPAPI only")])
    assert platform_only["status"] == "PASS"


def test_errors_are_counted_separately_from_failures(tmp_path):
    report = build(tmp_path, [errored(), failed()])
    assert report["totals"]["errors"] == 1
    assert report["totals"]["failed"] == 1
    assert report["totals"]["total"] == 2
    assert report["status"] == "FAIL"
    assert report["modules"][0]["errors"] == 1
    assert report["modules"][0]["failed"] == 1


def test_module_attribution_and_unmapped_failure(tmp_path):
    report = build(tmp_path, [
        failed(name="test_a", file="tests/test_secure_store.py"),
        failed(name="test_b", file="tests/release_regression/test_report.py"),
        failed(name="test_c", file="tests/test_unknown_new.py"),
    ])
    modules = {row["id"]: row for row in report["modules"]}
    assert modules["2.2"]["failed"] == 1
    assert modules["7.2"]["failed"] == 1
    assert modules["unmapped"]["failed"] == 1
    assert any(gap["kind"] == "unmapped_failure" for gap in report["gaps"])


def test_node_results_are_added_to_the_gateway_bucket(tmp_path):
    dev = tmp_path / "dev.json"
    dev.write_text(json.dumps({"node_pass": 66, "node_fail": 0, "node_skipped": 0,
                               "live_campus_calls": 0, "model_calls": 0,
                               "business_submissions": 0}), encoding="utf-8")
    report = build(tmp_path, [passed(), passed("test_two")], dev_results=dev)
    assert report["totals"]["passed"] == 68
    assert sum(row["passed"] for row in report["modules"]) == 68
    gateway = {row["id"]: row for row in report["modules"]}["3.1"]
    assert gateway["node"] == {"passed": 66, "failed": 0, "skipped": 0}
    assert gateway["passed"] == 66


def test_side_effect_source_and_invalid_values(tmp_path):
    dev = tmp_path / "dev.json"
    dev.write_text(json.dumps({"node_pass": 1, "node_fail": 0, "node_skipped": 0,
                               "live_campus_calls": True}), encoding="utf-8")
    report = build(tmp_path, [passed()], dev_results=dev)
    assert report["side_effect_source"] == "dev-results"
    assert any(gap["kind"] == "side_effect_value_invalid" for gap in report["gaps"])
    dev.write_text(json.dumps({"node_pass": 1, "node_fail": 0, "node_skipped": 0,
                               "business_submissions": 2}), encoding="utf-8")
    positive = build(tmp_path / "positive", [passed()], dev_results=dev)
    assert any(item["error_class"] == "UnexpectedSideEffect" for item in positive["failures"])


# ---------------------------------------------------------------------------
# Evidence floor
# ---------------------------------------------------------------------------

def floor(**overrides):
    values = {"min_total": 1600, "max_skip_ratio": 0.05, "require_node": False,
              "require_dev_results": False, "require_coverage": False}
    values.update(overrides)
    return values


def test_empty_one_case_and_all_skipped_suites_do_not_pass(tmp_path):
    empty = build(tmp_path / "empty", [], enforce_floor=True, floor=floor())
    assert empty["status"] == "FAIL"
    assert any(gap["kind"] == "evidence_empty" for gap in empty["gaps"])
    one = build(tmp_path / "one", [passed()], enforce_floor=True, floor=floor())
    assert one["status"] == "FAIL"
    assert any(gap["kind"] == "evidence_below_floor" for gap in one["gaps"])
    all_skipped = build(tmp_path / "skip", [skipped()], enforce_floor=True, floor=floor())
    assert all_skipped["status"] == "FAIL"
    assert any(gap["kind"] == "skip_ratio_exceeded" for gap in all_skipped["gaps"])


def test_missing_floor_is_a_failure(tmp_path):
    report = build(tmp_path, [passed()], enforce_floor=True, floor={})
    assert report["status"] == "FAIL"
    assert any(gap["kind"] == "evidence_floor_missing" for gap in report["gaps"])


def test_node_and_dev_results_are_required_by_the_floor(tmp_path):
    node_missing = build(tmp_path / "node", [passed()], enforce_floor=True,
                         floor=floor(min_total=1, require_node=True))
    assert any(gap["kind"] == "node_evidence_missing" for gap in node_missing["gaps"])
    dev_missing = build(tmp_path / "dev", [passed()], enforce_floor=True,
                        floor=floor(min_total=1, require_dev_results=True))
    assert any(gap["kind"] == "side_effect_evidence_missing" for gap in dev_missing["gaps"])


def test_coverage_matrix_files_are_required(tmp_path):
    report = build(tmp_path, [passed()], enforce_floor=True,
                   floor=floor(min_total=1, require_coverage=True))
    assert any(gap["kind"] == "test_file_coverage_incomplete" for gap in report["gaps"])


# ---------------------------------------------------------------------------
# Aggregation identity and binding
# ---------------------------------------------------------------------------

def test_aggregate_requires_both_platforms_and_one_commit():
    reports = [report_doc("linux", "abc"), report_doc("windows", "abc")]
    value = REPORT.aggregate_reports(reports)
    assert value["status"] == "PASS"
    assert value["source_commit"] == "abc"
    missing = REPORT.aggregate_reports(reports[:1])
    assert missing["status"] == "FAIL"
    assert missing["missing_platforms"] == ["windows"]
    mismatch = REPORT.aggregate_reports([report_doc("linux", "abc"),
                                         report_doc("windows", "def")])
    assert mismatch["status"] == "FAIL"
    assert any(gap["kind"] == "report_commit_mismatch" for gap in mismatch["gaps"])


def test_shape_report_is_rejected_by_default():
    reports = [report_doc("linux", "abc"),
               report_doc("windows", "abc", system="Linux", ci_runner="")]
    value = REPORT.aggregate_reports(reports)
    assert value["status"] == "FAIL"
    kinds = {gap["kind"] for gap in value["gaps"]}
    assert "platform_identity_mismatch" in kinds
    assert "platform_identity_unverified" in kinds


def test_ci_runner_mismatch_is_rejected():
    reports = [report_doc("linux", "abc"),
               report_doc("windows", "abc", ci_runner="Linux")]
    value = REPORT.aggregate_reports(reports)
    assert value["status"] == "FAIL"
    assert any(gap["kind"] == "ci_platform_identity_mismatch" for gap in value["gaps"])


def test_shape_reports_can_only_be_allowed_for_diagnostics():
    reports = [report_doc("linux", "abc"),
               report_doc("windows", "abc", system="Linux", ci_runner="")]
    value = REPORT.aggregate_reports(reports, allow_shape_reports=True)
    assert value["status"] == "PASS"
    assert value["allow_shape_reports"] is True


def test_evidence_must_match_the_expected_commit_product_and_contracts():
    reports = [report_doc("linux", "abc"), report_doc("windows", "abc")]
    drifted = REPORT.aggregate_reports(reports, expected_commit="head")
    assert drifted["status"] == "FAIL"
    assert any(gap["kind"] == "report_commit_mismatch" for gap in drifted["gaps"])
    product = REPORT.aggregate_reports(reports, expected_product_version="9.9.9")
    assert product["status"] == "FAIL"
    assert any(gap["kind"] == "report_product_mismatch" for gap in product["gaps"])
    contracts = REPORT.aggregate_reports(reports, expected_contracts_digest="other")
    assert contracts["status"] == "FAIL"
    assert any(gap["kind"] == "report_contracts_mismatch" for gap in contracts["gaps"])


def test_dirty_and_commitless_reports_are_rejected():
    dirty = REPORT.aggregate_reports([report_doc("linux", "abc", dirty=True),
                                      report_doc("windows", "abc")])
    assert dirty["status"] == "FAIL"
    assert any(gap["kind"] == "report_worktree_dirty" for gap in dirty["gaps"])
    missing = REPORT.aggregate_reports([report_doc("linux", None),
                                        report_doc("windows", None)])
    assert missing["status"] == "FAIL"
    assert any(gap["kind"] == "report_commit_missing" for gap in missing["gaps"])


def test_required_python_matrix_is_checked():
    reports = [report_doc("linux", "abc", python="3.12"),
               report_doc("windows", "abc", python="3.12")]
    value = REPORT.aggregate_reports(reports, required_python={
        "linux": ["3.12", "3.13"], "windows": ["3.12"]})
    assert value["status"] == "FAIL"
    assert any(gap["kind"] == "python_matrix_incomplete" for gap in value["gaps"])


def test_unreadable_reports_are_not_silently_dropped(tmp_path):
    directory = tmp_path / "reports"
    directory.mkdir()
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    (directory / "linux.json").write_text(json.dumps(report_doc("linux", "abc")),
                                          encoding="utf-8")
    reports, errors = REPORT.load_reports_detail(directory)
    assert len(reports) == 1
    assert errors[0]["rule"] == "unreadable"
    value = REPORT.aggregate_reports(reports, report_errors=errors)
    assert value["status"] == "FAIL"
    assert any(gap["kind"] == "report_unreadable" for gap in value["gaps"])


def test_duplicate_gaps_are_deduped_with_a_count():
    gap = {"kind": "environment_skip", "severity": "incomplete", "reason": "node"}
    reports = [report_doc("linux", "abc", gaps=[gap]),
               report_doc("windows", "abc", gaps=[gap])]
    value = REPORT.aggregate_reports(reports)
    row = next(item for item in value["gaps"] if item["kind"] == "environment_skip")
    assert row["occurrences"] == 2


# ---------------------------------------------------------------------------
# Marker and archive gate
# ---------------------------------------------------------------------------

def test_marker_scan_rejects_an_archive_with_a_private_path_and_token(tmp_path):
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("notes.txt", f"see {marker('path')} with {marker('token')}\n")
        bundle.writestr("session.json", "{}")
        bundle.writestr("host.txt", marker("host"))
    result = REPORT.marker_scan_archives([archive], MARKERS)
    text = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    rules = {row["rule"] for row in result["findings"]}
    assert {"marker:path", "marker:token", "marker:host", "credential-file"} <= rules
    assert marker("path") not in text
    assert marker("token") not in text
    assert marker("host") not in text
    assert result["archives"][0]["name"] == archive.name


def test_marker_scan_skips_only_the_marker_definition_member(tmp_path):
    archive = tmp_path / "openthu-0.2.0.tar.gz"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("openthu-0.2.0/tests/security_regression/marker_set.json",
                        marker("path"))
        bundle.writestr("openthu-0.2.0/notes.txt", marker("path"))
    result = REPORT.marker_scan_archives([archive], MARKERS)
    assert result["ok"] is False
    files = {row["file"] for row in result["findings"]}
    assert any(name.endswith("notes.txt") for name in files)
    assert not any("marker_set.json" in name for name in files)


def test_marker_scan_accepts_a_clean_public_report(tmp_path):
    archive = tmp_path / "clean.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("report.json", json.dumps({"status": "PASS", "items": []}))
    assert REPORT.marker_scan_archives([archive], MARKERS)["ok"] is True


def test_marker_scan_without_markers_does_not_fail_open(tmp_path):
    archive = tmp_path / "clean.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("report.json", "{}")
    result = REPORT.marker_scan_archives([archive], {})
    assert result["ok"] is False
    assert result["state"] == "NO_MARKERS"
    assert result["marker_count"] == 0


def test_load_markers_returns_a_stable_error_instead_of_failing_open(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(MARKERS), encoding="utf-8")
    assert REPORT.load_markers(valid, with_error=True) == (MARKERS, None)
    assert REPORT.load_markers(tmp_path / "missing.json", with_error=True)[1] == "MARKER_SET_MISSING"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert REPORT.load_markers(bad, with_error=True)[1] == "MARKER_SET_UNREADABLE"
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert REPORT.load_markers(empty, with_error=True)[1] == "MARKER_SET_EMPTY"
    assert REPORT.load_markers(None, with_error=True)[1] == "MARKER_SET_REQUIRED"


def test_posix_privacy_rule_flags_a_real_user_path_and_allows_synthetic_ones():
    scanner = REPORT._load_script("privacy_scan")
    real = scanner.findings("notes.txt", ("/" + "home/ubuntu/private/notes.txt").encode())
    assert any(row["rule"] == "posix-user-path" for row in real)
    synthetic = scanner.findings("notes.txt", ("/" + "home/synthetic/private/notes.txt").encode())
    assert not any(row["rule"] == "posix-user-path" for row in synthetic)
    route = scanner.findings("spaces.py", b"'/home/web/seat/area/1'")
    assert not any(row["rule"] == "posix-user-path" for row in route)


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------

def test_release_gate_is_incomplete_without_a_candidate_and_fails_when_required(tmp_path):
    reports = write_reports(tmp_path / "reports")
    value = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                require_dist=False, markers=MARKERS,
                                marker_error=None, expected_commit="abc")
    assert value["schema"] == REPORT.RELEASE_SCHEMA
    assert value["checks"]["reports"]["status"] == "PASS"
    assert value["checks"]["candidate"]["state"] == "NOT_BUILT"
    assert value["status"] == "INCOMPLETE"
    strict = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                  require_dist=True, markers=MARKERS,
                                  marker_error=None, expected_commit="abc")
    assert strict["status"] == "FAIL"


def test_release_gate_requires_the_python_matrix_by_default(tmp_path):
    reports = write_reports(tmp_path / "reports", include_linux_313=False)
    value = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                require_dist=False, markers=MARKERS,
                                marker_error=None, expected_commit="abc")
    assert value["checks"]["reports"]["status"] == "FAIL"
    assert any(gap["kind"] == "python_matrix_incomplete" for gap in value["gaps"])
    assert value["status"] == "FAIL"


def test_release_gate_rejects_evidence_from_a_foreign_commit(tmp_path):
    reports = write_reports(tmp_path / "reports", linux={"commit": "abc"},
                            windows={"commit": "abc"})
    value = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                require_dist=False, markers=MARKERS,
                                marker_error=None, expected_commit="head")
    assert value["checks"]["reports"]["ok"] is False
    assert value["checks"]["reports"]["status"] == "FAIL"
    assert any(gap["kind"] == "report_commit_mismatch" for gap in value["gaps"])
    assert value["status"] == "FAIL"


def test_release_gate_requires_test_evidence(tmp_path):
    value = REPORT.release_gate(dist=tmp_path / "missing")
    assert value["checks"]["reports"]["status"] == "MISSING"
    assert any(gap["kind"] == "test_evidence_missing" for gap in value["gaps"])
    assert value["status"] == "FAIL"


def test_release_gate_missing_marker_set_does_not_fail_open(tmp_path):
    reports = write_reports(tmp_path / "reports")
    markers, error = REPORT.load_markers(tmp_path / "missing.json", with_error=True)
    value = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                require_dist=True, markers=markers,
                                marker_error=error, expected_commit="abc")
    assert value["checks"]["marker_set"]["ok"] is False
    assert value["checks"]["marker_set"]["state"] == "MARKER_SET_MISSING"
    assert value["status"] == "FAIL"


def test_release_gate_rejects_a_leaking_wheel(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "openthu-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as bundle:
        bundle.writestr("leak.txt", MARKERS["path"])
    value = REPORT.release_gate(dist=dist, markers=MARKERS, require_dist=True)
    assert value["checks"]["marker_archives"]["ok"] is False
    assert any(row["rule"] == "marker:path"
               for row in value["checks"]["marker_archives"]["findings"])
    assert value["status"] == "FAIL"
    assert marker("path") not in json.dumps(value, ensure_ascii=False)


def test_candidate_check_reports_not_built_and_a_stable_verify_code(tmp_path):
    missing = REPORT.candidate_check(tmp_path / "missing")
    assert missing == {"ok": False, "state": "NOT_BUILT", "artifacts": []}
    empty = tmp_path / "empty"
    empty.mkdir()
    value = REPORT.candidate_check(empty)
    assert value["state"] == "VERIFY_FAILED"
    assert value["error_class"] == "RELEASE_MANIFEST_MISSING"
    assert value["artifacts"] == []


def test_dependency_and_defaults_audits_use_the_distribution_manifest():
    dependencies = REPORT.dependency_audit()
    assert dependencies["ok"] is True, dependencies["issues"]
    assert dependencies["source"] == "build_support/distribution.json"
    assert "cryptography" in dependencies["runtime"]
    assert dependencies["node_locked"]
    defaults = REPORT.defaults_audit(platform_name="posix")
    assert defaults["ok"] is True, defaults["issues"]
    assert defaults["details"]["listen_host"] == "127.0.0.1"
    assert defaults["details"]["secrets"] == "environment-only"


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------

def test_cli_report_diagnostic_writes_a_safe_json_file(tmp_path, capsys):
    junit = write_junit(tmp_path / "cli.xml", [passed()])
    output = tmp_path / "report.json"
    exit_code = REPORT.main(["report", "--junit", str(junit), "--platform", "linux",
                             "--commit", "deadbeef", "--diagnostic",
                             "--output", str(output)])
    assert exit_code == 0
    assert output.is_file()
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["schema"] == REPORT.REPORT_SCHEMA
    assert value["source"]["commit"] == "deadbeef"
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["status"] == "PASS"
    assert summary["totals"]["passed"] == 1


def test_cli_report_without_diagnostic_does_not_hide_a_one_case_suite(tmp_path):
    junit = write_junit(tmp_path / "cli.xml", [passed()])
    exit_code = REPORT.main(["report", "--junit", str(junit), "--platform", "linux",
                             "--commit", "deadbeef",
                             "--output", str(tmp_path / "report.json")])
    assert exit_code == 1


def test_cli_gate_returns_non_zero_for_a_failure(tmp_path):
    junit = write_junit(tmp_path / "gate.xml", [failed()])
    output = tmp_path / "gate.json"
    exit_code = REPORT.main(["gate", "--junit", str(junit), "--platform", "linux",
                             "--commit", "deadbeef", "--output", str(output)])
    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "FAIL"
