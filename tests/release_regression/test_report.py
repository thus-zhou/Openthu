"""Verification report generator and release-gate behaviour (module 7.2).

The report is the machine-readable handoff for the integration batch: source
commit, contracts, platform, scope, totals, skip reasons, side-effect counters
and module-attributed failures with a minimal reproduction.  These tests use
synthetic JUnit XML and synthetic archives, and assert that raw marker values
never appear in a public report.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "regression_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location("regression_report_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = load_report_module()


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
        elif case.get("status") == "failed":
            body = (f'<failure message="{case.get("message", "AssertionError: failed")}">'
                    f'{case.get("message", "")}</failure>')
        rows.append(f"<testcase {attrs}>{body}</testcase>")
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="pytest">'
        + "".join(rows) + "</testsuite></testsuites>\n", encoding="utf-8")
    return path


def build(tmp_path: Path, cases: list[dict], *, platform: str = "linux",
          commit: str = "deadbeef") -> dict:
    junit = write_junit(tmp_path / f"junit-{platform}.xml", cases)
    return REPORT.build_report(REPORT.parse_junit(junit), platform=platform, commit=commit)


def passed(name="test_ok", file="tests/test_local_api.py"):
    return {"name": name, "file": file, "status": "passed"}


def failed(name="test_bad", file="tests/test_local_api.py", message="AssertionError: boom"):
    return {"name": name, "file": file, "status": "failed", "message": message}


def skipped(name="test_skip", file="tests/test_local_api.py", reason="POSIX permission bits"):
    return {"name": name, "file": file, "status": "skipped", "reason": reason}


def test_report_includes_commit_contracts_platform_totals_and_side_effects(tmp_path):
    report = build(tmp_path, [passed(), passed("test_two"), skipped()])
    assert report["schema"] == REPORT.REPORT_SCHEMA
    assert report["source"]["commit"] == "deadbeef"
    assert report["platform"]["name"] == "linux"
    assert report["platform"]["python"]
    assert report["product_version"] == "0.2.0"
    assert report["contracts"]["result-envelope.schema.json"] == "result-envelope-v1"
    assert report["contract_set_sha256"]
    assert report["totals"] == {"passed": 2, "failed": 0, "skipped": 1, "errors": 0,
                                "total": 3}
    assert report["skip_reasons"] == [
        {"reason": "POSIX permission bits", "count": 1, "class": "platform"}]
    assert report["side_effects"] == {
        "live_campus_calls": 0, "model_calls": 0, "business_submissions": 0,
        "browser_launches": 0, "message_sends": 0, "background_processes": 0}
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


def test_unexplained_skip_fails_and_environment_gap_is_incomplete(tmp_path):
    unexplained = build(tmp_path / "u", [skipped(reason="some new mystery")])
    assert unexplained["status"] == "FAIL"
    assert unexplained["gaps"][0]["kind"] == "unexplained_skip"
    environment = build(tmp_path / "e", [skipped(reason="Node.js is not installed")])
    assert environment["status"] == "INCOMPLETE"
    assert environment["gaps"][0]["kind"] == "environment_skip"
    platform_only = build(tmp_path / "p", [skipped(reason="Windows DPAPI only")])
    assert platform_only["status"] == "PASS"


def test_module_attribution_uses_the_test_file(tmp_path):
    report = build(tmp_path, [
        failed(name="test_a", file="tests/test_secure_store.py"),
        failed(name="test_b", file="tests/release_regression/test_report.py"),
        failed(name="test_c", file="tests/test_unknown_new.py"),
    ])
    modules = {row["id"]: row for row in report["modules"]}
    assert modules["2.2"]["failed"] == 1
    assert modules["7.2"]["failed"] == 1
    assert modules["unmapped"]["failed"] == 1
    assert len(report["failures"]) == 3


def test_aggregate_requires_both_platforms_and_one_commit(tmp_path):
    reports = [build(tmp_path / "a", [passed()], platform="linux", commit="abc"),
               build(tmp_path / "b", [passed()], platform="windows", commit="abc")]
    value = REPORT.aggregate_reports(reports)
    assert value["status"] == "PASS"
    assert value["source_commit"] == "abc"
    missing = REPORT.aggregate_reports(reports[:1])
    assert missing["status"] == "FAIL"
    assert missing["missing_platforms"] == ["windows"]
    mismatch = REPORT.aggregate_reports([
        build(tmp_path / "c", [passed()], platform="linux", commit="abc"),
        build(tmp_path / "d", [passed()], platform="windows", commit="def")])
    assert mismatch["status"] == "FAIL"
    assert any(gap["kind"] == "report_commit_mismatch" for gap in mismatch["gaps"])


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


def test_marker_scan_accepts_a_clean_public_report(tmp_path):
    archive = tmp_path / "clean.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("report.json", json.dumps({"status": "PASS", "items": []}))
    assert REPORT.marker_scan_archives([archive], MARKERS)["ok"] is True


def test_release_gate_is_incomplete_without_a_candidate_and_fails_when_required(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for name, platform in (("linux.json", "linux"), ("windows.json", "windows")):
        report = build(tmp_path, [passed()], platform=platform, commit="abc")
        REPORT.write_report(report, reports / name)
    value = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                require_dist=False)
    assert value["schema"] == REPORT.RELEASE_SCHEMA
    assert value["checks"]["reports"]["status"] == "PASS"
    assert value["checks"]["candidate"]["state"] == "NOT_BUILT"
    assert value["status"] == "INCOMPLETE"
    strict = REPORT.release_gate(reports_dir=reports, dist=tmp_path / "missing",
                                 require_dist=True)
    assert strict["status"] == "FAIL"


def test_release_gate_requires_test_evidence(tmp_path):
    value = REPORT.release_gate(dist=tmp_path / "missing")
    assert value["checks"]["reports"]["status"] == "MISSING"
    assert any(gap["kind"] == "test_evidence_missing" for gap in value["gaps"])
    assert value["status"] == "FAIL"


def test_release_gate_rejects_a_leaking_wheel(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "openthu-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as bundle:
        bundle.writestr("leak.txt", MARKERS["path"])
    value = REPORT.release_gate(dist=dist, markers=MARKERS)
    assert value["checks"]["marker_archives"]["ok"] is False
    assert any(row["rule"] == "marker:path"
               for row in value["checks"]["marker_archives"]["findings"])
    assert value["status"] == "FAIL"
    assert marker("path") not in json.dumps(value, ensure_ascii=False)


def test_candidate_check_reports_not_built_without_dist(tmp_path):
    value = REPORT.candidate_check(tmp_path / "missing")
    assert value == {"ok": False, "state": "NOT_BUILT", "artifacts": []}


def test_dependency_and_defaults_audits_pass_on_this_checkout():
    dependencies = REPORT.dependency_audit()
    assert dependencies["ok"] is True, dependencies["issues"]
    assert dependencies["runtime"] == {"cryptography": "50.0.2", "pillow": "13.0.0",
                                       "requests": "2.33.0"}
    assert dependencies["node"] == {"tough-cookie": "6.0.0"}
    defaults = REPORT.defaults_audit(platform_name="posix")
    assert defaults["ok"] is True, defaults["issues"]
    assert defaults["details"]["listen_host"] == "127.0.0.1"
    assert defaults["details"]["secrets"] == "environment-only"


def test_cli_report_writes_a_safe_json_file(tmp_path, capsys):
    junit = write_junit(tmp_path / "cli.xml", [passed()])
    output = tmp_path / "report.json"
    exit_code = REPORT.main(["report", "--junit", str(junit), "--platform", "linux",
                             "--commit", "deadbeef", "--output", str(output)])
    assert exit_code == 0
    assert output.is_file()
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["schema"] == REPORT.REPORT_SCHEMA
    assert value["source"]["commit"] == "deadbeef"
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["status"] == "PASS"
    assert summary["totals"]["passed"] == 1


def test_cli_gate_returns_non_zero_for_a_failure(tmp_path):
    junit = write_junit(tmp_path / "gate.xml", [failed()])
    output = tmp_path / "gate.json"
    exit_code = REPORT.main(["gate", "--junit", str(junit), "--platform", "linux",
                             "--commit", "deadbeef", "--output", str(output)])
    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "FAIL"
