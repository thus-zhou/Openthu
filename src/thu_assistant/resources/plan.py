"""Batch download preview and execution.

A plan is a process-local, account-bound, expiring snapshot.  Preview reports
the file count, known and unknown sizes, the target directory, the target
relative path and the action for each item (new, reuse, conflict or review).
Execution requires explicit confirmation, calls the existing course download
interface through a narrow port and records every item separately: one failure
never turns the batch into a success and never discards the items that already
succeeded.

An existing file is never overwritten.  Identical content is reused; different
content is a conflict that keeps the original file.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..source_state import now_iso
from .archive import Archive, validate_destination
from .errors import (
    ResourceCancelled,
    ResourceConfirmationRequired,
    ResourceDownloadFailed,
    ResourceError,
    ResourceHashConflict,
    ResourceInvalidArgument,
    ResourceLimitExceeded,
    ResourceNotFound,
    ResourcePlanChanged,
    ResourcePlanExpired,
    ResourceStoreUnavailable,
)
from .index import ResourceIndex, destination_id
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import DownloadRecord, ResourceEntry

PLAN_VERSION = "resource-download-plan-v1"
TZ = timezone(timedelta(hours=8))
_ACTIONS = ("NEW", "REUSE", "VERIFY", "CONFLICT", "REVIEW_EXISTING")
_CONFLICT_POLICIES = ("REPORT", "ABORT")


@dataclass(frozen=True)
class PlanItem:
    stable_resource_uid: str
    version: str
    title: str
    course_id: str
    course_name: str
    source: str
    estimated_bytes: int | None
    archive_relative_path: str
    action: str
    existing_sha256: str | None = None
    reason: str | None = None
    fingerprint: tuple[str, str, str | None, str, int | None, str] = field(default=(), compare=False)

    def public(self) -> dict[str, Any]:
        return {
            "stable_resource_uid": self.stable_resource_uid,
            "version": self.version,
            "title": self.title,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "source": self.source,
            "estimated_bytes": self.estimated_bytes,
            "archive_relative_path": self.archive_relative_path,
            "action": self.action,
            "existing_sha256": self.existing_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DownloadPlan:
    plan_id: str
    account_scope: str
    semester: str
    destination: Path
    destination_id: str
    created_at: str
    expires_at: str
    conflict_policy: str
    items: tuple[PlanItem, ...]
    digest: str
    totals: dict[str, Any]
    blocked: bool

    def public(self) -> dict[str, Any]:
        return {
            "format_version": PLAN_VERSION,
            "plan_id": self.plan_id,
            "semester": self.semester,
            "destination": str(self.destination),
            "destination_id": self.destination_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "conflict_policy": self.conflict_policy,
            "blocked": self.blocked,
            "confirmation_required": True,
            "digest": self.digest,
            "totals": self.totals,
            "items": [item.public() for item in self.items],
        }


def _fingerprint(entry: ResourceEntry) -> tuple[str, str, str | None, str, int | None, str]:
    return (
        entry.stable_resource_uid,
        entry.version,
        entry.content_sha256,
        entry.title,
        entry.size_bytes,
        entry.state,
    )


def _digest(items: Sequence[PlanItem]) -> str:
    payload = [
        {
            "stable_resource_uid": item.stable_resource_uid,
            "version": item.version,
            "archive_relative_path": item.archive_relative_path,
            "action": item.action,
            "fingerprint": list(item.fingerprint),
        }
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_plan(
    entries: Iterable[ResourceEntry],
    *,
    account_scope: str,
    semester: str,
    destination: object,
    conflict_policy: str = "REPORT",
    verify_known: bool = False,
    limits: ResourceLimits = DEFAULT_LIMITS,
    clock: Callable[[], datetime] = lambda: datetime.now(TZ),
) -> DownloadPlan:
    if conflict_policy not in _CONFLICT_POLICIES:
        raise ResourceInvalidArgument("conflict_policy")
    root = validate_destination(destination)
    archive = Archive(root, limits=limits)
    seen: set[tuple[str, str]] = set()
    items: list[PlanItem] = []
    for entry in entries:
        key = (entry.stable_resource_uid, entry.version)
        if key in seen:
            continue
        seen.add(key)
        inspection = archive.inspect(entry)
        action = str(inspection["action"])
        if action in ("REUSE", "REVIEW_EXISTING") and verify_known:
            # Explicit content re-check: fetch and compare instead of trusting
            # the saved copy, so a same-version content change is detected and
            # an index lost while files remain can be rebuilt safely.
            action = "VERIFY"
        if action not in _ACTIONS:
            raise ResourceInvalidArgument("archive_action")
        items.append(
            PlanItem(
                stable_resource_uid=entry.stable_resource_uid,
                version=entry.version,
                title=entry.title,
                course_id=entry.course_id,
                course_name=entry.course_name,
                source=entry.source,
                estimated_bytes=entry.size_bytes,
                archive_relative_path=str(inspection["relative_path"]),
                action=action,
                existing_sha256=inspection.get("sha256"),  # type: ignore[arg-type]
                reason=inspection.get("reason"),  # type: ignore[arg-type]
                fingerprint=_fingerprint(entry),
            )
        )
        if len(items) > limits.max_plan_items:
            raise ResourceLimitExceeded("plan_items")
    if not items:
        raise ResourceInvalidArgument("empty_selection")
    new_items = [item for item in items if item.action == "NEW"]
    known = sum(item.estimated_bytes for item in new_items if item.estimated_bytes is not None)
    unknown = sum(1 for item in new_items if item.estimated_bytes is None)
    if known > limits.max_plan_known_bytes:
        raise ResourceLimitExceeded("plan_bytes")
    conflicts = sum(1 for item in items if item.action in ("CONFLICT", "REVIEW_EXISTING"))
    now = clock()
    created = now.isoformat(timespec="seconds")
    expires = (now + timedelta(seconds=limits.plan_ttl_seconds)).isoformat(timespec="seconds")
    totals = {
        "items": len(items),
        "new": len(new_items),
        "verify": sum(1 for item in items if item.action == "VERIFY"),
        "reuse": sum(1 for item in items if item.action == "REUSE"),
        "conflicts": conflicts,
        "estimated_bytes": known,
        "unknown_size_items": unknown,
        "estimate_complete": unknown == 0,
    }
    plan = DownloadPlan(
        plan_id="rplan_" + uuid.uuid4().hex,
        account_scope=account_scope,
        semester=semester,
        destination=root,
        destination_id=destination_id(str(root.resolve(strict=False))),
        created_at=created,
        expires_at=expires,
        conflict_policy=conflict_policy,
        items=tuple(items),
        digest=_digest(items),
        totals=totals,
        blocked=bool(conflicts and conflict_policy == "ABORT"),
    )
    return plan


def _record(
    *,
    uid: str,
    version: str,
    sha256: str | None,
    byte_size: int | None,
    relative: str | None,
    destination: str,
    status: str,
    written_at: str,
    error_code: str | None = None,
    media_asset_id: str | None = None,
    duplicate_of: tuple[str, ...] = (),
    verified: bool = False,
) -> DownloadRecord:
    return DownloadRecord(
        download_id="dl_" + uuid.uuid4().hex[:24],
        stable_resource_uid=uid,
        version=version,
        content_sha256=sha256,
        byte_size=byte_size,
        archive_relative_path=relative,
        destination_id=destination,
        status=status,
        written_at=written_at,
        error_code=error_code,
        media_asset_id=media_asset_id,
        duplicate_of=duplicate_of,
        verified=verified,
    )


def execute_plan(
    plan: DownloadPlan,
    *,
    confirm: bool,
    index: ResourceIndex,
    fetcher: Any,
    limits: ResourceLimits = DEFAULT_LIMITS,
    clock: Callable[[], datetime] = lambda: datetime.now(TZ),
    media: Any = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if confirm is not True:
        raise ResourceConfirmationRequired("download")
    if not isinstance(plan, DownloadPlan) or plan.account_scope != index.account_scope:
        raise ResourcePlanChanged("account")
    now = clock()
    expires = datetime.fromisoformat(plan.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=now.tzinfo or TZ)
    if now >= expires:
        raise ResourcePlanExpired("expired")
    if plan.semester != index.semester:
        raise ResourcePlanChanged("semester")
    if plan.digest != _digest(plan.items):
        raise ResourcePlanChanged("digest")
    if plan.blocked:
        raise ResourceHashConflict("plan_blocked")
    archive = Archive(plan.destination, limits=limits)
    destination = plan.destination_id
    written_at = now_iso(clock)
    results: list[dict[str, Any]] = []
    counts = {"downloaded": 0, "reused": 0, "conflict": 0, "failed": 0, "cancelled": 0}
    warnings: set[str] = set()
    cancelled = False
    for item in plan.items:
        if cancelled or (cancel is not None and cancel()):
            cancelled = True
            record = _record(
                uid=item.stable_resource_uid,
                version=item.version,
                sha256=None,
                byte_size=None,
                relative=None,
                destination=destination,
                status="CANCELLED",
                written_at=written_at,
                error_code=ResourceCancelled.code,
            )
            results.append({"item": item.public(), "result": record.public()})
            counts["cancelled"] += 1
            continue
        if item.action in ("CONFLICT", "REVIEW_EXISTING"):
            record = _record(
                uid=item.stable_resource_uid,
                version=item.version,
                sha256=item.existing_sha256,
                byte_size=None,
                relative=item.archive_relative_path,
                destination=destination,
                status="CONFLICT",
                written_at=written_at,
                error_code=ResourceHashConflict.code,
                verified=False,
            )
            try:
                index.add_download(record, persist=False)
            except ResourceStoreUnavailable:
                raise
            results.append({"item": item.public(), "result": record.public()})
            counts["conflict"] += 1
            continue
        try:
            current = index.find(item.stable_resource_uid, version=item.version, include_superseded=True)
            if _fingerprint(current) != item.fingerprint:
                raise ResourcePlanChanged("entry_changed")
            if item.action == "REUSE":
                inspected = archive.inspect(
                    current, expected_sha256=item.existing_sha256 or current.content_sha256
                )
                if inspected["action"] != "REUSE":
                    raise ResourcePlanChanged("target_changed")
                record = _record(
                    uid=current.stable_resource_uid,
                    version=current.version,
                    sha256=str(inspected["sha256"]),
                    byte_size=int(inspected["bytes"]) if inspected["bytes"] is not None else None,
                    relative=item.archive_relative_path,
                    destination=destination,
                    status="REUSED",
                    written_at=written_at,
                    verified=True,
                )
                index.add_download(record, persist=False)
                results.append(
                    {
                        "item": item.public(),
                        "result": record.public(),
                        "source_checked_at": current.last_checked_at,
                        "source_state": current.state,
                    }
                )
                counts["reused"] += 1
                continue
            if fetcher is None:
                raise ResourceDownloadFailed("no_downloader")
            payload = fetcher.fetch(current)
            if not isinstance(payload, bytes):
                raise ResourceDownloadFailed("fetch_type")
            if not payload:
                raise ResourceDownloadFailed("empty")
            if len(payload) > limits.max_attachment_bytes:
                raise ResourceLimitExceeded("attachment_bytes")
            digest = hashlib.sha256(payload).hexdigest()
            current, bumped = index.apply_content_hash(
                current, digest, checked_at=written_at, persist=False
            )
            if bumped:
                warnings.add("CONTENT_CHANGED_NEW_VERSION")
            outcome = archive.write(current, payload)
            duplicates = tuple(
                sorted(
                    {
                        other.stable_resource_uid + "@" + other.version
                        for other in index.downloads()
                        if other.content_sha256 == digest
                        and other.status in ("DOWNLOADED", "REUSED")
                        and (other.stable_resource_uid, other.version)
                        != (current.stable_resource_uid, current.version)
                    }
                )
            )
            media_id = None
            if media is not None:
                try:
                    reference = media.register_downloaded_attachment(
                        archive.target(current),
                        kind="ATTACHMENT",
                        account_scope=index.account_scope,
                        task_id=plan.plan_id,
                        purpose="COURSE_RESOURCE",
                        display_name=current.title,
                        source_evidence_id="learn.resource_download",
                        synthetic=current.synthetic,
                    )
                    media_id = getattr(reference, "media_id", None)
                except Exception:
                    warnings.add("MEDIA_REGISTRATION_FAILED")
            record = _record(
                uid=current.stable_resource_uid,
                version=current.version,
                sha256=digest,
                byte_size=len(payload),
                relative=str(outcome["relative_path"]),
                destination=destination,
                status="REUSED" if outcome["status"] == "reused" else "DOWNLOADED",
                written_at=written_at,
                media_asset_id=media_id,
                duplicate_of=duplicates,
                verified=bool(outcome.get("verified")),
            )
            index.add_download(record, persist=False)
            results.append(
                {
                    "item": item.public(),
                    "result": record.public(),
                    "source_checked_at": current.last_checked_at,
                    "source_state": current.state,
                }
            )
            counts["reused" if outcome["status"] == "reused" else "downloaded"] += 1
        except (ResourcePlanChanged, ResourcePlanExpired, ResourceConfirmationRequired, ResourceNotFound):
            # A plan-wide change is not one item's transfer failure: abort the
            # batch before any further write and let the caller re-preview.
            raise
        except ResourceCancelled:
            cancelled = True
            record = _record(
                uid=item.stable_resource_uid,
                version=item.version,
                sha256=None,
                byte_size=None,
                relative=None,
                destination=destination,
                status="CANCELLED",
                written_at=written_at,
                error_code=ResourceCancelled.code,
            )
            results.append({"item": item.public(), "result": record.public()})
            counts["cancelled"] += 1
        except ResourceError as error:
            record = _record(
                uid=item.stable_resource_uid,
                version=item.version,
                sha256=None,
                byte_size=None,
                relative=None,
                destination=destination,
                status="FAILED",
                written_at=written_at,
                error_code=error.code,
            )
            try:
                index.add_download(record, persist=False)
            except ResourceStoreUnavailable:
                raise
            results.append({"item": item.public(), "result": record.public()})
            counts["failed"] += 1
        except Exception:
            record = _record(
                uid=item.stable_resource_uid,
                version=item.version,
                sha256=None,
                byte_size=None,
                relative=None,
                destination=destination,
                status="FAILED",
                written_at=written_at,
                error_code=ResourceDownloadFailed.code,
            )
            try:
                index.add_download(record, persist=False)
            except ResourceStoreUnavailable:
                raise
            results.append({"item": item.public(), "result": record.public()})
            counts["failed"] += 1
    index.save()
    complete = not counts["failed"] and not counts["conflict"] and not counts["cancelled"]
    return {
        "format_version": PLAN_VERSION,
        "plan_id": plan.plan_id,
        "destination": str(plan.destination),
        "destination_id": destination,
        "checked_at": written_at,
        "status": "CANCELLED" if cancelled else ("PARTIAL" if not complete else "OK"),
        "complete": complete,
        "counts": counts,
        "warnings": sorted(warnings),
        "items": results,
    }


__all__ = [
    "DownloadPlan",
    "PLAN_VERSION",
    "PlanItem",
    "build_plan",
    "execute_plan",
]
