"""Preview and apply the legacy-to-current state migration.

The rules are deliberately conservative:

* a preview only reads the legacy tree; nothing is moved, renamed or deleted;
* Windows DPAPI session ciphertext is never copied to another platform for
  decryption; it is reported as ``UNSUPPORTED_PLATFORM`` and needs a new login;
* real state directories (``actions-v1``, ``consent-v1``, ``submissions-v1``)
  keep their journal or consent meaning: they are reported for review or
  excluded, never silently treated as a rebuildable cache;
* an existing target is a ``CONFLICT``; the default keeps both files and asks
  for an explicit ``replace``/``keep`` decision instead of overwriting;
* a plan is untrusted input: both roots and every item path are validated
  before anything is written;
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
from pathlib import Path, PurePosixPath

from . import paths

PREVIEW_SCHEMA = "openthu-migration-preview-v1"
JOURNAL_SCHEMA = "openthu-migration-journal-v1"
JOURNAL_NAME = "migration-journal.jsonl"

#: A file is written under this suffix first and renamed only after the hash
#: check succeeded.
PENDING_SUFFIX = ".migrating"
BACKUP_SUFFIX = ".pre-migration"

#: The real 0.2 state tree, as written by ``action_store``, ``consent.store``,
#: ``submissions.store``, ``todos``, ``snapshot_diff`` and ``sports_transport``.
#: The pre-0.2 names are kept as aliases so an older tree is not misread.
_DIRECTORY_KINDS: dict[str, tuple[str, str]] = {
    "actions-v1": ("ACTION_JOURNAL", "JOURNAL"),
    "actions": ("ACTION_JOURNAL", "JOURNAL"),
    "consent-v1": ("CONSENT_AUTHORITY", "CONSENT"),
    "consent": ("CONSENT_AUTHORITY", "CONSENT"),
    "submissions-v1": ("SUBMISSION_PLAN", "JOURNAL"),
    "todos-v1": ("TODO_CACHE", "CACHE"),
    "todos": ("TODO_CACHE", "CACHE"),
    "events-v1": ("EVENT_BASELINE", "CACHE"),
    "sports-scheduler": ("SPORTS_SCHEDULER", "CACHE"),
    "cache": ("CACHE", "CACHE"),
    "downloads": ("DOWNLOADS", "USER_FILES"),
    "logs": ("LOGS", "USER_FILES"),
    "learn": ("LEARN_SESSION", "SESSION"),
    "learnassistant": ("LEARN_SESSION", "SESSION"),
    "keys": ("KEY_MATERIAL", "KEY"),
    "keyrings": ("KEY_MATERIAL", "KEY"),
    "secrets": ("KEY_MATERIAL", "KEY"),
    "credentials": ("KEY_MATERIAL", "KEY"),
}

#: Compatibility values that always mean "do not copy", independent of the
#: item status.  ``apply()`` checks these prefixes, not ``status``.
_SKIP_PREFIXES = ("UNSUPPORTED",)
_SKIP_VALUES = {"EXCLUDED_USER_FILES", "EXCLUDED_SENSITIVE"}


class UpgradeError(ValueError):
    """A migration request that cannot be honoured safely."""

    def __init__(self, code: str, item: str = ""):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}" if item else code)


def _format_of(base: str, kind: str) -> str:
    if base.endswith(".dpapi"):
        return "DPAPI"
    if base.endswith(".secure"):
        return "SECURE-V3"
    if base.endswith(".grant"):
        return "GRANT"
    if base.endswith(".key"):
        return "KEY"
    if kind == "KEY_MATERIAL":
        return "KEY"
    if base.endswith(".json"):
        return "JSON"
    return "BINARY"


def _classify(relative: str, *, platform_name: str) -> dict:
    """Describe one legacy entry without opening or decrypting it.

    The directory decides the kind first: ``actions-v1/<id>.dpapi`` is an
    action journal entry, not a session, even though it shares the extension.
    """
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if not parts:
        raise UpgradeError("MIGRATION_ITEM_INVALID", relative)
    top = parts[0].lower()
    base = parts[-1].lower()
    directory = _DIRECTORY_KINDS.get(top) if len(parts) > 1 else None
    if directory is not None:
        kind, category = directory
    elif base.endswith(".dpapi"):
        kind, category = "SESSION_CIPHERTEXT", "SESSION"
    elif base.endswith(".secure"):
        kind, category = "SESSION_CIPHERTEXT", "SESSION"
    elif base in {"session.json", "cookies.json", "token.json"} and len(parts) == 1:
        kind, category = "SESSION_PLAINTEXT", "SESSION"
    elif base == "config.json" and len(parts) == 1:
        kind, category = "CONFIG", "CONFIG"
    elif base == "directory.json" and len(parts) == 1:
        kind, category = "CACHE", "CACHE"
    else:
        kind, category = "STATE", "STATE"
    return {
        "id": "/".join(parts),
        "relative_path": "/".join(parts),
        "kind": kind,
        "category": category,
        "format": _format_of(base, kind),
        "platform": platform_name,
    }


def _compatibility(
    item: dict,
    *,
    platform_name: str,
    key_file: Path | None,
    runtime_key: Path | None,
) -> dict:
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
        if key_file is None or not key_file.is_file():
            return {
                "compatibility": "COMPATIBLE_IF_KEY",
                "requires_reauth": True,
                "reason": "未提供主密钥文件；迁移后需要重新认证",
            }
        if runtime_key is not None and _same_file(key_file, runtime_key):
            return {
                "compatibility": "COMPATIBLE_IF_KEY",
                "requires_reauth": False,
                "reason": "主密钥位于运行时默认位置（配置目录的 keys/master.key）",
            }
        return {
            "compatibility": "COMPATIBLE_IF_KEY",
            "requires_reauth": True,
            "reason": "提供的密钥不在运行时默认位置；需自行放到配置目录的 keys/master.key",
        }
    if kind == "SESSION_PLAINTEXT":
        return {
            "compatibility": "UNSUPPORTED_FORMAT",
            "requires_reauth": True,
            "reason": "明文会话不是受支持的状态格式，不迁移也不复制",
        }
    if kind in {"ACTION_JOURNAL", "SUBMISSION_PLAN"}:
        return {
            "compatibility": "REVIEW_REQUIRED",
            "requires_reauth": False,
            "reason": "未决事务或待提交计划必须人工核对；迁移只复制，不重放",
        }
    if kind in {"CONSENT_AUTHORITY", "KEY_MATERIAL"}:
        return {
            "compatibility": "EXCLUDED_SENSITIVE",
            "requires_reauth": False,
            "reason": "主机授权密钥与待消费授权不随状态迁移；需要时请重新授权",
        }
    if kind == "LEARN_SESSION":
        return {
            "compatibility": "REVIEW_REQUIRED",
            "requires_reauth": True,
            "reason": "学习会话按文件格式逐项判断；不支持的格式需要重新登录",
        }
    if kind in {"DOWNLOADS", "LOGS"}:
        return {
            "compatibility": "EXCLUDED_USER_FILES",
            "requires_reauth": False,
            "reason": "使用者下载与日志保持原位；迁移不改写也不复制",
        }
    if kind == "CONFIG":
        return {
            "compatibility": "COMPATIBLE",
            "requires_reauth": False,
            "reason": "配置复制到新位置；秘密仍从环境变量读取",
        }
    return {
        "compatibility": "COMPATIBLE",
        "requires_reauth": False,
        "reason": "可重建缓存；迁移失败只影响缓存",
    }


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _iter_legacy_files(root: Path) -> list[str]:
    rows: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file():
            rows.append(path.relative_to(root).as_posix())
    return rows


def _absolute_root(value, item: str) -> Path:
    text = str(value or "")
    if not text or any(character in text for character in ("\x00", "\n", "\r")):
        raise UpgradeError("MIGRATION_PATH_NOT_ABSOLUTE", item)
    candidate = Path(text)
    if not candidate.is_absolute():
        raise UpgradeError("MIGRATION_PATH_NOT_ABSOLUTE", item)
    return candidate


def _validated_target(root: Path) -> Path:
    """A state target must not sit inside program resources or key folders."""
    try:
        return paths.ensure_private_directory(root, item="migration.target")
    except paths.PathError as error:
        raise UpgradeError(error.code, "migration.target") from None


def _safe_relative(value) -> PurePosixPath:
    text = str(value or "").replace("\\", "/")
    if not text or text.startswith("/") or any(
        part in {"..", ""} for part in text.split("/")
    ):
        raise UpgradeError("MIGRATION_ITEM_INVALID", text[:80])
    pure = PurePosixPath(text)
    if pure.is_absolute() or not pure.parts:
        raise UpgradeError("MIGRATION_ITEM_INVALID", text[:80])
    return pure


def _overlaps(left: Path, right: Path) -> bool:
    left_resolved = left.resolve(strict=False)
    right_resolved = right.resolve(strict=False)
    return (
        left_resolved == right_resolved
        or paths.is_within(left_resolved, right_resolved)
        or paths.is_within(right_resolved, left_resolved)
    )


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
    by omitting them.  When both resolve to the same directory the result is a
    successful no-op: the state is already where the current version keeps it.
    """
    platform_name = platform_name or os.name
    legacy_source = "explicit"
    if legacy_root is None:
        legacy_root = paths.legacy_state_dir(env=env, platform_name=platform_name)
        legacy_source = paths.legacy_state_source(env=env, platform_name=platform_name)
    if target_root is None:
        target_root = paths.default_paths(env=env, platform_name=platform_name).state_dir
    legacy = _absolute_root(legacy_root, "migration.from")
    target = _absolute_root(target_root, "migration.to")
    if key_file is not None:
        key_file = Path(key_file)

    if _overlaps(target, legacy):
        if _same_file(target, legacy):
            return {
                "schema_version": PREVIEW_SCHEMA,
                "action": "preview-only",
                "status": "NO_MIGRATION_NEEDED",
                "no_op": True,
                "same_root": True,
                "moved": False,
                "from": str(legacy),
                "to": str(target),
                "legacy_source": legacy_source,
                "legacy_present": legacy.is_dir(),
                "target_present": target.is_dir(),
                "format_version": format_version,
                "platform": platform_name,
                "items": [],
                "summary": _summary([]),
                "note": "状态目录已经是当前版本的位置，无需迁移；不会写入任何文件。",
            }
        raise UpgradeError("MIGRATION_OVERLAP", str(target))

    target = _validated_target(target)
    runtime_key = paths.default_paths(env=env, platform_name=platform_name).config_file
    runtime_key = runtime_key.parent / "keys" / "master.key"

    items: list[dict] = []
    if legacy.is_dir():
        for relative in _iter_legacy_files(legacy):
            item = _classify(relative, platform_name=platform_name)
            item.update(
                _compatibility(
                    item,
                    platform_name=platform_name,
                    key_file=key_file,
                    runtime_key=runtime_key,
                )
            )
            item["source"] = str(legacy / item["relative_path"])
            item["target"] = str(target / item["relative_path"])
            item["target_exists"] = (target / item["relative_path"]).exists()
            if item["compatibility"].startswith(_SKIP_PREFIXES):
                item["status"] = "UNSUPPORTED"
                if item["target_exists"]:
                    item["target_note"] = "目标同名文件保持不动"
            elif item["compatibility"] in _SKIP_VALUES:
                item["status"] = "EXCLUDED"
            elif item["target_exists"]:
                item["status"] = "CONFLICT"
                item["reason"] = item["reason"] + "；目标已存在，需明确选择 replace/keep"
            else:
                item["status"] = "READY"
            items.append(item)

    return {
        "schema_version": PREVIEW_SCHEMA,
        "action": "preview-only",
        "status": "PREVIEW",
        "no_op": False,
        "same_root": False,
        "moved": False,
        "from": str(legacy),
        "to": str(target),
        "legacy_source": legacy_source,
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
    if plan.get("no_op"):
        lines.extend([plan["note"], ""])
        return "\n".join(lines)
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
            f"保持原位 {summary['excluded']}，"
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


def _validate_plan(plan: dict) -> tuple[Path, Path, list[tuple[dict, PurePosixPath, Path, Path]]]:
    """Validate an untrusted plan and return resolved roots and items."""
    if not isinstance(plan, dict) or plan.get("schema_version") != PREVIEW_SCHEMA:
        raise UpgradeError("MIGRATION_PLAN_INVALID", "schema")
    if plan.get("same_root") or plan.get("no_op"):
        raise UpgradeError("MIGRATION_SAME_ROOT", str(plan.get("to", "")))
    source_root = _absolute_root(plan.get("from"), "migration.from")
    target_root = _absolute_root(plan.get("to"), "migration.to")
    if _overlaps(target_root, source_root):
        raise UpgradeError("MIGRATION_OVERLAP", str(target_root))
    target_root = _validated_target(target_root)
    source_resolved = source_root.resolve(strict=False)
    items: list[tuple[dict, PurePosixPath, Path, Path]] = []
    seen: set[str] = set()
    for item in plan.get("items") or ():
        if not isinstance(item, dict):
            raise UpgradeError("MIGRATION_ITEM_INVALID", "item")
        relative = _safe_relative(item.get("relative_path"))
        key = relative.as_posix()
        if key in seen:
            raise UpgradeError("MIGRATION_ITEM_DUPLICATE", key)
        seen.add(key)
        source = Path(str(item.get("source") or ""))
        if not source.is_absolute():
            raise UpgradeError("MIGRATION_PATH_NOT_ABSOLUTE", key)
        try:
            source.resolve(strict=False).relative_to(source_resolved)
        except ValueError:
            raise UpgradeError("MIGRATION_SOURCE_OUTSIDE", key) from None
        target = target_root / Path(*relative.parts)
        if not paths.is_within(target.resolve(strict=False), target_root.resolve(strict=False)):
            raise UpgradeError("MIGRATION_TARGET_OUTSIDE", key)
        items.append((item, relative, source, target))
    return source_root, target_root, items


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
    decisions = dict(decisions or {})
    for key, value in decisions.items():
        if value not in {"replace", "keep"}:
            raise UpgradeError("MIGRATION_DECISION_INVALID", key)
    source_root, target_root, items = _validate_plan(plan)
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
                "from": str(source_root),
                "to": str(target_root),
                "items": len(items),
            },
        )
    try:
        for item, relative, source, target in items:
            decision = decisions.get(relative.as_posix())
            record = {
                "item": relative.as_posix(),
                "kind": item["kind"],
                "compatibility": item["compatibility"],
            }
            if item["compatibility"].startswith(_SKIP_PREFIXES) or item[
                "compatibility"
            ] in _SKIP_VALUES:
                record["status"] = (
                    "SKIPPED_EXCLUDED"
                    if item["compatibility"] in _SKIP_VALUES
                    else "SKIPPED_UNSUPPORTED"
                )
                results.append(record)
                continue
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
    pending_decisions = counts.get("NEEDS_DECISION", 0)
    return {
        "schema_version": "openthu-migration-result-v1",
        "dry_run": bool(dry_run),
        "ok": not failed,
        "complete": not failed and pending_decisions == 0,
        "pending_decisions": pending_decisions,
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
