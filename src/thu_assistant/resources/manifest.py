"""Stable resource manifest, pagination and duplicate reporting.

The manifest answers three questions without inventing identity:

* two files with the same title but different source records stay separate;
* one source record with several versions keeps each version addressable;
* identical bytes that arrived from different sources are reported as a group,
  never merged into one entry and never overwritten.

No absolute path appears here; a download record contributes only its content
hash, size, status and relative archive path.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .errors import ResourceInvalidArgument
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import DownloadRecord, ResourceEntry

MANIFEST_VERSION = "resource-manifest-v2"


def _version_row(entry: ResourceEntry) -> dict[str, Any]:
    return {
        "version": entry.version,
        "base_version": entry.base_version,
        "state": entry.state,
        "title": entry.title,
        "source": entry.source,
        "updated_at": entry.updated_at,
        "size_bytes": entry.size_bytes,
        "content_sha256": entry.content_sha256,
        "body_state": entry.body_state,
        "body_truncated": entry.body_truncated,
        "last_checked_at": entry.last_checked_at,
        "version_note": entry.version_note,
    }


def same_title_groups(entries: Iterable[ResourceEntry]) -> list[dict[str, Any]]:
    groups: dict[str, list[ResourceEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.title.casefold()].append(entry)
    result = []
    for rows in groups.values():
        uids = sorted({entry.stable_resource_uid for entry in rows})
        if len(uids) < 2:
            continue
        result.append(
            {
                "title": rows[0].title,
                "stable_resource_uids": uids,
                "reason": "SAME_TITLE_DIFFERENT_SOURCE_RECORD",
            }
        )
    result.sort(key=lambda item: (item["title"].casefold(), item["stable_resource_uids"]))
    return result


def content_groups(records: Iterable[DownloadRecord]) -> list[dict[str, Any]]:
    groups: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for record in records:
        if record.content_sha256 is None or record.status not in ("DOWNLOADED", "REUSED"):
            continue
        groups[record.content_sha256].add((record.stable_resource_uid, record.version))
    result = []
    for digest, keys in groups.items():
        if len(keys) < 2:
            continue
        result.append(
            {
                "content_sha256": digest,
                "resources": [
                    {"stable_resource_uid": uid, "version": version}
                    for uid, version in sorted(keys)
                ],
                "reason": "SAME_CONTENT_DIFFERENT_SOURCE_OR_VERSION",
            }
        )
    result.sort(key=lambda item: item["content_sha256"])
    return result


def build_manifest(
    entries: Iterable[ResourceEntry],
    records: Iterable[DownloadRecord] = (),
    *,
    semester: str | None = None,
    offset: int = 0,
    limit: int = 200,
    limits: ResourceLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ResourceInvalidArgument("offset")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= limits.max_manifest_limit:
        raise ResourceInvalidArgument("limit")
    rows = list(entries)
    if semester is not None:
        rows = [entry for entry in rows if entry.semester == semester]
    by_uid: dict[str, list[ResourceEntry]] = defaultdict(list)
    for entry in rows:
        by_uid[entry.stable_resource_uid].append(entry)
    resources = []
    multiple_versions = []
    for uid in sorted(by_uid):
        versions = sorted(by_uid[uid], key=lambda entry: (entry.version, entry.last_checked_at))
        active = next((entry for entry in versions if entry.state == "ACTIVE"), None)
        first = versions[0]
        resources.append(
            {
                "stable_resource_uid": uid,
                "source": first.source,
                "course_id": first.course_id,
                "course_name": first.course_name,
                "semester": first.semester,
                "title": first.title,
                "active_version": active.version if active else None,
                "versions": [_version_row(entry) for entry in versions],
            }
        )
        if len({entry.version for entry in versions}) > 1:
            multiple_versions.append(
                {
                    "stable_resource_uid": uid,
                    "versions": [entry.version for entry in versions],
                }
            )
    total = len(resources)
    page = resources[offset : offset + limit]
    return {
        "format_version": MANIFEST_VERSION,
        "semester": semester,
        "count": total,
        "entries": len(rows),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + len(page) if offset + len(page) < total else None,
        "snapshot_complete": offset + len(page) >= total,
        "resources": page,
        "same_title_groups": same_title_groups(rows),
        "multiple_versions": multiple_versions,
        "content_duplicate_groups": content_groups(records),
        "notes": {
            "identity": "stable_resource_uid is the source record, not the title",
            "versions": "one entry per source version; superseded versions remain addressable",
            "content": "identical bytes are grouped, never merged across sources",
        },
    }


__all__ = [
    "MANIFEST_VERSION",
    "build_manifest",
    "content_groups",
    "same_title_groups",
]
