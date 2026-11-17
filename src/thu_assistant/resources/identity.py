"""Stable resource identity and safe archive component names.

A resource is identified by its source record, never by its display title.  The
public identifier follows the frozen ``stable-identity-samples-v1`` shape
(``learn:FILE:<source record id>``).  A version is the reliable source update
instant, or ``UNKNOWN`` when the source gives only a date or no time; the index
then derives a bounded sequence token instead of pretending to know an order.

Archive names are built from a sanitised course name, a sanitised title and a
short token over ``(stable uid, version)``.  A remote title can therefore never
add a parent directory, an absolute path, a drive letter, a reserved Windows
device name or a trailing dot that Windows silently drops.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import PurePosixPath

from ..source_state import normalize_timestamp
from .errors import ResourceInvalidArgument, ResourceSourceUnsupported, ResourceTitleUnsafe

SOURCE_KINDS = ("COURSE_FILE", "NOTICE_ATTACHMENT", "ASSIGNMENT_ATTACHMENT")
SOURCE_PREFIX = {
    "COURSE_FILE": "FILE",
    "NOTICE_ATTACHMENT": "NOTICE_ATTACHMENT",
    "ASSIGNMENT_ATTACHMENT": "ASSIGNMENT_ATTACHMENT",
}
PREFIX_SOURCE = {value: key for key, value in SOURCE_PREFIX.items()}

_UID = re.compile(r"^learn:(FILE|NOTICE_ATTACHMENT|ASSIGNMENT_ATTACHMENT):([A-Za-z0-9_:.-]{1,200})$")
_IDENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SEMESTER = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_WHITESPACE = re.compile(r"\s+")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED.update(f"COM{index}" for index in range(1, 10))
_RESERVED.update(f"LPT{index}" for index in range(1, 10))
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def validate_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise ResourceInvalidArgument(f"{name}_format")
    return value


def validate_semester(value: object) -> str:
    if not isinstance(value, str) or not _SEMESTER.fullmatch(value):
        raise ResourceInvalidArgument("semester_format")
    return value


def stable_uid(source: str, source_record_id: object) -> str:
    if source not in SOURCE_KINDS:
        raise ResourceSourceUnsupported("source_kind")
    if not isinstance(source_record_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_:.-]{1,200}", source_record_id
    ):
        raise ResourceInvalidArgument("source_record_id_format")
    return f"learn:{SOURCE_PREFIX[source]}:{source_record_id}"


def parse_uid(value: object) -> tuple[str, str]:
    """Return ``(source_kind, source_record_id)`` for a public identifier."""
    if not isinstance(value, str):
        raise ResourceInvalidArgument("stable_resource_uid_format")
    match = _UID.fullmatch(value)
    if match is None:
        raise ResourceInvalidArgument("stable_resource_uid_format")
    return PREFIX_SOURCE[match[1]], match[2]


def version_for(updated_at: object) -> str:
    """A reliable update instant, otherwise the explicit ``UNKNOWN`` token.

    A date-only value is not a version: the content can change twice inside the
    same day.  The index keeps such an entry addressable through a derived
    sequence token, never by inventing a midnight timestamp.
    """
    normalized = normalize_timestamp(updated_at, fallback=None)
    return normalized if normalized is not None else "UNKNOWN"


def display_title(value: object, *, limits_title_chars: int) -> str:
    if not isinstance(value, str):
        raise ResourceTitleUnsafe("title_type")
    text = unicodedata.normalize("NFC", value)
    text = _UNSAFE.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip(" .")
    if not text:
        raise ResourceTitleUnsafe("title_empty")
    if len(text) > limits_title_chars:
        text = text[: limits_title_chars].rstrip(" .")
    if not text:
        raise ResourceTitleUnsafe("title_empty")
    return text


def safe_component(value: object, *, fallback: str, max_length: int) -> str:
    """One path segment, never a relative path or a device name."""
    text = value if isinstance(value, str) else ""
    text = unicodedata.normalize("NFC", text)
    text = _UNSAFE.sub("_", text)
    text = _WHITESPACE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    # Windows treats ``CON.txt`` and ``CON`` alike; check the part before the
    # first dot.  A leading underscore keeps the name readable and safe.
    if text.split(".", 1)[0].strip().upper() in _RESERVED:
        text = "_" + text
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    if not text or text in (".", ".."):
        text = fallback
    return text


def resource_token(uid: str, version: str) -> str:
    return hashlib.sha256(f"{uid}\0{version}".encode("utf-8")).hexdigest()[:10]


def archive_relative_path(
    *,
    uid: str,
    version: str,
    course_id: str,
    course_name: str,
    title: str,
    max_title_chars: int = 240,
) -> str:
    """A stable relative path inside the user's chosen download directory."""
    parse_uid(uid)
    course_component = safe_component(
        course_name, fallback="course", max_length=48
    ) + "__" + safe_component(course_id, fallback="id", max_length=40)
    safe_title = display_title(title, limits_title_chars=max_title_chars)
    suffix = PurePosixPath(safe_title).suffix
    if _EXTENSION.fullmatch(suffix):
        stem = safe_title[: -len(suffix)]
        extension = suffix.lower()
    else:
        stem = safe_title
        extension = ""
    stem = safe_component(stem, fallback="resource", max_length=80)
    token = resource_token(uid, version)
    file_name = f"{stem} [{token}]{extension}"
    relative = PurePosixPath(course_component, file_name)
    if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
        raise ResourceTitleUnsafe("archive_escape")
    if len(relative.as_posix()) > 240:
        raise ResourceTitleUnsafe("archive_too_long")
    return relative.as_posix()


__all__ = [
    "PREFIX_SOURCE",
    "SOURCE_KINDS",
    "SOURCE_PREFIX",
    "archive_relative_path",
    "display_title",
    "parse_uid",
    "resource_token",
    "safe_component",
    "stable_uid",
    "validate_identifier",
    "validate_semester",
    "version_for",
]
