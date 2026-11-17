"""Normalised resource observations, index entries and download records.

The adapter hands over *observations*; it never hands over a downloaded file.
An entry is the indexed metadata for one ``(source record, version)`` pair.  A
download record is evidence that bytes were saved once, keyed by content hash
and a relative archive path; it never stores the absolute download directory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..source_state import normalize_timestamp, sanitize_url
from .errors import ResourceInvalidArgument
from .identity import (
    SOURCE_KINDS,
    display_title,
    parse_uid,
    validate_identifier,
    validate_semester,
    version_for,
)

BODY_STATES = ("READ", "NOT_READ", "UNRESOLVED", "UNKNOWN")
ENTRY_STATES = ("ACTIVE", "SUPERSEDED", "STALE", "MISSING")
DOWNLOAD_STATES = ("DOWNLOADED", "REUSED", "CONFLICT", "FAILED", "CANCELLED")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DOWNLOAD_ID = re.compile(r"^dl_[a-f0-9]{16,32}$")
_RELATIVE = re.compile(r"^[^\x00-\x1f\\:]+(?:/[^\x00-\x1f\\:]+)?$")


def _optional_int(value: Any, *, name: str, minimum: int = 0) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ResourceInvalidArgument(f"{name}_format")
    return value


def _optional_text(value: Any, *, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ResourceInvalidArgument(f"{name}_format")
    return value


@dataclass(frozen=True)
class ResourceCandidate:
    """One attachment or course file as observed from an existing read adapter."""

    stable_resource_uid: str
    source: str
    course_id: str
    course_name: str
    semester: str
    title: str
    size_bytes: int | None = None
    updated_at: str | None = None
    body_state: str = "NOT_READ"
    body_excerpt: str | None = None
    attachment_hint: bool | None = None
    source_url: str | None = None
    synthetic: bool = False
    version: str = ""
    source_record_id: str = ""
    max_title_chars: int = 240
    max_body_chars: int = 2000

    def __post_init__(self) -> None:
        source, record_id = parse_uid(self.stable_resource_uid)
        if source != self.source:
            raise ResourceInvalidArgument("uid_source_mismatch")
        if self.source not in SOURCE_KINDS:
            raise ResourceInvalidArgument("source_kind")
        validate_identifier(self.course_id, name="course_id")
        validate_semester(self.semester)
        if not isinstance(self.course_name, str) or len(self.course_name) > 240:
            raise ResourceInvalidArgument("course_name_format")
        title = display_title(self.title, limits_title_chars=self.max_title_chars)
        _optional_int(self.size_bytes, name="size_bytes")
        normalized = normalize_timestamp(self.updated_at, fallback=None)
        if self.updated_at is not None and normalized is None:
            raise ResourceInvalidArgument("updated_at_format")
        if self.body_state not in BODY_STATES:
            raise ResourceInvalidArgument("body_state")
        if self.body_state == "READ":
            if not isinstance(self.body_excerpt, str) or not self.body_excerpt.strip():
                raise ResourceInvalidArgument("body_excerpt_missing")
            excerpt = self.body_excerpt[: self.max_body_chars]
        else:
            if self.body_excerpt not in (None, ""):
                raise ResourceInvalidArgument("body_excerpt_without_read")
            excerpt = None
        if self.attachment_hint is not None and not isinstance(self.attachment_hint, bool):
            raise ResourceInvalidArgument("attachment_hint")
        if self.source_url is not None and not isinstance(self.source_url, str):
            raise ResourceInvalidArgument("source_url_format")
        if not isinstance(self.synthetic, bool):
            raise ResourceInvalidArgument("synthetic_format")
        version = self.version or version_for(normalized)
        if not isinstance(version, str) or not version or len(version) > 80:
            raise ResourceInvalidArgument("version_format")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "updated_at", normalized)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "body_excerpt", excerpt)
        object.__setattr__(self, "source_url", sanitize_url(self.source_url))
        object.__setattr__(self, "source_record_id", record_id)

    def key(self) -> tuple[str, str]:
        return self.stable_resource_uid, self.version


@dataclass(frozen=True)
class ResourceEntry:
    """One indexed ``(source record, version)`` pair, account bound."""

    stable_resource_uid: str
    source: str
    source_record_id: str
    course_id: str
    course_name: str
    semester: str
    title: str
    version: str
    size_bytes: int | None
    updated_at: str | None
    body_state: str
    body_excerpt: str | None
    attachment_hint: bool | None
    source_url: str | None
    synthetic: bool
    account_scope: str
    first_checked_at: str
    last_checked_at: str
    state: str = "ACTIVE"
    content_sha256: str | None = None
    version_note: str = ""

    def __post_init__(self) -> None:
        source, record_id = parse_uid(self.stable_resource_uid)
        if source != self.source or record_id != self.source_record_id:
            raise ResourceInvalidArgument("entry_identity_mismatch")
        validate_identifier(self.course_id, name="course_id")
        validate_semester(self.semester)
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 240:
            raise ResourceInvalidArgument("entry_title_format")
        if not isinstance(self.course_name, str) or len(self.course_name) > 240:
            raise ResourceInvalidArgument("entry_course_name_format")
        if self.state not in ENTRY_STATES:
            raise ResourceInvalidArgument("entry_state")
        if self.body_state not in BODY_STATES:
            raise ResourceInvalidArgument("body_state")
        if self.body_state == "READ" and not self.body_excerpt:
            raise ResourceInvalidArgument("body_excerpt_missing")
        if self.body_state != "READ" and self.body_excerpt not in (None, ""):
            raise ResourceInvalidArgument("body_excerpt_without_read")
        if self.content_sha256 is not None and not _SHA256.fullmatch(self.content_sha256):
            raise ResourceInvalidArgument("content_sha256_format")
        if not isinstance(self.account_scope, str) or not self.account_scope.startswith("acct_"):
            raise ResourceInvalidArgument("account_scope_format")
        if not isinstance(self.first_checked_at, str) or not isinstance(self.last_checked_at, str):
            raise ResourceInvalidArgument("checked_at_format")
        if not isinstance(self.version, str) or not self.version or len(self.version) > 80:
            raise ResourceInvalidArgument("version_format")

    @classmethod
    def from_candidate(
        cls,
        candidate: ResourceCandidate,
        *,
        account_scope: str,
        checked_at: str,
        version: str | None = None,
        state: str = "ACTIVE",
        version_note: str = "",
    ) -> "ResourceEntry":
        return cls(
            stable_resource_uid=candidate.stable_resource_uid,
            source=candidate.source,
            source_record_id=candidate.source_record_id,
            course_id=candidate.course_id,
            course_name=candidate.course_name,
            semester=candidate.semester,
            title=candidate.title,
            version=version or candidate.version,
            size_bytes=candidate.size_bytes,
            updated_at=candidate.updated_at,
            body_state=candidate.body_state,
            body_excerpt=candidate.body_excerpt,
            attachment_hint=candidate.attachment_hint,
            source_url=candidate.source_url,
            synthetic=candidate.synthetic,
            account_scope=account_scope,
            first_checked_at=checked_at,
            last_checked_at=checked_at,
            state=state,
            version_note=version_note,
        )

    def refreshed(self, candidate: ResourceCandidate, *, checked_at: str) -> "ResourceEntry":
        # A source that stops returning a reliable update instant does not make
        # the previously known version time wrong; keep it and let the caller
        # attach VERSION_RETAINED_SOURCE_UNRELIABLE.
        updated_at = candidate.updated_at if candidate.updated_at is not None else self.updated_at
        return replace(
            self,
            title=candidate.title,
            size_bytes=candidate.size_bytes,
            updated_at=updated_at,
            body_state=candidate.body_state,
            body_excerpt=candidate.body_excerpt,
            attachment_hint=candidate.attachment_hint,
            source_url=candidate.source_url,
            last_checked_at=checked_at,
            state="ACTIVE",
        )

    def public(self) -> dict[str, Any]:
        """Public metadata; no local path, no account pseudonym."""
        return {
            "stable_resource_uid": self.stable_resource_uid,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "semester": self.semester,
            "title": self.title,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "updated_at": self.updated_at,
            "body_state": self.body_state,
            "attachment_hint": self.attachment_hint,
            "source_url": self.source_url,
            "state": self.state,
            "first_checked_at": self.first_checked_at,
            "last_checked_at": self.last_checked_at,
            "content_sha256": self.content_sha256,
            "synthetic": self.synthetic,
        }

    def to_state(self) -> dict[str, Any]:
        return self.public() | {
            "account_scope": self.account_scope,
            "version_note": self.version_note,
        }

    @classmethod
    def from_state(cls, data: Any) -> "ResourceEntry":
        if not isinstance(data, dict):
            raise ResourceInvalidArgument("entry_state_shape")
        fields = {
            "stable_resource_uid", "source", "source_record_id", "course_id",
            "course_name", "semester", "title", "version", "size_bytes",
            "updated_at", "body_state", "body_excerpt", "attachment_hint",
            "source_url", "synthetic", "account_scope", "first_checked_at",
            "last_checked_at", "state", "content_sha256", "version_note",
        }
        if set(data) - fields:
            raise ResourceInvalidArgument("entry_state_unknown_field")
        values = {key: data.get(key) for key in fields}
        values["state"] = values.get("state") or "ACTIVE"
        values["version_note"] = values.get("version_note") or ""
        try:
            return cls(**values)
        except TypeError:
            raise ResourceInvalidArgument("entry_state_missing_field") from None


@dataclass(frozen=True)
class DownloadRecord:
    """One saved (or refused) batch item; persisted without an absolute path."""

    download_id: str
    stable_resource_uid: str
    version: str
    content_sha256: str | None
    byte_size: int | None
    archive_relative_path: str | None
    destination_id: str
    status: str
    written_at: str
    error_code: str | None = None
    media_asset_id: str | None = None
    duplicate_of: tuple[str, ...] = field(default_factory=tuple)
    verified: bool = False

    def __post_init__(self) -> None:
        parse_uid(self.stable_resource_uid)
        if not _DOWNLOAD_ID.fullmatch(self.download_id):
            raise ResourceInvalidArgument("download_id_format")
        if self.status not in DOWNLOAD_STATES:
            raise ResourceInvalidArgument("download_status")
        if self.content_sha256 is not None and not _SHA256.fullmatch(self.content_sha256):
            raise ResourceInvalidArgument("content_sha256_format")
        if self.byte_size is not None and (
            not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool) or self.byte_size < 0
        ):
            raise ResourceInvalidArgument("byte_size_format")
        if self.archive_relative_path is not None:
            if not _RELATIVE.fullmatch(self.archive_relative_path):
                raise ResourceInvalidArgument("archive_relative_path_format")
            if self.archive_relative_path.startswith("/") or ".." in Path(self.archive_relative_path).parts:
                raise ResourceInvalidArgument("archive_relative_path_format")
        if not isinstance(self.destination_id, str) or not re.fullmatch(r"dst_[a-f0-9]{12}", self.destination_id):
            raise ResourceInvalidArgument("destination_id_format")
        if not isinstance(self.written_at, str) or not self.written_at:
            raise ResourceInvalidArgument("written_at_format")
        if self.error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", self.error_code):
            raise ResourceInvalidArgument("error_code_format")
        if self.media_asset_id is not None and not re.fullmatch(r"media_[A-Za-z0-9_-]{8,64}", self.media_asset_id):
            raise ResourceInvalidArgument("media_asset_id_format")
        if not all(isinstance(item, str) for item in self.duplicate_of):
            raise ResourceInvalidArgument("duplicate_of_format")

    def public(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "stable_resource_uid": self.stable_resource_uid,
            "version": self.version,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "archive_relative_path": self.archive_relative_path,
            "destination_id": self.destination_id,
            "status": self.status,
            "written_at": self.written_at,
            "error_code": self.error_code,
            "media_asset_id": self.media_asset_id,
            "duplicate_of": list(self.duplicate_of),
            "verified": self.verified,
        }

    def to_state(self) -> dict[str, Any]:
        return self.public()

    @classmethod
    def from_state(cls, data: Any) -> "DownloadRecord":
        if not isinstance(data, dict):
            raise ResourceInvalidArgument("download_state_shape")
        fields = {
            "download_id", "stable_resource_uid", "version", "content_sha256",
            "byte_size", "archive_relative_path", "destination_id", "status",
            "written_at", "error_code", "media_asset_id", "duplicate_of", "verified",
        }
        if set(data) - fields:
            raise ResourceInvalidArgument("download_state_unknown_field")
        values = dict(data)
        values["duplicate_of"] = tuple(values.get("duplicate_of") or ())
        try:
            return cls(**{key: values.get(key) for key in fields})
        except TypeError:
            raise ResourceInvalidArgument("download_state_missing_field") from None


def empty_index(account_scope: str, semester: str) -> dict[str, Any]:
    return {
        "format_version": "resource-index-v1",
        "account_scope": account_scope,
        "semester": semester,
        "entries": [],
        "downloads": [],
        "checks": [],
        "updated_at": None,
    }


__all__ = [
    "BODY_STATES",
    "DOWNLOAD_STATES",
    "ENTRY_STATES",
    "DownloadRecord",
    "ResourceCandidate",
    "ResourceEntry",
    "empty_index",
]
