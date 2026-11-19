"""Legacy state migration: preview, real state tree, conflicts and failure safety.

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


def real_state_tree(tmp_path: Path) -> Path:
    """The directory names the 0.2 runtime actually writes."""
    root = tmp_path / "legacy-real"
    for name in (
        "actions-v1",
        "consent-v1",
        "submissions-v1",
        "todos-v1",
        "events-v1",
        "sports-scheduler",
        "keys",
        "downloads",
        "logs",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "actions-v1" / ("a" * 32 + ".dpapi")).write_bytes(b"synthetic-journal")
    (root / "consent-v1" / "host.key").write_bytes(b"synthetic-host-key")
    (root / "consent-v1" / ("b" * 32 + ".grant")).write_bytes(b"synthetic-grant")
    (root / "submissions-v1" / "plan.json").write_text('{"plan": "synthetic"}', encoding="utf-8")
    (root / "todos-v1" / "cache.json").write_text('{"items": []}', encoding="utf-8")
    (root / "events-v1" / "snapshot.json").write_text("{}", encoding="utf-8")
    (root / "sports-scheduler" / "state.json").write_text("{}", encoding="utf-8")
    (root / "keys" / "master.key").write_bytes(b"synthetic-key")
    (root / "downloads" / "paper.pdf").write_bytes(b"%PDF synthetic")
    (root / "logs" / "app.log").write_text("synthetic\n", encoding="utf-8")
    return root


def target_tree(tmp_path: Path) -> Path:
    target = tmp_path / "state"
    target.mkdir(exist_ok=True)
    return target


def preview(tmp_path, *, platform_name="posix", key_file=None, legacy=None, target=None, env=None):
    return upgrade.preview(
        legacy_root=legacy or legacy_tree(tmp_path),
        target_root=target or target_tree(tmp_path),
        platform_name=platform_name,
        key_file=key_file,
        env=env,
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
    assert plan["status"] == "PREVIEW"
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


def test_dpapi_ciphertext_is_not_copied_even_when_the_target_exists_and_replace_is_chosen(
    tmp_path,
):
    """P1-1: a conflict used to override UNSUPPORTED_PLATFORM in apply()."""
    target = target_tree(tmp_path)
    (target / "campus-v2.dpapi").write_bytes(b"existing-target")
    plan = preview(tmp_path, platform_name="posix", target=target)
    item = by_path(plan)["campus-v2.dpapi"]
    assert item["compatibility"] == "UNSUPPORTED_PLATFORM"
    assert item["status"] == "UNSUPPORTED"
    result = upgrade.apply(plan, decisions={"campus-v2.dpapi": "replace"})
    row = next(row for row in result["results"] if row["item"] == "campus-v2.dpapi")
    assert row["status"] == "SKIPPED_UNSUPPORTED"
    assert (target / "campus-v2.dpapi").read_bytes() == b"existing-target"
    assert not (target / "campus-v2.dpapi.pre-migration").exists()


def test_secure_v3_needs_the_runtime_key_location_for_a_no_reauth_claim(tmp_path):
    plan = preview(tmp_path)
    assert by_path(plan)["session.secure"]["requires_reauth"] is True

    elsewhere = tmp_path / "elsewhere.key"
    elsewhere.write_bytes(b"synthetic-key")
    plan = preview(tmp_path, key_file=elsewhere)
    secure = by_path(plan)["session.secure"]
    assert secure["compatibility"] == "COMPATIBLE_IF_KEY"
    assert secure["requires_reauth"] is True
    assert "默认位置" in secure["reason"]

    config_home = tmp_path / "config"
    runtime_key = config_home / "openthu" / "keys" / "master.key"
    runtime_key.parent.mkdir(parents=True)
    runtime_key.write_bytes(b"synthetic-key")
    plan = preview(tmp_path, key_file=runtime_key, env={"XDG_CONFIG_HOME": str(config_home)})
    secure = by_path(plan)["session.secure"]
    assert secure["compatibility"] == "COMPATIBLE_IF_KEY"
    assert secure["requires_reauth"] is False


def test_real_state_directories_keep_their_journal_and_consent_meaning(tmp_path):
    """P1-4: the classifier must match the directories the runtime writes."""
    plan = preview(tmp_path, legacy=real_state_tree(tmp_path), platform_name="nt")
    items = by_path(plan)
    journal = items["actions-v1/" + "a" * 32 + ".dpapi"]
    assert journal["kind"] == "ACTION_JOURNAL"
    assert journal["format"] == "DPAPI"
    assert journal["compatibility"] == "REVIEW_REQUIRED"
    assert journal["status"] == "READY"
    assert items["consent-v1/host.key"]["kind"] == "CONSENT_AUTHORITY"
    assert items["consent-v1/host.key"]["compatibility"] == "EXCLUDED_SENSITIVE"
    assert items["consent-v1/host.key"]["status"] == "EXCLUDED"
    assert items["consent-v1/" + "b" * 32 + ".grant"]["compatibility"] == "EXCLUDED_SENSITIVE"
    assert items["keys/master.key"]["compatibility"] == "EXCLUDED_SENSITIVE"
    assert items["submissions-v1/plan.json"]["compatibility"] == "REVIEW_REQUIRED"
    assert items["todos-v1/cache.json"]["kind"] == "TODO_CACHE"
    assert items["todos-v1/cache.json"]["compatibility"] == "COMPATIBLE"
    assert items["events-v1/snapshot.json"]["kind"] == "EVENT_BASELINE"
    assert items["sports-scheduler/state.json"]["kind"] == "SPORTS_SCHEDULER"
    assert items["downloads/paper.pdf"]["compatibility"] == "EXCLUDED_USER_FILES"
    assert plan["summary"]["requires_review"] == 2
    result = upgrade.apply(plan)
    statuses = {row["item"]: row["status"] for row in result["results"]}
    assert statuses["consent-v1/host.key"] == "SKIPPED_EXCLUDED"
    assert statuses["keys/master.key"] == "SKIPPED_EXCLUDED"
    assert statuses["actions-v1/" + "a" * 32 + ".dpapi"] == "COPIED"
    target = Path(plan["to"])
    assert not (target / "consent-v1").exists()
    assert not (target / "keys").exists()


def test_existing_target_is_a_conflict_until_the_user_decides(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text('{"ui": {"page_size": 99}}', encoding="utf-8")
    plan = preview(tmp_path, target=target)
    item = by_path(plan)["config.json"]
    assert item["status"] == "CONFLICT"
    result = upgrade.apply(plan)
    statuses = {row["item"]: row["status"] for row in result["results"]}
    assert statuses["config.json"] == "NEEDS_DECISION"
    assert result["complete"] is False
    assert result["pending_decisions"] == 1
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
    assert result["complete"] is True
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
    assert result["complete"] is True


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
    assert result["complete"] is False
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
        row["status"].startswith(("WOULD", "SKIPPED")) for row in result["results"]
    )
    assert any(row["status"].startswith("WOULD") for row in result["results"])
    assert snapshot(target) == before


def test_the_default_layout_on_windows_is_a_successful_no_op(tmp_path):
    """P1-7: legacy and current state are the same directory on Windows."""
    state = tmp_path / "AppData" / "Local" / "TsinghuaAssistant"
    state.mkdir(parents=True)
    (state / "config.json").write_text("{}", encoding="utf-8")
    env = {"USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(state.parent)}
    plan = upgrade.preview(env=env, platform_name="nt")
    assert plan["status"] == "NO_MIGRATION_NEEDED"
    assert plan["no_op"] is True
    assert plan["moved"] is False
    assert plan["items"] == []
    assert snapshot(state) == {
        "config.json": ((state / "config.json").stat().st_mtime_ns, b"{}")
    }
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(plan)
    assert caught.value.code == "MIGRATION_SAME_ROOT"


def test_an_environment_state_directory_is_found_and_reported(tmp_path):
    """P2-12: the compatibility variable names a real legacy tree."""
    custom = tmp_path / "custom-state"
    custom.mkdir()
    (custom / "config.json").write_text('{"a": 1}', encoding="utf-8")
    env = {
        "HOME": str(tmp_path),
        "THU_ASSISTANT_STATE_DIR": str(custom),
    }
    plan = upgrade.preview(env=env, platform_name="posix", target_root=custom)
    assert plan["legacy_source"] == "environment:THU_ASSISTANT_STATE_DIR"
    assert plan["status"] == "NO_MIGRATION_NEEDED"
    plan = upgrade.preview(env=env, platform_name="posix", target_root=tmp_path / "new")
    assert plan["legacy_present"] is True
    assert by_path(plan)["config.json"]["status"] == "READY"


def test_a_plan_cannot_escape_the_target_or_the_source_tree(tmp_path):
    """P1-5: apply() must treat the plan as untrusted input."""
    legacy = legacy_tree(tmp_path)
    target = target_tree(tmp_path)
    plan = preview(tmp_path, legacy=legacy, target=target)
    escaped = json.loads(json.dumps(plan))
    escaped["items"][0]["relative_path"] = "../escaped.txt"
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(escaped)
    assert caught.value.code == "MIGRATION_ITEM_INVALID"
    assert not (tmp_path / "escaped.txt").exists()

    outside = json.loads(json.dumps(plan))
    outside["items"][0]["source"] = str(tmp_path / "outside.txt")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(outside)
    assert caught.value.code == "MIGRATION_SOURCE_OUTSIDE"

    relative_target = json.loads(json.dumps(plan))
    relative_target["to"] = "relative/state"
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(relative_target)
    assert caught.value.code == "MIGRATION_PATH_NOT_ABSOLUTE"


def test_a_program_directory_is_refused_as_a_target(tmp_path):
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.preview(
            legacy_root=legacy_tree(tmp_path),
            target_root=ROOT / "src" / "thu_assistant",
            platform_name="posix",
        )
    assert caught.value.code in {"PATH_PROTECTED", "MIGRATION_OVERLAP"}


def test_a_symlinked_target_pointing_at_the_legacy_tree_is_a_no_op(tmp_path):
    """P2-7: string comparison used to miss a symlink to the same directory."""
    legacy = legacy_tree(tmp_path)
    link = tmp_path / "state-link"
    try:
        link.symlink_to(legacy, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable on this platform")
    plan = upgrade.preview(legacy_root=legacy, target_root=link, platform_name="posix")
    assert plan["status"] == "NO_MIGRATION_NEEDED"
    assert plan["items"] == []
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(plan)
    assert caught.value.code == "MIGRATION_SAME_ROOT"
    assert set(path.name for path in legacy.iterdir()) == {
        "actions",
        "todos",
        "downloads",
        "logs",
        "campus-v2.dpapi",
        "session.secure",
        "config.json",
    }


def test_a_target_inside_the_legacy_tree_is_refused(tmp_path):
    legacy = legacy_tree(tmp_path)
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.preview(
            legacy_root=legacy, target_root=legacy / "nested", platform_name="posix"
        )
    assert caught.value.code == "MIGRATION_OVERLAP"


def test_an_unknown_decision_is_refused_before_any_copy(tmp_path):
    target = target_tree(tmp_path)
    plan = preview(tmp_path, target=target)
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(plan, decisions={"config.json": "overwrite"})
    assert caught.value.code == "MIGRATION_DECISION_INVALID"
    assert list(target.iterdir()) == []


def test_a_duplicate_item_is_refused(tmp_path):
    plan = preview(tmp_path)
    duplicated = json.loads(json.dumps(plan))
    duplicated["items"].append(duplicated["items"][0])
    with pytest.raises(upgrade.UpgradeError) as caught:
        upgrade.apply(duplicated)
    assert caught.value.code == "MIGRATION_ITEM_DUPLICATE"


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


def test_cli_apply_exits_nonzero_while_a_decision_is_pending(tmp_path):
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
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["complete"] is False
    statuses = {row["item"]: row["status"] for row in payload["results"]}
    assert statuses["config.json"] == "NEEDS_DECISION"
    assert (target / "config.json").read_text(encoding="utf-8") == "{}"


def test_cli_apply_exits_zero_after_an_explicit_replace(tmp_path):
    target = target_tree(tmp_path)
    (target / "config.json").write_text("{}", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "migrate_state.py"),
            "apply",
            "--from",
            str(legacy_tree(tmp_path)),
            "--to",
            str(target),
            "--replace",
            "config.json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert (target / "config.json.pre-migration").is_file()
