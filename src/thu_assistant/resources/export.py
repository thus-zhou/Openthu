"""Resource summary export.

The export is the user's local asset.  It contains only the selected fields,
never an account pseudonym, an absolute download directory or an unselected
course body.  A body excerpt is personal coursework, so including it requires
an explicit acknowledgement before anything is written.

The file is written through a temporary file and an atomic publish.  An
existing export is not overwritten unless the caller passes ``overwrite=True``.
Writing inside a Git work tree is refused: the repository is not a place for
personal course material.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Iterable

from ..source_state import now_iso
from .errors import (
    ResourceConfirmationRequired,
    ResourceExportEmpty,
    ResourceExportExists,
    ResourceExportInRepository,
    ResourceInvalidArgument,
    ResourceLimitExceeded,
    ResourceWriteFailed,
    require_bool,
)
from .index import default_clock
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import DownloadRecord, ResourceEntry

EXPORT_VERSION = "resource-export-v2"
BASE_FIELDS = (
    "stable_resource_uid",
    "source",
    "course_id",
    "course_name",
    "semester",
    "title",
    "version",
    "size_bytes",
    "updated_at",
    "state",
    "checked_at",
    "source_link",
)
OPTIONAL_FIELDS = (
    "content_sha256",
    "download_status",
    "body_excerpt",
    "body_state",
    "body_truncated",
)
ALLOWED_FIELDS = BASE_FIELDS + OPTIONAL_FIELDS
_LINK_FALLBACK_ERRNOS = {
    getattr(os, "EPERM", 1),
    getattr(os, "EACCES", 13),
    getattr(os, "EOPNOTSUPP", 95),
    getattr(os, "ENOSYS", 38),
    getattr(os, "EXDEV", 18),
}


def _inside_git_tree(path: Path) -> bool:
    current = path.resolve(strict=False)
    while True:
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish(temporary: Path, target: Path, *, overwrite: bool) -> None:
    if overwrite:
        os.replace(temporary, target)
        return
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise ResourceExportExists("target_exists") from None
    except OSError as error:
        if getattr(error, "errno", None) not in _LINK_FALLBACK_ERRNOS:
            raise
        if target.exists() or target.is_symlink():
            raise ResourceExportExists("target_exists") from None
        os.replace(temporary, target)


def _atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.part")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        _publish(temporary, path, overwrite=overwrite)
        _fsync_directory(path.parent)
    except (ResourceExportExists, ResourceWriteFailed):
        raise
    except OSError:
        raise ResourceWriteFailed("export") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except OSError:
            pass


def _latest_statuses(records: Iterable[DownloadRecord]) -> dict[tuple[str, str], str]:
    latest: dict[tuple[str, str], str] = {}
    for record in records:
        key = (record.stable_resource_uid, record.version)
        if key not in latest:
            latest[key] = record.status
    return latest


def export_summary(
    entries: Iterable[ResourceEntry],
    *,
    destination: object,
    fields: Iterable[str] | None = None,
    include_body: bool = False,
    acknowledge_personal: bool = False,
    overwrite: bool = False,
    records: Iterable[DownloadRecord] = (),
    limits: ResourceLimits = DEFAULT_LIMITS,
    clock: Callable[[], datetime] = default_clock,
) -> dict[str, Any]:
    require_bool(include_body, "include_body")
    require_bool(acknowledge_personal, "acknowledge_personal")
    require_bool(overwrite, "overwrite")
    if fields is None:
        selected = list(BASE_FIELDS)
    elif isinstance(fields, (str, bytes)) or not isinstance(fields, Iterable):
        raise ResourceInvalidArgument("fields_type")
    else:
        selected = list(fields)
    if any(not isinstance(name, str) for name in selected):
        raise ResourceInvalidArgument("export_field")
    if include_body:
        selected.append("body_excerpt")
    unknown = [name for name in selected if name not in ALLOWED_FIELDS]
    if unknown:
        raise ResourceInvalidArgument("export_field")
    selected = list(dict.fromkeys(selected))
    if "body_excerpt" in selected and acknowledge_personal is not True:
        raise ResourceConfirmationRequired("export_personal_content")
    if "body_excerpt" in selected and "body_truncated" not in selected:
        selected.append("body_truncated")
    if "body_truncated" in selected and "body_excerpt" not in selected:
        raise ResourceInvalidArgument("body_truncated_requires_body")
    if destination is None or not isinstance(destination, (str, os.PathLike)):
        raise ResourceInvalidArgument("destination")
    path = Path(destination).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise ResourceInvalidArgument("destination")
    if path.is_symlink() or path.is_dir():
        raise ResourceInvalidArgument("destination")
    if _inside_git_tree(path.parent):
        raise ResourceExportInRepository("git_tree")
    if path.exists() and not overwrite:
        raise ResourceExportExists("target_exists")
    rows_in = list(entries)
    if len(rows_in) > limits.max_export_items:
        raise ResourceLimitExceeded("export_items")
    if not rows_in:
        raise ResourceExportEmpty("no_rows")
    statuses = _latest_statuses(records)
    rows = []
    for entry in rows_in:
        row: dict[str, Any] = {}
        for name in selected:
            if name == "source_link":
                row[name] = entry.source_url
            elif name == "checked_at":
                row[name] = entry.last_checked_at
            elif name == "download_status":
                row[name] = statuses.get((entry.stable_resource_uid, entry.version))
            elif name == "body_excerpt":
                row[name] = entry.body_excerpt
            elif name in ("body_state", "body_truncated"):
                row[name] = entry.public().get(name)
            elif name == "content_sha256":
                row[name] = entry.content_sha256
            else:
                row[name] = entry.public().get(name)
        if "body_excerpt" in selected:
            row["body_truncated"] = entry.body_truncated
        rows.append(row)
    semesters = sorted({entry.semester for entry in rows_in})
    link_values = [row.get("source_link") for row in rows] if "source_link" in selected else []
    payload = {
        "format_version": EXPORT_VERSION,
        "exported_at": now_iso(clock),
        "semester": semesters[0] if len(semesters) == 1 else None,
        "semesters": semesters,
        "count": len(rows),
        "fields": selected,
        "personal_content_included": "body_excerpt" in selected,
        "source_link_state": (
            "NOT_AVAILABLE_IN_THIS_VERSION"
            if link_values and all(value is None for value in link_values)
            else ("AVAILABLE" if link_values else "NOT_REQUESTED")
        ),
        "rows": rows,
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, data, overwrite=overwrite)
    return {
        "format_version": EXPORT_VERSION,
        "path": str(path),
        "semester": payload["semester"],
        "count": len(rows),
        "fields": selected,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "personal_content_included": payload["personal_content_included"],
        "source_link_state": payload["source_link_state"],
        "exported_at": payload["exported_at"],
    }


__all__ = [
    "ALLOWED_FIELDS",
    "BASE_FIELDS",
    "EXPORT_VERSION",
    "OPTIONAL_FIELDS",
    "export_summary",
]
