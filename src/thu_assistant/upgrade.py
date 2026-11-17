"""Preview and apply the legacy-to-current state migration.

The rules are deliberately conservative:

* a preview only reads the legacy tree; nothing is moved, renamed or deleted;
* Windows DPAPI ciphertext is never copied to another platform for decryption;
  the item is reported as ``UNSUPPORTED_PLATFORM`` and needs a new login;
* an existing target is a ``CONFLICT``; the default keeps both files and asks
  for an explicit ``replace``/``keep`` decision instead of overwriting;
* a copy is written to a temporary file, verified by hash and only then
  renamed into place, so a failed migration keeps the original file and the
  journal entry that describes the attempt;
* installers replace program resources only.  Downloads, logs and the legacy
  tree itself are never rewritten or removed by this module.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from . import paths

PREVIEW_SCHEMA = "openthu-migration-preview-v1"
JOURNAL_SCHEMA = "openthu-migration-journal-v1"
JOURNAL_NAME = "migration-journal.jsonl"

#: A file is written under this suffix first and renamed only after the hash
#: check succeeded.
PENDING_SUFFIX = ".migrating"
BACKUP_SUFFIX = ".pre-migration"


class UpgradeError(ValueError):
    """A migration request that cannot be honoured safely."""

    def __init__(self, code: str, item: str = ""):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}" if item else code)


_DIRECTORY_KINDS = {
    "actions": ("ACTION_JOURNAL", "CACHE"),
    "todos": ("TODO_CACHE", "CACHE"),
    "cache": ("CACHE", "CACHE"),
    "downloads": ("DOWNLOADS", "USER_FILES"),
    "logs": ("LOGS", "USER_FILES"),
    "learn": ("LEARN_SESSION", "SESSION"),
}


def _classify(relative: str, *, platform_name: str) -> dict:
    """Describe one legacy entry without opening or decrypting it."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if not parts:
        raise UpgradeError("MIGRATION_ITEM_INVALID", relative)
    top = parts[0].lower()
    base = parts[-1].lower()
    kind, category = _DIRECTORY_KINDS.get(top, ("STATE", "STATE"))
    if base.endswith(".dpapi"):
        kind, category = "SESSION_CIPHERTEXT", "SESSION"
        fmt = "DPAPI"
    elif base.endswith(".secure"):
        kind, category = "SESSION_CIPHERTEXT", "SESSION"
        fmt = "SECURE-V3"
    elif base in {"session.json", "cookies.json", "token.json"} and len(parts) == 1:
        kind, category = "SESSION_PLAINTEXT", "SESSION"
        fmt = "PLAINTEXT"
    elif base == "config.json" and len(parts) == 1:
        kind, category = "CONFIG", "CONFIG"
        fmt = "JSON"
    else:
        fmt = "JSON" if base.endswith(".json") else "BINARY"
    return {
        "id": "/".join(parts),
        "relative_path": "/".join(parts),
        "kind": kind,
        "category": category,
        "format": fmt,
        "platform": platform_name,
    }


def _compatibility(item: dict, *, platform_name: str, key_file: Path | None) -> dict:
    """Return compatibility, re-authentication need and the reason code."""
    kind = item["kind"]
    if kind == "SESSION_CIPHERTEXT" and item["format"] == "DPAPI":
        if platform_name != "nt":
            return {
                "compatibility": "UNSUPPORTED_PLATFORM",
                "requires_reauth": True,
                "reason": "Windows DPAPI 密文只能由同一 Windows 账户解密；不导往其他平台",
            }
        return {
            "compatibility": "COMPATIBLE",
            "requires_reauth": False,
            "reason": "同一 Windows 账户的 DPAPI 密文保持可读",
        }
    if kind == "SESSION_CIPHERTEXT" and item["format"] == "SECURE-V3":
        if key_file is not None and key_file.is_file():
            return {
                "compatibility": "COMPATIBLE_IF_KEY",
                "requires_reauth": False,
                "reason": "需要原主密钥文件；密钥只由使用者在本机提供",
            }
        return {
            "compatibility": "COMPATIBLE_IF_KEY",
            "requires_reauth": True,
            "reason": "未找到主密钥文件，迁移后需要重新认证",
        }
    if kind == "SESSION_PLAINTEXT":
        return {
            "compatibility": "UNSUPPORTED_FORMAT",
            "requires_reauth": True,
            "reason": "明文会话不是受支持的状态格式，不迁移也不复制",
        }
    if kind == "ACTION_JOURNAL":
        return {
            "compatibility": "REVIEW_REQUIRED",
            "requires_reauth": False,
            "reason": "未决事务必须人工核对；迁移只复制，不重放",
        }
    if kind == "CONFIG":
        return {
            "compatibility": "COMPATIBLE",
            "requires_reauth": False,
            "reason": "配置复制到新位置；秘密仍从环境变量读取",
        }
    if kind in {"TODO_CACHE", "CACHE", "STATE"}:
        return {
            "compatibility": "COMPATIBLE",
            "requires_reauth": False,
            "reason": "可重建缓存；迁移失败只影响缓存",
        }
    if kind in {"DOWNLOADS", "LOGS"}:
        return {
            "compatibility": "EXCLUDED_USER_FILES",
            "requires_reauth": False,
            "reason": "使用者下载与日志保持原位；迁移不改写也不复制",
        }
    if kind == "LEARN_SESSION":
        return {
            "compatibility": "REVIEW_REQUIRED",
            "requires_reauth": True,
            "reason": "学习会话按文件格式逐项判断；不支持的格式需要重新登录",
        }
    return {
        "compatibility": "COMPATIBLE",
        "requires_reauth": False,
        "reason": "使用者文件保持原位，程序升级不改写",
    }


