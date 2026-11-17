"""Resource summary export.

The export is the user's local asset.  It contains only the selected fields,
never an account pseudonym, an absolute download directory or an unselected
course body.  A body excerpt is personal coursework, so including it requires
an explicit acknowledgement before anything is written.

The file is written through a temporary file and an atomic rename.  Writing
inside a Git work tree is refused: the repository is not a place for personal
course material.
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
    ResourceExportInRepository,
    ResourceInvalidArgument,
    ResourceLimitExceeded,
    ResourceWriteFailed,
)
from .index import default_clock
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import DownloadRecord, ResourceEntry

EXPORT_VERSION = "resource-export-v1"
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
OPTIONAL_FIELDS = ("content_sha256", "download_status", "body_excerpt", "body_state")
ALLOWED_FIELDS = BASE_FIELDS + OPTIONAL_FIELDS


def _inside_git_tree(path: Path) -> bool:
    current = path.resolve(strict=False)
    while True:
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
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
        os.replace(temporary, path)
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
    records: Iterable[DownloadRecord] = (),
    limits: ResourceLimits = DEFAULT_LIMITS,
    clock: Callable[[], datetime] = default_clock,
) -> dict[str, Any]:
    selected = list(BASE_FIELDS if fields is None else fields)
    if include_body:
        selected.append("body_excerpt")
    unknown = [name for name in selected if name not in ALLOWED_FIELDS]
    if unknown:
        raise ResourceInvalidArgument("export_field")
    selected = list(dict.fromkeys(selected))
    if "body_excerpt" in selected and not acknowledge_personal:
        raise ResourceConfirmationRequired("export_personal_content")
    if destination is None or not isinstance(destination, (str, os.PathLike)):
        raise ResourceInvalidArgument("destination")
    path = Path(destination).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise ResourceInvalidArgument("destination")
    if path.is_symlink():
        raise ResourceInvalidArgument("destination_symlink")
    if _inside_git_tree(path.parent):
        raise ResourceExportInRepository("git_tree")
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
            elif name == "content_sha256":
                row[name] = entry.content_sha256
            else:
                row[name] = entry.public().get(name)
        rows.append(row)
    payload = {
        "format_version": EXPORT_VERSION,
        "exported_at": now_iso(clock),
        "count": len(rows),
        "fields": selected,
        "personal_content_included": "body_excerpt" in selected,
        "rows": rows,
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, data)
    return {
        "format_version": EXPORT_VERSION,
        "path": str(path),
        "count": len(rows),
        "fields": selected,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "personal_content_included": payload["personal_content_included"],
        "exported_at": payload["exported_at"],
    }


__all__ = [
    "ALLOWED_FIELDS",
    "BASE_FIELDS",
    "EXPORT_VERSION",
    "OPTIONAL_FIELDS",
    "export_summary",
]
