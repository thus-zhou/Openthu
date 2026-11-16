"""Media reference records, scoped resolution and optional encrypted state.

A reference is not a download URL and not a reusable capability. It is bound to
one account scope, one task authorization, one purpose and one expiry instant.
Resolving it requires all of them to match; a different request must register
the file again. The local path lives only in this process table and in the
optional encrypted record, never in the public descriptor.

The descriptor produced by ``to_reference`` is the frozen
``media-reference-v1`` shape. The richer manifest entry (purpose, source kind,
transform note) stays internal, because the contract forbids extra keys.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    MediaInvalidArgument,
    MediaReferenceAccountMismatch,
    MediaReferenceCorrupt,
    MediaReferenceExpired,
    MediaReferenceNotFound,
    MediaReferencePurposeMismatch,
    MediaReferenceRevoked,
    MediaReferenceTaskMismatch,
    MediaStateUnavailable,
)
from .limits import MediaLimits

KINDS = ("IMAGE", "ATTACHMENT")
PURPOSES = ("MODEL_INPUT", "COURSE_RESOURCE", "USER_REVIEW")
SOURCE_KINDS = ("USER_SELECTED", "DOWNLOADED_ATTACHMENT", "SYNTHETIC")
RETENTIONS = ("SESSION", "EXPLICIT_DELETE", "PROJECT_CACHE")
ACCOUNT_DOMAINS = ("CAMPUS", "MODEL", "LOCAL", "SYSTEM", "UNKNOWN")
SERVICE_DOMAINS = ("INFO", "LEARN", "LIBRARY", "SPORTS", "MADMODEL", "LOCAL", "UNKNOWN")

_ID = re.compile(r"^media_[A-Za-z0-9_-]{8,64}$")
_SCOPE = re.compile(r"^(?:none|acct_[A-Za-z0-9_-]{16,64})$")
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_EVIDENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]+/[a-z0-9][a-z0-9.+-]+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def validate_scope(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SCOPE.fullmatch(value):
        raise MediaInvalidArgument(f"{name}_format")
    return value


def validate_task(value: object) -> str:
    if not isinstance(value, str) or not _TASK.fullmatch(value):
        raise MediaInvalidArgument("task_id_format")
    return value


def validate_choice(value: object, *, name: str, choices: Iterable[str]) -> str:
    allowed = tuple(choices)
    if value not in allowed:
        raise MediaInvalidArgument(f"{name}_value")
    return str(value)


@dataclass
class MediaReference:
    media_id: str
    kind: str
    mime_type: str
    byte_size: int
    sha256: str
    width: int | None
    height: int | None
    pixel_count: int | None
    source_evidence_id: str
    account_domain: str
    service_domain: str
    account_scope: str
    task_id: str
    purpose: str
    source_kind: str
    created_at: datetime
    expires_at: datetime | None
    retention: str
    display_name: str
    synthetic: bool
    external_transfer_allowed: bool
    path: Path
    source_sha256: str
    source_mime: str
    transform: dict[str, Any] = field(default_factory=dict)
    state: str = "ACTIVE"
    uses: int = 0

    def to_reference(self) -> dict[str, Any]:
        """The frozen public descriptor; no path, no bytes, no extra keys."""
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "source_evidence_id": self.source_evidence_id,
            "storage_purpose": "MEDIA",
            "account_domain": self.account_domain,
            "service_domain": self.service_domain,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "external_transfer_allowed": self.external_transfer_allowed,
            "retention": self.retention,
            "display_name": self.display_name,
            "synthetic": self.synthetic,
        }

    def to_manifest_entry(self) -> dict[str, Any]:
        """Internal list the user can inspect before anything is sent."""
        return {
            "asset_id": self.media_id,
            "account_scope": self.account_scope,
            "purpose": self.purpose,
            "kind": self.kind,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "retention": self.retention,
            "source": {
                "kind": self.source_kind,
                "source_evidence_id": self.source_evidence_id,
                "display_name": self.display_name,
                "synthetic": self.synthetic,
                "source_sha256": self.source_sha256,
                "source_mime": self.source_mime,
            },
            "transform": dict(self.transform),
            "external_transfer_allowed": self.external_transfer_allowed,
            "state": self.state,
        }

    def state_entry(self) -> dict[str, Any]:
        """Encrypted persistence form: relative file name, no absolute path."""
        payload = self.to_manifest_entry()
        payload.update(
            {
                "account_domain": self.account_domain,
                "service_domain": self.service_domain,
                "task_id": self.task_id,
                "file_name": self.path.name,
                "state": self.state,
                "uses": self.uses,
            }
        )
        return payload

    @classmethod
    def from_state_entry(cls, payload: dict[str, Any], *, root: Path) -> "MediaReference":
        source = payload.get("source") or {}
        return cls(
            media_id=str(payload["asset_id"]),
            kind=str(payload["kind"]),
            mime_type=str(payload["mime_type"]),
            byte_size=int(payload["byte_size"]),
            sha256=str(payload["sha256"]),
            width=payload.get("width"),
            height=payload.get("height"),
            pixel_count=payload.get("pixel_count"),
            source_evidence_id=str(source.get("source_evidence_id", "")),
            account_domain=str(payload["account_domain"]),
            service_domain=str(payload["service_domain"]),
            account_scope=str(payload["account_scope"]),
            task_id=str(payload["task_id"]),
            purpose=str(payload["purpose"]),
            source_kind=str(source.get("kind", "USER_SELECTED")),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            expires_at=(
                datetime.fromisoformat(str(payload["expires_at"]))
                if payload.get("expires_at")
                else None
            ),
            retention=str(payload["retention"]),
            display_name=str(source.get("display_name", "unnamed")),
            synthetic=bool(source.get("synthetic", False)),
            external_transfer_allowed=bool(payload.get("external_transfer_allowed")),
            path=Path(root) / str(payload["file_name"]),
            source_sha256=str(source.get("source_sha256", "")),
            source_mime=str(source.get("source_mime", "")),
            transform=dict(payload.get("transform") or {}),
            state=str(payload.get("state", "ACTIVE")),
            uses=int(payload.get("uses", 0)),
        )


class ReferenceRegistry:
    """In-process table of live references."""

    def __init__(self) -> None:
        self._items: dict[str, MediaReference] = {}
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, reference: MediaReference) -> MediaReference:
        if not _ID.fullmatch(reference.media_id):
            raise MediaInvalidArgument("media_id_format")
        if reference.mime_type and not _MIME.fullmatch(reference.mime_type):
            raise MediaInvalidArgument("mime_format")
        if not _HEX64.fullmatch(reference.sha256):
            raise MediaInvalidArgument("sha256_format")
        with self._lock:
            if reference.media_id in self._items:
                raise MediaInvalidArgument("duplicate_media_id")
            self._items[reference.media_id] = reference
        return reference

    def restore(self, reference: MediaReference) -> bool:
        """Reload a persisted entry; missing derived files are dropped."""
        with self._lock:
            if reference.media_id in self._items:
                return False
            if not reference.path.is_file():
                return False
            self._items[reference.media_id] = reference
        return True

    def get(self, media_id: object) -> MediaReference:
        if not isinstance(media_id, str) or not _ID.fullmatch(media_id):
            raise MediaInvalidArgument("media_id_format")
        with self._lock:
            reference = self._items.get(media_id)
        if reference is None:
            raise MediaReferenceNotFound("unknown_id")
        return reference

    def resolve(
        self,
        media_id: object,
        *,
        account_scope: str,
        task_id: str,
        purpose: str | None,
        now: datetime,
    ) -> MediaReference:
        reference = self.get(media_id)
        if reference.account_scope != account_scope:
            raise MediaReferenceAccountMismatch("account")
        if reference.task_id != task_id:
            raise MediaReferenceTaskMismatch("task")
        if purpose is not None and reference.purpose != purpose:
            raise MediaReferencePurposeMismatch("purpose")
        if reference.state == "REVOKED":
            raise MediaReferenceRevoked("revoked")
        if reference.expires_at is not None and now >= reference.expires_at:
            reference.state = "EXPIRED"
            raise MediaReferenceExpired("elapsed")
        return reference

    def touch(self, reference: MediaReference) -> None:
        reference.uses += 1

    def revoke_task(self, task_id: str) -> list[MediaReference]:
        with self._lock:
            selected = [
                item for item in self._items.values() if item.task_id == task_id
            ]
        for item in selected:
            item.state = "REVOKED"
        return selected

    def remove(self, media_id: str) -> None:
        with self._lock:
            self._items.pop(media_id, None)

    def retained_paths(self) -> set[Path]:
        """Derived files whose retention survives this process."""
        with self._lock:
            return {
                item.path
                for item in self._items.values()
                if item.state == "ACTIVE" and item.retention != "SESSION"
            }

    def active_paths(self) -> set[Path]:
        with self._lock:
            return {
                item.path for item in self._items.values() if item.state == "ACTIVE"
            }

    def snapshot(self) -> list[MediaReference]:
        with self._lock:
            return list(self._items.values())

    def entries(self, *, account_scope: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())
        if account_scope is not None:
            items = [item for item in items if item.account_scope == account_scope]
        return [item.to_manifest_entry() for item in items]

    def state_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items.values())
        return [
            item.state_entry()
            for item in items
            if item.state != "REVOKED" and item.retention != "SESSION"
        ]


class MediaStateStore:
    """Optional encrypted persistence for references.

    Paths are stored as file names inside the controlled temporary root; the
    absolute root is never written. The secure store binds the account into the
    authenticated data, so loading another account's media state fails
    authentication instead of silently returning references.
    """

    VERSION = 1

    def __init__(
        self,
        store: Any,
        *,
        record_id: str = "media-references",
        account_domain: str = "CAMPUS",
        service_domain: str = "LEARN",
    ) -> None:
        self.store = store
        self.record_id = record_id
        self.account_domain = account_domain
        self.service_domain = service_domain

    def save(self, entries: list[dict[str, Any]], *, account: str) -> int:
        payload = {"version": self.VERSION, "entries": entries}
        try:
            return int(
                self.store.write_json(
                    self.record_id,
                    payload,
                    purpose="MEDIA",
                    account_domain=self.account_domain,
                    service_domain=self.service_domain,
                    account=account,
                )
            )
        except Exception as error:  # secure_store raises its own taxonomy
            raise MediaStateUnavailable(type(error).__name__) from None

    def load(self, *, account: str) -> list[dict[str, Any]]:
        try:
            payload = self.store.read_json(
                self.record_id,
                purpose="MEDIA",
                account_domain=self.account_domain,
                service_domain=self.service_domain,
                account=account,
            )
        except Exception as error:
            if type(error).__name__ in (
                "SecureRecordNotFound",
                "FileNotFoundError",
            ):
                return []
            raise MediaStateUnavailable(type(error).__name__) from None
        if not isinstance(payload, dict) or payload.get("version") != self.VERSION:
            raise MediaStateUnavailable("version")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise MediaStateUnavailable("entries")
        return [item for item in entries if isinstance(item, dict)]


def expiry_for(now: datetime, limits: MediaLimits, *, seconds: int | None = None) -> datetime:
    try:
        ttl = limits.reference_ttl_seconds if seconds is None else int(seconds)
    except (TypeError, ValueError):
        raise MediaInvalidArgument("expiry") from None
    if ttl <= 0:
        raise MediaInvalidArgument("expiry")
    return now + timedelta(seconds=ttl)


def verify_bytes(reference: MediaReference, data: bytes) -> None:
    import hashlib

    if len(data) != reference.byte_size:
        raise MediaReferenceCorrupt("size")
    if hashlib.sha256(data).hexdigest() != reference.sha256:
        raise MediaReferenceCorrupt("sha256")


__all__ = [
    "ACCOUNT_DOMAINS",
    "KINDS",
    "MediaReference",
    "MediaStateStore",
    "PURPOSES",
    "RETENTIONS",
    "ReferenceRegistry",
    "SERVICE_DOMAINS",
    "SOURCE_KINDS",
    "expiry_for",
    "utc_now",
    "validate_choice",
    "validate_scope",
    "validate_task",
    "verify_bytes",
]