def _iter_legacy_files(root: Path) -> list[str]:
    rows: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file():
            rows.append(path.relative_to(root).as_posix())
    return rows


def preview(
    *,
    legacy_root: Path | str | None = None,
    target_root: Path | str | None = None,
    env: dict[str, str] | None = None,
    platform_name: str | None = None,
    key_file: Path | str | None = None,
    format_version: str = "openthu-state-1",
) -> dict:
    """Describe every legacy item without changing either tree.

    ``legacy_root``/``target_root`` default to the frozen path resolver, so a
    caller cannot accidentally point the migration at an unrelated directory
    by omitting them.
    """
    platform_name = platform_name or os.name
    if legacy_root is None:
        legacy_root = paths.legacy_state_dir(env=env, platform_name=platform_name)
    if target_root is None:
        target_root = paths.default_paths(env=env, platform_name=platform_name).state_dir
    legacy = Path(legacy_root)
    target = Path(target_root)
    if target == legacy:
        raise UpgradeError("MIGRATION_SAME_ROOT", str(target))
    if key_file is not None:
        key_file = Path(key_file)

    items: list[dict] = []
    if legacy.is_dir():
        for relative in _iter_legacy_files(legacy):
            item = _classify(relative, platform_name=platform_name)
            item.update(_compatibility(item, platform_name=platform_name, key_file=key_file))
            item["source"] = str(legacy / item["relative_path"])
            item["target"] = str(target / item["relative_path"])
            item["target_exists"] = (target / item["relative_path"]).exists()
            if item["target_exists"]:
                item["status"] = "CONFLICT"
                item["reason"] = item["reason"] + "；目标已存在，需明确选择 replace/keep"
            elif item["compatibility"].startswith("UNSUPPORTED"):
                item["status"] = "UNSUPPORTED"
            elif item["compatibility"] == "EXCLUDED_USER_FILES":
                item["status"] = "EXCLUDED"
            else:
                item["status"] = "READY"
            items.append(item)

    return {
        "schema_version": PREVIEW_SCHEMA,
        "action": "preview-only",
        "moved": False,
        "from": str(legacy),
        "to": str(target),
        "legacy_present": legacy.is_dir(),
        "target_present": target.is_dir(),
        "format_version": format_version,
        "platform": platform_name,
        "items": items,
        "summary": _summary(items),
        "note": "预览只读取旧目录；不移动、不删除、不复制任何文件。",
    }


def _summary(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "items": len(items),
        "ready": counts.get("READY", 0),
        "conflicts": counts.get("CONFLICT", 0),
        "unsupported": counts.get("UNSUPPORTED", 0),
        "excluded": counts.get("EXCLUDED", 0),
        "requires_reauth": sum(1 for item in items if item["requires_reauth"]),
        "requires_review": sum(
            1 for item in items if item["compatibility"] == "REVIEW_REQUIRED"
        ),
    }


