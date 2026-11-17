"""Sensitive marker set across public outputs, archives, caches and handoffs.

Every marker is built at runtime from harmless pieces, so this file itself does
not contain a token, a private host or a user path.  The tests inject the values
into synthetic names, accounts, paths, hosts, tokens, files and images, then
prove that:

* private state still round-trips through the protected store;
* the public projection, error objects, reports, media descriptors and handoff
  checkpoints do not carry the raw values;
* the privacy/marker gates fail stably when a value reaches a public archive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from thu_assistant import source_state
from thu_assistant.local_api import projection
from thu_assistant.media_assets import MediaAssets
from thu_assistant.secure_store import SecureStore, create_key_file, load_provider
from thu_assistant.submissions.errors import SubmissionError

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = "windows-dpapi" if os.name == "nt" else "file-key"
ACCOUNT_SCOPE = "acct_" + "c" * 24
MARKERS = {
    "name": "SYNTHETIC-PERSON-" + "A1B2C3",
    "account": "SYNTHETIC-STUDENT-" + "D4E5F6",
    "scope": ACCOUNT_SCOPE,
    "home": "/" + "home/" + "synthetic-marker" + "/private/notes.txt",
    "host": ".".join(["192", "168", "77", "9"]),
    "token": "ghp_" + "Q" * 36,
    "file": "SYNTHETIC-PRIVATE-FILE.pdf",
    "image": "SYNTHETIC-IMAGE-COMMENT-7.2",
}


def load_report_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "regression_report_markers", ROOT / "scripts" / "regression_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REPORT = load_report_module()


def assert_clean(value: object, *keys: str) -> None:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    selected = keys or ("name", "account", "home", "host", "token", "file", "image")
    for key in selected:
        assert MARKERS[key] not in text, (key, text[:300])


# ---------------------------------------------------------------------------
# public projection and error objects
# ---------------------------------------------------------------------------

def test_local_api_projection_removes_private_markers():
    envelope = {
        "status": "OK",
        "source": {"service_id": "learn", "operation": "courses",
                   "adapter_version": "fixture-v1",
                   "url": "https://" + MARKERS["host"] + "/course/1"},
        "checked_at": "2026-11-17T09:00:00+08:00",
        "verification_level": "BUSINESS_READ",
        "source_complete": True, "snapshot_complete": True, "complete": True,
        "next_cursor": None, "warnings": [],
        "items": [{
            "member_name": MARKERS["name"],
            "student_id": MARKERS["account"],
            "cookie_header": MARKERS["token"],
            "note": "see " + MARKERS["home"],
            "source_url": "https://" + MARKERS["host"] + "/",
            "title": "合成课程",
        }],
    }
    projected = projection.project(envelope, service_id="learn", operation="courses",
                                   request_id="req-synthetic-1")
    assert_clean(projected)
    item = projected["items"][0]
    assert "member_name" not in item
    assert "student_id" not in item
    assert "cookie_header" not in item
    assert item["note"] == "see [REDACTED]"
    assert item["source_url"] is None
    assert projected["source"]["url"] is None
    warnings = set(projected["warnings"])
    assert "LOCAL_API_SENSITIVE_FIELD_DROPPED" in warnings
    assert "LOCAL_API_ABSOLUTE_PATH_REDACTED" in warnings
    assert "LOCAL_API_LINK_WITHHELD" in warnings


def test_public_error_objects_never_copy_marker_detail():
    error = SubmissionError("SUBMISSION_INVALID_ARGUMENTS", MARKERS["home"])
    assert error.detail == "DETAIL_REDACTED"
    assert_clean(error.as_error())
    public = source_state.public_error(
        "SUBMISSION_INVALID_ARGUMENTS", message="failed at " + MARKERS["home"])
    assert_clean(public)
    assert public["message"] != "failed at " + MARKERS["home"]
    classified = source_state.classify_error(error)
    assert_clean(classified)


# ---------------------------------------------------------------------------
# private storage keeps the value while the public surface does not
# ---------------------------------------------------------------------------

def test_secure_store_round_trips_markers_without_plaintext_on_disk(tmp_path):
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, PLATFORM)
    store = SecureStore(load_provider(key_path), tmp_path / "data")
    payload = json.dumps({"account": MARKERS["account"], "token": MARKERS["token"],
                          "path": MARKERS["home"], "file": MARKERS["file"]},
                         ensure_ascii=False).encode("utf-8")
    store.write("marker-record", payload, purpose="SESSION", account_domain="CAMPUS",
                service_domain="LEARN", account=MARKERS["account"])
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for key in ("account", "token", "home", "file"):
                assert MARKERS[key].encode("utf-8") not in data, (path.name, key)
    restored = store.read("marker-record", purpose="SESSION", account_domain="CAMPUS",
                          service_domain="LEARN", account=MARKERS["account"])
    assert json.loads(restored)["token"] == MARKERS["token"]


def test_image_comment_marker_is_removed_from_public_descriptor(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    base = source_dir / "base.png"
    # A valid synthetic image, then a real PNG text chunk carrying the marker.
    from thu_assistant.media_assets import synthetic as media_synthetic
    base.write_bytes(media_synthetic.png())
    marked = source_dir / "marked.png"
    image = Image.open(base)
    info = PngInfo()
    info.add_text("Comment", MARKERS["image"])
    image.save(marked, pnginfo=info)
    assert MARKERS["image"].encode() in marked.read_bytes()

    assets = MediaAssets(tmp_path / "temp")
    reference = assets.register_user_file(
        marked, account_scope=ACCOUNT_SCOPE, task_id="task-marker-1",
        purpose="MODEL_INPUT", source_evidence_id="local.user_selected")
    descriptor = assets.descriptor(reference.media_id, account_scope=ACCOUNT_SCOPE,
                                   task_id="task-marker-1")
    manifest = assets.manifest([reference.media_id], account_scope=ACCOUNT_SCOPE,
                               task_id="task-marker-1")
    source_info = assets.source_info(reference.media_id, account_scope=ACCOUNT_SCOPE,
                                     task_id="task-marker-1")
    assert_clean(descriptor)
    assert_clean(manifest)
    assert_clean(source_info)
    assert MARKERS["image"].encode() not in assets.read_bytes(
        reference.media_id, account_scope=ACCOUNT_SCOPE, task_id="task-marker-1")
    assert MARKERS["image"].encode() in marked.read_bytes()  # the source is untouched
    assets.close()


# ---------------------------------------------------------------------------
# reports, archives and handoffs
# ---------------------------------------------------------------------------

def test_report_generator_never_echoes_raw_failure_values(tmp_path):
    value = " ".join([MARKERS["home"], MARKERS["token"], MARKERS["host"],
                      MARKERS["account"]])
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        '<testcase classname="tests.test_local_api" name="test_marker" '
        'file="tests/test_local_api.py"><failure message="AssertionError: '
        + value + '">' + value + '</failure></testcase></testsuite></testsuites>',
        encoding="utf-8")
    report = REPORT.build_report(REPORT.parse_junit(junit), platform="linux",
                                 commit="deadbeef")
    assert_clean(report)
    assert report["failures"][0]["error_class"] == "AssertionError"
    assert report["status"] == "FAIL"


def test_privacy_gate_rejects_markers_in_public_archives(tmp_path):
    archive = tmp_path / "public.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("notes.txt", " ".join([
            MARKERS["home"], MARKERS["token"], MARKERS["host"], MARKERS["account"],
            MARKERS["scope"]]))
        bundle.writestr("session.json", "{}")
    privacy = REPORT.privacy_scan_archives([archive])
    markers = REPORT.marker_scan_archives([archive])
    assert privacy["ok"] is False
    assert markers["ok"] is False
    assert "github-token" in {row["rule"] for row in privacy["findings"]}
    assert {"private-path", "credential-token", "private-host", "account-scope",
            "credential-file"} <= {row["rule"] for row in markers["findings"]}
    assert_clean(privacy)
    assert_clean(markers)


def test_marker_findings_scan_stdout_without_returning_the_value():
    text = "stdout: " + " ".join([MARKERS["home"], MARKERS["token"], MARKERS["host"]])
    rows = REPORT.marker_findings("stdout", text.encode("utf-8"))
    assert {row["rule"] for row in rows} >= {"private-path", "credential-token",
                                             "private-host"}
    assert_clean(rows)


def test_handoff_checkpoint_does_not_embed_private_note_values(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "handoffs").mkdir()
    shutil.copyfile(ROOT / "scripts" / "handoff.py", tmp_path / "scripts" / "handoff.py")
    (tmp_path / "handoffs" / "TEMPLATE.md").write_text("unfilled template\n", encoding="utf-8")
    (tmp_path / "handoffs" / "T1.md").write_text(
        "Goal: " + MARKERS["name"] + " at " + MARKERS["home"] + "\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-b", "hermes/synthetic")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "tests@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("add", ".")
    git("commit", "-m", "Synthetic task state")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "handoff.py"), "T1", "--to", "codex"],
        cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    checkpoint = json.loads((tmp_path / "handoffs" / "T1.json").read_text(encoding="utf-8"))
    assert checkpoint["dispatch"] == "manual"
    assert checkpoint["next_owner"] == "codex"
    assert_clean(checkpoint)
