"""Content-signature detection for local media.

A browser, a web page or a model can all claim a MIME type; none of them is
evidence. This module decides from the bytes themselves, then checks that the
file extension and any declared MIME agree. Disagreement is a rejection, not a
warning: the documented attack is an image extension on non-image content.

Only the first 4 KiB are inspected. That is enough for every container format
accepted here and keeps detection bounded regardless of file size.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .errors import MediaContentForbidden, MediaContentMismatch, MediaTypeUnsupported
from .limits import MediaLimits

IMAGE = "IMAGE"
DOCUMENT = "DOCUMENT"
ARCHIVE = "ARCHIVE"
TEXT = "TEXT"
MARKUP = "MARKUP"
EXECUTABLE = "EXECUTABLE"
UNKNOWN = "UNKNOWN"

OOXML_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OOXML_SLIDES = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OOXML_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OOXML_TYPES = (OOXML_WORD, OOXML_SLIDES, OOXML_SHEET)

_EXTENSION_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".docx": OOXML_WORD,
    ".pptx": OOXML_SLIDES,
    ".xlsx": OOXML_SHEET,
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".exe": "application/x-msdownload",
    ".sh": "text/x-shellscript",
}

_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "text/x-markdown": "text/markdown",
    "application/x-zip-compressed": "application/zip",
    "application/x-pdf": "application/pdf",
}

_GENERIC_MIME = {"application/octet-stream", ""}

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
    (b"\x1f\x8b\x08", "application/gzip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
    (b"\x7fELF", "application/x-elf"),
)

_MARKUP = re.compile(r"<\s*(?:!doctype\s+html|html|head|body|script|svg|iframe)\b", re.I)
_PRINTABLE = re.compile(r"^[\x09\x0a\x0d\x20-\x7e\u00a0-\uffff]+$")


@dataclass(frozen=True)
class Detection:
    mime: str | None
    family: str
    evidence: str

    @property
    def known(self) -> bool:
        return self.mime is not None


def normalize_mime(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).split(";", 1)[0].strip().lower()
    if not text:
        return None
    return _MIME_ALIASES.get(text, text)


def family_of(mime: str | None) -> str:
    if not mime:
        return UNKNOWN
    if mime.startswith("image/"):
        return IMAGE
    if mime in ("text/html", "image/svg+xml", "text/javascript", "application/xml",
                "text/xml"):
        return MARKUP
    if mime in ("application/x-elf", "application/x-msdownload", "text/x-shellscript",
                "application/x-mach-binary"):
        return EXECUTABLE
    if mime in ("application/pdf", "application/x-ole-storage"):
        return DOCUMENT
    if mime in ("application/zip", "application/gzip") or mime in OOXML_TYPES:
        return ARCHIVE
    if mime.startswith("text/") or mime == "application/json":
        return TEXT
    return UNKNOWN


def mime_token(mime: str | None) -> str:
    """A log-safe spelling of a MIME type (no path separator)."""
    return (mime or "unknown").replace("/", "-")


def expected_from_name(name: str) -> str | None:
    lowered = str(name).lower()
    dot = lowered.rfind(".")
    if dot < 0:
        return None
    return _EXTENSION_MAP.get(lowered[dot:])


def detect(head: bytes) -> Detection:
    if not head:
        return Detection(None, UNKNOWN, "empty")
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return Detection("image/webp", IMAGE, "magic")
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return Detection(mime, family_of(mime), "magic")
    if (
        head[0:2] == b"MZ"
        and len(head) >= 64
        and 0 < int.from_bytes(head[0x3C:0x40], "little") < 0x1000
    ):
        # A two-byte "MZ" prefix also appears in ordinary text; require the
        # DOS header's e_lfanew field to point at a plausible PE header.
        return Detection("application/x-msdownload", EXECUTABLE, "magic")
    if head[0:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"):
        return Detection("application/x-mach-binary", EXECUTABLE, "magic")
    if b"\x00" in head:
        return Detection(None, UNKNOWN, "binary")

    text = head.decode("utf-8", "ignore").lstrip("\ufeff \t\r\n")
    if not text or not _PRINTABLE.match(text):
        return Detection(None, UNKNOWN, "binary")
    match = _MARKUP.search(text)
    if match:
        tag = match.group(0).lower()
        if "svg" in tag:
            return Detection("image/svg+xml", MARKUP, "text")
        if "script" in tag:
            return Detection("text/javascript", MARKUP, "text")
        return Detection("text/html", MARKUP, "text")
    if text.startswith("<?xml"):
        return Detection("application/xml", MARKUP, "text")
    if text[0] in "{[":
        try:
            json.loads(text)
        except ValueError:
            pass
        else:
            return Detection("application/json", TEXT, "text")
        if len(text) > 4096:
            return Detection("application/json", TEXT, "text")
    return Detection("text/plain", TEXT, "text")


def _same_container(detected: str, expected: str) -> bool:
    if detected == "application/zip" and expected in OOXML_TYPES:
        return True
    if detected == "application/zip" and expected == "application/zip":
        return True
    return detected.startswith("text/") and expected.startswith("text/")


def verify(
    *,
    name: str,
    head: bytes,
    declared_mime: str | None,
    kind: str,
    limits: MediaLimits,
) -> str:
    """Return the effective MIME type or raise a stable media error.

    ``kind`` is ``IMAGE`` or ``ATTACHMENT``. The returned value is what the
    reference records; it always comes from the detected content, never from a
    caller-provided string.
    """
    detection = detect(head)
    if detection.family in (EXECUTABLE, MARKUP):
        raise MediaContentForbidden(f"content={mime_token(detection.mime)}")
    if not detection.known:
        raise MediaTypeUnsupported(f"detected={detection.evidence}")

    effective = detection.mime
    expected = expected_from_name(name)
    if expected is not None and expected != effective:
        if _same_container(effective, expected):
            effective = expected
        else:
            raise MediaContentMismatch(
                f"ext={mime_token(expected)},detected={mime_token(effective)}"
            )

    declared = normalize_mime(declared_mime)
    if declared is not None and declared not in _GENERIC_MIME and declared != effective:
        if _same_container(effective, declared):
            pass
        else:
            raise MediaContentMismatch(
                f"declared={mime_token(declared)},detected={mime_token(effective)}"
            )

    allowed = limits.image_mime_types if kind == "IMAGE" else limits.attachment_mime_types
    if effective not in allowed:
        raise MediaTypeUnsupported(f"detected={mime_token(effective)}")
    return effective


__all__ = [
    "ARCHIVE",
    "DOCUMENT",
    "Detection",
    "EXECUTABLE",
    "IMAGE",
    "MARKUP",
    "OOXML_SHEET",
    "OOXML_SLIDES",
    "OOXML_TYPES",
    "OOXML_WORD",
    "TEXT",
    "UNKNOWN",
    "detect",
    "expected_from_name",
    "family_of",
    "mime_token",
    "normalize_mime",
    "verify",
]
