"""Stable, leak-free error taxonomy for the local media pipeline.

Every error carries the ``error-classification-v1`` shape so a caller can
classify it without parsing prose. The free-form detail is restricted to short
enum-like tokens: a local path, image bytes, a hash of a private file or any
authentication material must never appear in an exception message or a log
line, because those strings are copied into terminal output, issue reports and
handover notes.

Codes stay module-local until the business facade (6.1) registers them in
``contracts/v0.2/error-classification.json``. Each class already uses the
category policy values frozen there, so registration is mechanical.
"""
from __future__ import annotations

import re

_DETAIL = re.compile(r"^[A-Za-z0-9_.:=,+-]{0,200}$")


class MediaError(Exception):
    """Base class; callers catch this and read ``code``/``category``."""

    code = "MEDIA_ERROR"
    category = "INTERNAL"
    retryable = False
    automatic_replay_allowed = False
    user_action = "CONTACT_SUPPORT"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail if _DETAIL.fullmatch(detail or "") else "DETAIL_REDACTED"
        super().__init__(f"{self.code}:{self.detail}" if self.detail else self.code)

    def as_error(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "message": str(self),
            "retryable": self.retryable,
            "automatic_replay_allowed": False,
            "user_action": self.user_action,
        }


def _define(
    name: str,
    code: str,
    category: str,
    retryable: bool,
    user_action: str,
    doc: str = "",
) -> type[MediaError]:
    return type(
        name,
        (MediaError,),
        {
            "code": code,
            "category": category,
            "retryable": retryable,
            "user_action": user_action,
            "__doc__": doc,
        },
    )


# INPUT: malformed, unsupported or oversized caller input.
MediaInvalidArgument = _define(
    "MediaInvalidArgument", "MEDIA_INVALID_ARGUMENT", "INPUT", False, "FIX_INPUT"
)
MediaSourceKindInvalid = _define(
    "MediaSourceKindInvalid", "MEDIA_SOURCE_KIND_INVALID", "INPUT", False, "FIX_INPUT",
    "A path field carried a URL or an unsupported source kind.",
)
MediaFileNotFound = _define(
    "MediaFileNotFound", "MEDIA_FILE_NOT_FOUND", "INPUT", False, "FIX_INPUT"
)
MediaNotRegularFile = _define(
    "MediaNotRegularFile", "MEDIA_NOT_REGULAR_FILE", "INPUT", False, "FIX_INPUT"
)
MediaPathInvalid = _define(
    "MediaPathInvalid", "MEDIA_PATH_INVALID", "INPUT", False, "FIX_INPUT",
    "Symlink, changed identity or otherwise unusable path.",
)
MediaEmptyFile = _define(
    "MediaEmptyFile", "MEDIA_EMPTY_FILE", "INPUT", False, "FIX_INPUT"
)
MediaSizeLimit = _define(
    "MediaSizeLimit", "MEDIA_SIZE_LIMIT", "INPUT", False, "FIX_INPUT"
)
MediaTypeUnsupported = _define(
    "MediaTypeUnsupported", "MEDIA_TYPE_UNSUPPORTED", "INPUT", False, "FIX_INPUT"
)
MediaContentMismatch = _define(
    "MediaContentMismatch", "MEDIA_CONTENT_MISMATCH", "INPUT", False, "FIX_INPUT",
    "Declared or extension MIME disagrees with the detected content signature.",
)
MediaDecodeFailed = _define(
    "MediaDecodeFailed", "MEDIA_DECODE_FAILED", "INPUT", False, "FIX_INPUT"
)
MediaResourceLimit = _define(
    "MediaResourceLimit", "MEDIA_RESOURCE_LIMIT", "INPUT", False, "FIX_INPUT",
    "Pixel count, side length or decode allocation exceeds the engineering limit.",
)
MediaProcessingTimeout = _define(
    "MediaProcessingTimeout", "MEDIA_PROCESSING_TIMEOUT", "INPUT", False, "FIX_INPUT",
    "The caller's processing deadline elapsed before a sendable result existed.",
)
MediaBundleLimit = _define(
    "MediaBundleLimit", "MEDIA_BUNDLE_LIMIT", "INPUT", False, "FIX_INPUT",
    "More images or more bytes than one request may carry.",
)

# PERMISSION: refused by policy, not by accident.
MediaContentForbidden = _define(
    "MediaContentForbidden", "MEDIA_CONTENT_FORBIDDEN", "PERMISSION", False, "CONTACT_SUPPORT",
    "Executable, markup or script content disguised as a document.",
)
MediaFileUnreadable = _define(
    "MediaFileUnreadable", "MEDIA_FILE_UNREADABLE", "PERMISSION", False, "CONTACT_SUPPORT"
)
MediaTempInsecure = _define(
    "MediaTempInsecure", "MEDIA_TEMP_INSECURE", "PERMISSION", False, "CONTACT_SUPPORT"
)
MediaTransferNotAuthorized = _define(
    "MediaTransferNotAuthorized", "MEDIA_TRANSFER_NOT_AUTHORIZED", "PERMISSION", False,
    "CONTACT_SUPPORT",
)

