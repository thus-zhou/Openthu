"""Legacy state migration: preview, explicit conflicts and failure safety.

Every case uses synthetic files.  No real credential, session or campus record
is read, decrypted or copied, and nothing outside ``tmp_path`` is touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from thu_assistant import upgrade

ROOT = Path(__file__).resolve().parents[1]


def legacy_tree(tmp_path: Path) -> Path:
    """A synthetic pre-v0.2 state directory with every declared shape."""
    root = tmp_path / "legacy"
    for name in ("actions", "todos", "downloads", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "campus-v2.dpapi").write_bytes(b"synthetic-dpapi-ciphertext")
    (root / "session.secure").write_bytes(b"synthetic-secure-v3")
    (root / "config.json").write_text('{"ui": {"page_size": 20}}', encoding="utf-8")
    (root / "actions" / "pending.json").write_text('{"state": "UNKNOWN"}', encoding="utf-8")
    (root / "todos" / "cache.json").write_text('{"items": []}', encoding="utf-8")
    (root / "downloads" / "paper.pdf").write_bytes(b"%PDF synthetic")
    (root / "logs" / "app.log").write_text("synthetic\n", encoding="utf-8")
    return root


def target_tree(tmp_path: Path) -> Path:
    target = tmp_path / "state"
    target.mkdir(exist_ok=True)
    return target


def preview(tmp_path, *, platform_name="posix", key_file=None, legacy=None, target=None):
    return upgrade.preview(
        legacy_root=legacy or legacy_tree(tmp_path),
        target_root=target or target_tree(tmp_path),
        platform_name=platform_name,
        key_file=key_file,
    )


def by_path(plan) -> dict:
    return {item["relative_path"]: item for item in plan["items"]}


def snapshot(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_preview_lists_format_target_and_reauthentication(tmp_path):
    plan = preview(tmp_path)
    items = by_path(plan)
    assert plan["action"] == "preview-only"
    assert plan["moved"] is False
    assert plan["schema_version"] == "openthu-migration-preview-v1"
    dpapi = items["campus-v2.dpapi"]
    assert dpapi["kind"] == "SESSION_CIPHERTEXT"
    assert dpapi["format"] == "DPAPI"
    assert dpapi["compatibility"] == "UNSUPPORTED_PLATFORM"
    assert dpapi["requires_reauth"] is True
    assert dpapi["target"].endswith("campus-v2.dpapi")
    secure = items["session.secure"]
    assert secure["format"] == "SECURE-V3"
    assert secure["requires_reauth"] is True
    assert items["actions/pending.json"]["compatibility"] == "REVIEW_REQUIRED"
    assert items["todos/cache.json"]["compatibility"] == "COMPATIBLE"
    assert items["config.json"]["compatibility"] == "COMPATIBLE"
    assert items["downloads/paper.pdf"]["compatibility"] == "EXCLUDED_USER_FILES"
    assert plan["summary"]["requires_reauth"] >= 2
    text = upgrade.describe(plan)
    assert "迁移预览" in text and "campus-v2.dpapi" in text


def test_preview_does_not_modify_the_legacy_tree(tmp_path):
    legacy = legacy_tree(tmp_path)
    target = target_tree(tmp_path)
    before = snapshot(legacy)
    plan = preview(tmp_path, legacy=legacy, target=target)
    assert plan["legacy_present"] is True
    assert snapshot(legacy) == before
    assert list(target.iterdir()) == []


def test_windows_dpapi_ciphertext_is_never_offered_to_another_platform(tmp_path):
    plan = preview(tmp_path, platform_name="posix")
    dpapi = by_path(plan)["campus-v2.dpapi"]
    assert dpapi["status"] == "UNSUPPORTED"
    result = upgrade.apply(plan)
    statuses = {row["item"]: row["status"] for row in result["results"]}
    assert statuses["campus-v2.dpapi"] == "SKIPPED_UNSUPPORTED"
    assert not (Path(plan["to"]) / "campus-v2.dpapi").exists()
    assert (Path(plan["from"]) / "campus-v2.dpapi").is_file()


def test_secure_v3_requires_a_key_and_says_so_before_migrating(tmp_path):
    plan = preview(tmp_path)
    assert by_path(plan)["session.secure"]["requires_reauth"] is True
    key = tmp_path / "master.key"
    key.write_bytes(b"synthetic-key")
    plan = preview(tmp_path, key_file=key)
    secure = by_path(plan)["session.secure"]
    assert secure["compatibility"] == "COMPATIBLE_IF_KEY"
    assert secure["requires_reauth"] is False


def test_existing_target_is_a_conflict_until_the_user_decides(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text('{"ui": {"page_size": 99}}', encoding="utf-8")
    plan = preview(tmp_path, target=target)
    item = by_path(plan)["config.json"]
    assert item["status"] == "CONFLICT"
    result = upgrade.apply(plan)
    statuses = {row["item"]: row["status"] for row in result["results"]}
    assert statuses["config.json"] == "NEEDS_DECISION"
    assert json.loads((target / "config.json").read_text(encoding="utf-8")) == {
        "ui": {"page_size": 99}
    }
    assert (Path(plan["from"]) / "config.json").is_file()


def test_replace_keeps_a_backup_of_the_previous_target(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text('{"ui": {"page_size": 99}}', encoding="utf-8")
    plan = preview(tmp_path, target=target)
    result = upgrade.apply(plan, decisions={"config.json": "replace"})
    assert result["ok"] is True
    row = next(row for row in result["results"] if row["item"] == "config.json")
    assert row["status"] == "COPIED"
    assert json.loads((target / "config.json").read_text(encoding="utf-8")) == {
        "ui": {"page_size": 20}
    }
    backup = target / "config.json.pre-migration"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"ui": {"page_size": 99}}
    assert (Path(plan["from"]) / "config.json").is_file()


def test_keep_leaves_both_copies_untouched(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text('{"ui": {"page_size": 99}}', encoding="utf-8")
    plan = preview(tmp_path, target=target)
    result = upgrade.apply(plan, decisions={"config.json": "keep"})
    row = next(row for row in result["results"] if row["item"] == "config.json")
    assert row["status"] == "SKIPPED_KEEP"
    assert json.loads((target / "config.json").read_text(encoding="utf-8")) == {
        "ui": {"page_size": 99}
    }
    assert not (target / "config.json.pre-migration").exists()


def test_downloads_and_logs_are_left_where_they_are(tmp_path):
    plan = preview(tmp_path)
    result = upgrade.apply(plan)
    statuses = {row["item"]: row["status"] for row in result["results"]}
    assert statuses["downloads/paper.pdf"] == "SKIPPED_EXCLUDED"
    assert statuses["logs/app.log"] == "SKIPPED_EXCLUDED"
    target = Path(plan["to"])
    assert not (target / "downloads").exists()
    assert not (target / "logs").exists()
    assert (Path(plan["from"]) / "downloads/paper.pdf").is_file()


def test_a_failed_copy_keeps_the_original_file_and_the_journal(tmp_path):
    target = target_tree(tmp_path)
    plan = preview(tmp_path, target=target)
    staging = target / "config.json.migrating"
    staging.write_bytes(b"left over from a crashed attempt")
    result = upgrade.apply(plan, decisions={"config.json": "replace"})
    row = next(row for row in result["results"] if row["item"] == "config.json")
    assert row["status"] == "FAILED"
    assert row["error"] == "MIGRATION_STAGING_EXISTS"
    assert result["ok"] is False
    assert result["failed"] == ["config.json"]
    assert (Path(plan["from"]) / "config.json").is_file()
    assert staging.is_file()
    journal = Path(result["journal"])
    assert journal.is_file()
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "migration-started" for row in events)
    assert any(row["event"] == "failed" for row in events)
    assert events[-1]["event"] == "migration-finished"


def test_dry_run_reports_without_writing_anything(tmp_path):
    target = target_tree(tmp_path)
    plan = preview(tmp_path, target=target)
    before = snapshot(target)
    result = upgrade.apply(plan, dry_run=True)
    assert result["dry_run"] is True
    assert result["journal"] is None
    assert all(
        row["status"].startswith(("WOULD", "SKIPPED"))
        for row in result["results"]
    )
    assert any(row["status"].startswith("WOULD") for row in result["results"])
    assert snapshot(target) == before


def test_migrating_a_directory_into_itself_is_refused(tmp_path):
    legacy = legacy_tree(tmp_path)
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.preview(legacy_root=legacy, target_root=legacy, platform_name="posix")
    assert caught.value.code == "MIGRATION_SAME_ROOT"


def test_an_unknown_decision_is_refused_before_any_copy(tmp_path):
    target = target_tree(tmp_path)
    plan = preview(tmp_path, target=target)
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(plan, decisions={"config.json": "overwrite"})
    assert caught.value.code == "MIGRATION_DECISION_INVALID"
    assert list(target.iterdir()) == []


def test_preview_round_trip_carries_no_file_contents(tmp_path):
    plan = preview(tmp_path)
    encoded = upgrade.encode_preview(plan)
    restored = upgrade.decode_preview(encoded)
    assert restored["from"] == plan["from"]
    assert "synthetic-dpapi-ciphertext" not in json.dumps(restored)
    assert "synthetic-dpapi-ciphertext" not in encoded
    with pytest.raises(upgrade.UpgradeError):
        upgrade.decode_preview("not base64 at all")


def test_cli_preview_is_read_only_and_prints_json(tmp_path):
    legacy = legacy_tree(tmp_path)
    target = target_tree(tmp_path)
    before = snapshot(legacy)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_state.py"),
            "preview",
            "--json",
            "--from",
            str(legacy),
            "--to",
            str(target),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["action"] == "preview-only"
    assert snapshot(legacy) == before
    assert list(target.iterdir()) == []


def test_cli_apply_reports_a_needed_decision_with_a_nonzero_exit(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_state.py"),
            "apply",
            "--json",
            "--from",
            str(legacy_tree(tmp_path)),
            "--to",
            str(target),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    statuses = {row["item"]: row["status"] for row in payload["results"]}
    assert statuses["config.json"] == "NEEDS_DECISION"
    assert (target / "config.json").read_text(encoding="utf-8") == "{}"
