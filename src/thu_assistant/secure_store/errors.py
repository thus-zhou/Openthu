"""Stable, public-safe errors for protected local storage.

The exception string is deliberately just the code.  A caller may attach a
short, enumerated reason in ``detail``, but file paths, account names, ciphertext
and key material must never be interpolated into an error.
"""
from __future__ import annotations


class SecureStoreError(Exception):
    """Base class for storage failures that can cross a process boundary."""

    code = "SECURE_STORE_ERROR"
    category = "INTERNAL"
    retryable = False
    user_action = "CONTACT_SUPPORT"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(self.code)

    def as_error(self) -> dict:
        """Return the frozen error shape without adding local context."""
        return {
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "automatic_replay_allowed": False,
            "user_action": self.user_action,
        }


class SecureInputError(SecureStoreError):
    code = "SECURE_INVALID_INPUT"
    category = "INPUT"
    user_action = "FIX_INPUT"


class SecureInvalidRecordId(SecureInputError):
    code = "SECURE_INVALID_RECORD_ID"


class SecureBackendUnsupported(SecureStoreError):
    code = "SECURE_BACKEND_UNSUPPORTED"
    category = "NOT_IMPLEMENTED"


class SecureCryptoUnavailable(SecureBackendUnsupported):
    code = "SECURE_CRYPTO_UNAVAILABLE"
    detail_default = "The cryptography package is required for authenticated storage."

    def __init__(self, detail: str = detail_default) -> None:
        super().__init__(detail)


class SecureKeyNotConfigured(SecureStoreError):
    code = "SECURE_KEY_NOT_CONFIGURED"
    category = "NOT_IMPLEMENTED"


class SecureKeyExists(SecureStoreError):
    code = "SECURE_KEY_EXISTS"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureKeyPermissionError(SecureStoreError):
    code = "SECURE_KEY_PERMISSION"
    category = "PERMISSION"


class SecureKeyLocationError(SecureStoreError):
    code = "SECURE_KEY_LOCATION"
    category = "PERMISSION"


class SecureKeyFormatError(SecureStoreError):
    code = "SECURE_KEY_FORMAT"


class SecureKeyUnreadable(SecureStoreError):
    code = "SECURE_KEY_UNREADABLE"


class SecureKeyMismatch(SecureStoreError):
    code = "SECURE_KEY_MISMATCH"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureKeyUnavailable(SecureStoreError):
    code = "SECURE_KEY_UNAVAILABLE"
    category = "CONFLICT"


class SecureEnvelopeCorrupt(SecureStoreError):
    code = "SECURE_ENVELOPE_CORRUPT"


class SecureDecryptError(SecureStoreError):
    code = "SECURE_DECRYPT_FAILED"


class SecurePayloadInvalid(SecureStoreError):
    code = "SECURE_PAYLOAD_INVALID"


class SecureAccountMismatch(SecureStoreError):
    code = "SECURE_ACCOUNT_MISMATCH"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecurePurposeMismatch(SecureStoreError):
    code = "SECURE_PURPOSE_MISMATCH"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureServiceMismatch(SecureStoreError):
    code = "SECURE_SERVICE_MISMATCH"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureRecordMismatch(SecureStoreError):
    code = "SECURE_RECORD_MISMATCH"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureRevisionConflict(SecureStoreError):
    code = "SECURE_REVISION_CONFLICT"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureRecordNotFound(SecureStoreError):
    code = "SECURE_RECORD_NOT_FOUND"


class SecureStoreCorrupt(SecureStoreError):
    code = "SECURE_STORE_CORRUPT"


class SecureStoreBusy(SecureStoreError):
    code = "SECURE_STORE_BUSY"
    category = "CONFLICT"
    retryable = True
    user_action = "WAIT"


class SecureWriteError(SecureStoreError):
    code = "SECURE_WRITE_FAILED"


class SecureRotationIncomplete(SecureStoreError):
    code = "SECURE_ROTATION_INCOMPLETE"

    def __init__(self, migrated: int = 0, total: int = 0, *, cause_code: str = "") -> None:
        self.migrated = migrated
        self.total = total
        self.cause_code = cause_code
        super().__init__("rotation did not reach the pointer switch")


class SecureRotationNoop(SecureStoreError):
    code = "SECURE_ROTATION_NOOP"
    category = "CONFLICT"
    user_action = "FIX_INPUT"


class SecureRollbackUnavailable(SecureStoreError):
    code = "SECURE_ROLLBACK_UNAVAILABLE"
    category = "CONFLICT"


class SecureLegacyUnavailable(SecureStoreError):
    code = "SECURE_LEGACY_UNAVAILABLE"
    category = "AUTH"
    user_action = "REAUTHENTICATE"


class SecureLegacyFormat(SecureStoreError):
    code = "SECURE_LEGACY_FORMAT"
    category = "INTERNAL"


class SecureDataDirError(SecureStoreError):
    code = "SECURE_DATA_DIR_INVALID"
    category = "PERMISSION"


class SecureDirPermissionError(SecureStoreError):
    code = "SECURE_DATA_DIR_PERMISSION"
    category = "PERMISSION"