# COMPLETENESS: the reference or grant is stale and must be re-issued.
MediaReferenceExpired = _define(
    "MediaReferenceExpired", "MEDIA_REFERENCE_EXPIRED", "COMPLETENESS", True, "RETRY"
)
MediaBundleExpired = _define(
    "MediaBundleExpired", "MEDIA_BUNDLE_EXPIRED", "COMPLETENESS", True, "RETRY"
)

# CONFLICT: a local binding changed.
MediaReferenceAccountMismatch = _define(
    "MediaReferenceAccountMismatch", "MEDIA_REFERENCE_ACCOUNT_MISMATCH", "CONFLICT", False,
    "FIX_INPUT",
)
MediaReferenceTaskMismatch = _define(
    "MediaReferenceTaskMismatch", "MEDIA_REFERENCE_TASK_MISMATCH", "CONFLICT", False,
    "FIX_INPUT",
)
MediaReferencePurposeMismatch = _define(
    "MediaReferencePurposeMismatch", "MEDIA_REFERENCE_PURPOSE_MISMATCH", "CONFLICT", False,
    "FIX_INPUT",
)
MediaReferenceConsumed = _define(
    "MediaReferenceConsumed", "MEDIA_REFERENCE_CONSUMED", "CONFLICT", False, "FIX_INPUT"
)
MediaReferenceRevoked = _define(
    "MediaReferenceRevoked", "MEDIA_REFERENCE_REVOKED", "CONFLICT", False, "FIX_INPUT"
)
MediaTempInUse = _define(
    "MediaTempInUse", "MEDIA_TEMP_IN_USE", "CONFLICT", False, "FIX_INPUT",
    "Another live instance owns this controlled temporary root.",
)

# NOT_IMPLEMENTED: the decoder or transport is absent from this build.
MediaDecoderUnavailable = _define(
    "MediaDecoderUnavailable", "MEDIA_DECODER_UNAVAILABLE", "NOT_IMPLEMENTED", False,
    "CONTACT_SUPPORT",
)

# CANCELLED: the caller cancelled; nothing was produced.
MediaCancelled = _define(
    "MediaCancelled", "MEDIA_CANCELLED", "CANCELLED", False, "NONE"
)

# INTERNAL: local state or storage failure.
MediaTempUnavailable = _define(
    "MediaTempUnavailable", "MEDIA_TEMP_UNAVAILABLE", "INTERNAL", False, "CONTACT_SUPPORT"
)
MediaWriteFailed = _define(
    "MediaWriteFailed", "MEDIA_WRITE_FAILED", "INTERNAL", False, "CONTACT_SUPPORT"
)
MediaReadFailed = _define(
    "MediaReadFailed", "MEDIA_READ_FAILED", "INTERNAL", False, "CONTACT_SUPPORT",
    "A local read failed after the file was opened and validated.",
)
MediaReferenceNotFound = _define(
    "MediaReferenceNotFound", "MEDIA_REFERENCE_NOT_FOUND", "INTERNAL", False, "CONTACT_SUPPORT"
)
MediaReferenceCorrupt = _define(
    "MediaReferenceCorrupt", "MEDIA_REFERENCE_CORRUPT", "INTERNAL", False, "CONTACT_SUPPORT"
)
MediaSanitizeIncomplete = _define(
    "MediaSanitizeIncomplete", "MEDIA_SANITIZE_INCOMPLETE", "INTERNAL", False, "CONTACT_SUPPORT"
)
MediaStateUnavailable = _define(
    "MediaStateUnavailable", "MEDIA_STATE_UNAVAILABLE", "INTERNAL", False, "CONTACT_SUPPORT"
)


ERROR_CLASSES = tuple(
    value
    for value in dict(globals()).values()
    if isinstance(value, type) and issubclass(value, MediaError) and value is not MediaError
)

__all__ = [
    "ERROR_CLASSES",
    "MediaError",
    "MediaBundleExpired",
    "MediaBundleLimit",
    "MediaCancelled",
    "MediaContentForbidden",
    "MediaContentMismatch",
    "MediaDecodeFailed",
    "MediaDecoderUnavailable",
    "MediaEmptyFile",
    "MediaFileNotFound",
    "MediaFileUnreadable",
    "MediaInvalidArgument",
    "MediaNotRegularFile",
    "MediaPathInvalid",
    "MediaProcessingTimeout",
    "MediaReadFailed",
    "MediaReferenceAccountMismatch",
    "MediaReferenceConsumed",
    "MediaReferenceCorrupt",
    "MediaReferenceExpired",
    "MediaReferenceNotFound",
    "MediaReferencePurposeMismatch",
    "MediaReferenceRevoked",
    "MediaReferenceTaskMismatch",
    "MediaResourceLimit",
    "MediaSanitizeIncomplete",
    "MediaSizeLimit",
    "MediaSourceKindInvalid",
    "MediaStateUnavailable",
    "MediaTempInUse",
    "MediaTempInsecure",
    "MediaTempUnavailable",
    "MediaTransferNotAuthorized",
    "MediaTypeUnsupported",
    "MediaWriteFailed",
]
