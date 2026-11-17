"""Stable, leak-free error taxonomy for the course resource centre.

Every error carries the ``error-classification-v1`` shape so a caller can
classify it without parsing prose.  The free-form detail is restricted to short
enum-like tokens and the final message is capped at 200 characters: a local
path, a download directory, an account name or a course body must never appear
in an exception message, a log line or an export report, because those strings
are copied into terminal output and issue reports.

Codes stay module-local until the business facade (6.1) registers them in
``contracts/v0.2/error-classification.json``.  Authentication and permission
codes reuse the frozen registry values so a caller can act on them immediately.
"""
from __future__ import annotations

import re

_DETAIL = re.compile(r"^[A-Za-z0-9_.:=,+-]{0,120}$")
_MAX_MESSAGE = 200


class ResourceError(Exception):
    """Base class; callers catch this and read ``code``/``category``."""

    code = "RESOURCE_ERROR"
    category = "INTERNAL"
    retryable = False
    automatic_replay_allowed = False
    user_action = "CONTACT_SUPPORT"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail if _DETAIL.fullmatch(detail or "") else "DETAIL_REDACTED"
        message = f"{self.code}:{self.detail}" if self.detail else self.code
        super().__init__(message[:_MAX_MESSAGE])

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
) -> type[ResourceError]:
    return type(
        name,
        (ResourceError,),
        {
            "code": code,
            "category": category,
            "retryable": retryable,
            "user_action": user_action,
            "__doc__": doc,
        },
    )


def require_bool(value: object, name: str) -> bool:
    """Only a real bool crosses a safety gate; ``"false"`` is not ``False``."""
    if not isinstance(value, bool):
        raise ResourceInvalidArgument(f"{name}_type")
    return value