def describe(plan: dict) -> str:
    """Human-readable preview; contains no file contents or secret values."""
    lines = [
        "Openthu 状态迁移预览（只读）",
        "",
        "从：" + plan["from"],
        "到：" + plan["to"],
        "",
    ]
    for item in plan["items"]:
        flag = {
            "READY": "可迁移",
            "CONFLICT": "冲突",
            "UNSUPPORTED": "不迁移",
            "EXCLUDED": "保持原位",
        }.get(item["status"], item["status"])
        lines.append(
            f"[{flag}] {item['relative_path']}（{item['kind']}／{item['format']}，"
            f"{item['compatibility']}）：{item['reason']}"
        )
    summary = plan["summary"]
    lines.extend(
        [
            "",
            f"合计 {summary['items']} 项：可迁移 {summary['ready']}，"
            f"冲突 {summary['conflicts']}，不支持 {summary['unsupported']}，"
            f"需重新认证 {summary['requires_reauth']}，需人工核对 {summary['requires_review']}。",
            plan["note"],
        ]
    )
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_journal(handle, record: dict) -> None:
    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _copy_verified(source: Path, target: Path) -> str:
    """Copy one file atomically; leaves the source untouched on any failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + PENDING_SUFFIX)
    if staging.exists():
        raise UpgradeError("MIGRATION_STAGING_EXISTS", str(staging.name))
    expected = _sha256(source)
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            with source.open("rb") as reader:
                for chunk in iter(lambda: reader.read(1024 * 256), b""):
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(staging) != expected:
            raise UpgradeError("MIGRATION_HASH_MISMATCH", str(source.name))
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    return expected


def _backup(target: Path) -> Path:
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    descriptor = os.open(
        backup,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        with target.open("rb") as reader:
            for chunk in iter(lambda: reader.read(1024 * 256), b""):
                handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return backup


def apply(
    plan: dict,
    *,
    decisions: dict[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Copy the ready items of a preview into the target tree.

    ``decisions`` maps an item path to ``"replace"`` or ``"keep"``.  Without a
    decision a conflicting item is reported and left alone.  Nothing is ever
    deleted, moved or replayed.
    """
    if not isinstance(plan, dict) or plan.get("schema_version") != PREVIEW_SCHEMA:
        raise UpgradeError("MIGRATION_PLAN_INVALID", "schema")
    decisions = dict(decisions or {})
    for key, value in decisions.items():
        if value not in {"replace", "keep"}:
            raise UpgradeError("MIGRATION_DECISION_INVALID", key)
    target_root = Path(plan["to"])
    results: list[dict] = []
    failed: list[dict] = []
    journal_path = target_root / JOURNAL_NAME
    if not dry_run:
        target_root.mkdir(parents=True, exist_ok=True)
    handle = None
    if not dry_run:
        handle = journal_path.open("a", encoding="utf-8")
        _write_journal(
            handle,
            {
                "schema_version": JOURNAL_SCHEMA,
                "event": "migration-started",
                "format_version": plan.get("format_version"),
                "platform": plan.get("platform"),
                "from": plan["from"],
                "to": plan["to"],
                "items": len(plan["items"]),
            },
        )
    try:
        for item in plan["items"]:
            decision = decisions.get(item["relative_path"])
            record = {
                "item": item["relative_path"],
                "kind": item["kind"],
                "compatibility": item["compatibility"],
            }
            if item["status"] == "UNSUPPORTED" or item["compatibility"] in {
                "UNSUPPORTED_FORMAT",
                "EXCLUDED_USER_FILES",
            }:
                record["status"] = (
                    "SKIPPED_EXCLUDED"
                    if item["compatibility"] == "EXCLUDED_USER_FILES"
                    else "SKIPPED_UNSUPPORTED"
                )
                results.append(record)
                continue
            source = Path(item["source"])
            target = target_root / item["relative_path"]
            if not source.is_file():
                record["status"] = "SKIPPED_MISSING"
                results.append(record)
                continue
            if target.exists():
                if decision == "keep":
                    record["status"] = "SKIPPED_KEEP"
                    record["note"] = "保留目标文件与旧文件"
                    results.append(record)
                    continue
                if decision != "replace":
                    record["status"] = "NEEDS_DECISION"
                    record["note"] = "目标已存在；需显式选择 replace 或 keep"
                    results.append(record)
                    continue
                if dry_run:
                    record["status"] = "WOULD_REPLACE"
                    results.append(record)
                    continue
                assert handle is not None
                _write_journal(handle, {**record, "event": "backup"})
                try:
                    record["backup"] = _backup(target).name
                except OSError as error:
                    record["status"] = "FAILED"
                    record["error"] = getattr(error, "code", type(error).__name__)
                    _write_journal(handle, {**record, "event": "failed"})
                    failed.append(record)
                    results.append(record)
                    continue
            if dry_run:
                record["status"] = "WOULD_COPY"
                results.append(record)
                continue
            assert handle is not None
            _write_journal(handle, {**record, "event": "copy", "source": str(source)})
            try:
                record["sha256"] = _copy_verified(source, target)
                record["status"] = "COPIED"
                _write_journal(handle, {**record, "event": "copied"})
            except (OSError, UpgradeError) as error:
                record["status"] = "FAILED"
                record["error"] = getattr(error, "code", type(error).__name__)
                _write_journal(handle, {**record, "event": "failed"})
                failed.append(record)
            results.append(record)
    finally:
        if handle is not None:
            try:
                _write_journal(
                    handle,
                    {
                        "schema_version": JOURNAL_SCHEMA,
                        "event": "migration-finished",
                        "failed": len(failed),
                    },
                )
            finally:
                handle.close()
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "schema_version": "openthu-migration-result-v1",
        "dry_run": bool(dry_run),
        "ok": not failed,
        "journal": None if dry_run else str(journal_path),
        "counts": counts,
        "failed": [row["item"] for row in failed],
        "results": results,
        "note": "旧目录保持原样；失败项保留原文件与日志，不重试、不删除。",
    }


def encode_preview(plan: dict) -> str:
    """Base64 JSON so a preview can be pasted without exposing file names."""
    payload = json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_preview(text: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(text.encode("ascii")))
    except (ValueError, TypeError):
        raise UpgradeError("MIGRATION_PREVIEW_INVALID", "base64") from None