def require_text(
    value: object, name: str, *, maximum: int = 200, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ResourceInvalidArgument(f"{name}_type")
    return value


# INPUT: malformed or unsupported caller input.
ResourceInvalidArgument = _define(
    "ResourceInvalidArgument", "RESOURCE_INVALID_ARGUMENT", "INPUT", False, "FIX_INPUT"
)
ResourceSourceUnsupported = _define(
    "ResourceSourceUnsupported", "RESOURCE_SOURCE_UNSUPPORTED", "INPUT", False, "FIX_INPUT",
    "The observation names a source kind this module does not index.",
)
ResourceTitleUnsafe = _define(
    "ResourceTitleUnsafe", "RESOURCE_TITLE_UNSAFE", "INPUT", False, "FIX_INPUT",
    "A title cannot be turned into a safe archive component.",
)
ResourceLimitExceeded = _define(
    "ResourceLimitExceeded", "RESOURCE_LIMIT_EXCEEDED", "INPUT", False, "FIX_INPUT",
    "A bounded engineering limit (entries, plan items, export rows) was exceeded.",
)
ResourceSelectionInvalid = _define(
    "ResourceSelectionInvalid", "RESOURCE_SELECTION_INVALID", "INPUT", False, "FIX_INPUT"
)
ResourceNotFound = _define(
    "ResourceNotFound", "RESOURCE_NOT_FOUND", "INPUT", False, "FIX_INPUT"
)
ResourceConfirmationRequired = _define(
    "ResourceConfirmationRequired", "RESOURCE_CONFIRMATION_REQUIRED", "INPUT", False, "FIX_INPUT",
    "A batch download or a personal-content export requires explicit confirmation.",
)
ResourceExportEmpty = _define(
    "ResourceExportEmpty", "RESOURCE_EXPORT_EMPTY", "INPUT", False, "FIX_INPUT"
)

# AUTH / PERMISSION: reuse frozen registry codes and categories.
ResourceAuthRequired = _define(
    "ResourceAuthRequired", "AUTH_REQUIRED", "AUTH", False, "REAUTHENTICATE",
    "The existing campus session expired; complete the official login again.",
)
ResourceForbidden = _define(
    "ResourceForbidden", "FORBIDDEN", "PERMISSION", False, "CONTACT_SUPPORT",
    "The account is not allowed to read this source.",
)

# PERMISSION: refused by policy.
ResourceExportInRepository = _define(
    "ResourceExportInRepository", "RESOURCE_EXPORT_IN_REPOSITORY", "PERMISSION", False,
    "CONTACT_SUPPORT",
    "An export must not be written inside a Git working tree.",
)

# COMPLETENESS: the plan or index is stale.
ResourcePlanExpired = _define(
    "ResourcePlanExpired", "RESOURCE_PLAN_EXPIRED", "COMPLETENESS", True, "RETRY"
)
ResourceIndexIncomplete = _define(
    "ResourceIndexIncomplete", "RESOURCE_INDEX_INCOMPLETE", "COMPLETENESS", True, "RETRY",
    "The index was built from an incomplete source read.",
)
ResourceSourceIncomplete = _define(
    "ResourceSourceIncomplete", "RESOURCE_SOURCE_INCOMPLETE", "COMPLETENESS", True, "RETRY",
    "A requested source range (detail page, attachment list or course) was not read.",
)

# CONFLICT: a local or account binding changed.
ResourceAccountMismatch = _define(
    "ResourceAccountMismatch", "RESOURCE_ACCOUNT_MISMATCH", "CONFLICT", False, "FIX_INPUT"
)
ResourceVersionConflict = _define(
    "ResourceVersionConflict", "RESOURCE_VERSION_CONFLICT", "CONFLICT", False, "FIX_INPUT",
    "A source or content version cannot be selected without breaking monotonicity.",
)
ResourcePlanChanged = _define(
    "ResourcePlanChanged", "RESOURCE_PLAN_CHANGED", "CONFLICT", False, "FIX_INPUT",
    "The index or target changed after the plan was previewed.",
)
ResourceHashConflict = _define(
    "ResourceHashConflict", "RESOURCE_HASH_CONFLICT", "CONFLICT", False, "FIX_INPUT",
    "The target file exists with different content; it is never overwritten.",
)
ResourceExportExists = _define(
    "ResourceExportExists", "RESOURCE_EXPORT_EXISTS", "CONFLICT", False, "FIX_INPUT",
    "The export target exists; an explicit overwrite flag is required.",
)

# UPSTREAM_CONTRACT: the source read or transfer failed for one item.
ResourceDownloadFailed = _define(
    "ResourceDownloadFailed", "RESOURCE_DOWNLOAD_FAILED", "UPSTREAM_CONTRACT", True, "RETRY"
)
ResourceContentUnavailable = _define(
    "ResourceContentUnavailable", "RESOURCE_CONTENT_UNAVAILABLE", "NOT_IMPLEMENTED", False,
    "CONTACT_SUPPORT",
    "No text extraction exists for this document type in this version.",
)

# CANCELLED: the caller stopped the batch; nothing further was written.
ResourceCancelled = _define(
    "ResourceCancelled", "RESOURCE_CANCELLED", "CANCELLED", False, "NONE"
)

# INTERNAL: local state or storage failure.
ResourceStoreUnavailable = _define(
    "ResourceStoreUnavailable", "RESOURCE_STORE_UNAVAILABLE", "INTERNAL", False,
    "CONTACT_SUPPORT",
    "The protected index store is absent, locked or failed to decrypt.",
)
ResourceIndexCorrupt = _define(
    "ResourceIndexCorrupt", "RESOURCE_INDEX_CORRUPT", "INTERNAL", False, "CONTACT_SUPPORT"
)
ResourceWriteFailed = _define(
    "ResourceWriteFailed", "RESOURCE_WRITE_FAILED", "INTERNAL", False, "CONTACT_SUPPORT"
)
ResourceDiskFull = _define(
    "ResourceDiskFull", "RESOURCE_DISK_FULL", "INTERNAL", False, "CONTACT_SUPPORT"
)
ResourceFileLocked = _define(
    "ResourceFileLocked", "RESOURCE_FILE_LOCKED", "INTERNAL", False, "CONTACT_SUPPORT"
)

ERROR_CLASSES = tuple(
    value
    for value in dict(globals()).values()
    if isinstance(value, type)
    and issubclass(value, ResourceError)
    and value is not ResourceError
)

__all__ = [
    "ERROR_CLASSES",
    "require_bool",
    "require_text",
    "ResourceAccountMismatch",
    "ResourceAuthRequired",
    "ResourceCancelled",
    "ResourceConfirmationRequired",
    "ResourceContentUnavailable",
    "ResourceDiskFull",
    "ResourceDownloadFailed",
    "ResourceError",
    "ResourceExportEmpty",
    "ResourceExportExists",
    "ResourceExportInRepository",
    "ResourceFileLocked",
    "ResourceForbidden",
    "ResourceHashConflict",
    "ResourceIndexCorrupt",
    "ResourceIndexIncomplete",
    "ResourceInvalidArgument",
    "ResourceLimitExceeded",
    "ResourceNotFound",
    "ResourcePlanChanged",
    "ResourcePlanExpired",
    "ResourceSelectionInvalid",
    "ResourceSourceIncomplete",
    "ResourceSourceUnsupported",
    "ResourceStoreUnavailable",
    "ResourceTitleUnsafe",
    "ResourceVersionConflict",
    "ResourceWriteFailed",
]
