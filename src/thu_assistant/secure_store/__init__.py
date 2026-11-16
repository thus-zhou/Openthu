"""Cross-platform protected storage for Openthu v0.2.

Public entry points live here; platform key loading is deliberately explicit.
A production caller must configure a key path and must not fall back to a test
codec or plaintext when it is missing.
"""
from __future__ import annotations

from .envelope import (
    ACCOUNT_DOMAINS,
    BACKENDS,
    CIPHER_SUITE,
    FORMAT_VERSION,
    PURPOSES,
    SERVICE_DOMAINS,
    StorageContext,
    inspect_envelope,
    pack_envelope,
    parse_envelope,
)
from .errors import (
    SecureAccountMismatch,
    SecureBackendUnsupported,
    SecureCryptoUnavailable,
    SecureDataDirError,
    SecureDecryptError,
    SecureDirPermissionError,
    SecureEnvelopeCorrupt,
    SecureInputError,
    SecureInvalidRecordId,
    SecureKeyExists,
    SecureKeyFormatError,
    SecureKeyLocationError,
    SecureKeyMismatch,
    SecureKeyNotConfigured,
    SecureKeyPermissionError,
    SecureKeyUnavailable,
    SecureKeyUnreadable,
    SecureLegacyFormat,
    SecureLegacyUnavailable,
    SecurePayloadInvalid,
    SecurePurposeMismatch,
    SecureRecordNotFound,
    SecureRecordMismatch,
    SecureRevisionConflict,
    SecureRollbackUnavailable,
    SecureRotationIncomplete,
    SecureRotationNoop,
    SecureServiceMismatch,
    SecureStoreBusy,
    SecureStoreCorrupt,
    SecureStoreError,
    SecureWriteError,
)
from .keys import (
    DPAPI_KEY_MAGIC,
    FILE_KEY_MAGIC,
    KeyMaterial,
    create_key_file,
    default_backend,
    load_key_file,
)
from .providers import (
    CRYPTO_AVAILABLE,
    AeadProvider,
    FileKeyProvider,
    WindowsDpapiKeyProvider,
    create_provider,
    load_provider,
    provider_from_environment,
)
from .store import (
    LegacyDpapiReader,
    LegacyRecord,
    SecureStore,
    open_store_from_environment,
    validate_key_separation,
)

__all__ = [
    "ACCOUNT_DOMAINS",
    "BACKENDS",
    "CIPHER_SUITE",
    "CRYPTO_AVAILABLE",
    "DPAPI_KEY_MAGIC",
    "FILE_KEY_MAGIC",
    "FORMAT_VERSION",
    "PURPOSES",
    "SERVICE_DOMAINS",
    "AeadProvider",
    "FileKeyProvider",
    "KeyMaterial",
    "LegacyDpapiReader",
    "LegacyRecord",
    "SecureAccountMismatch",
    "SecureBackendUnsupported",
    "SecureCryptoUnavailable",
    "SecureDataDirError",
    "SecureDecryptError",
    "SecureDirPermissionError",
    "SecureEnvelopeCorrupt",
    "SecureInputError",
    "SecureInvalidRecordId",
    "SecureKeyExists",
    "SecureKeyFormatError",
    "SecureKeyLocationError",
    "SecureKeyMismatch",
    "SecureKeyNotConfigured",
    "SecureKeyPermissionError",
    "SecureKeyUnavailable",
    "SecureKeyUnreadable",
    "SecureLegacyFormat",
    "SecureLegacyUnavailable",
    "SecurePayloadInvalid",
    "SecurePurposeMismatch",
    "SecureRecordMismatch",
    "SecureRecordNotFound",
    "SecureRevisionConflict",
    "SecureRollbackUnavailable",
    "SecureRotationIncomplete",
    "SecureRotationNoop",
    "SecureServiceMismatch",
    "SecureStore",
    "SecureStoreBusy",
    "SecureStoreCorrupt",
    "SecureStoreError",
    "SecureWriteError",
    "StorageContext",
    "WindowsDpapiKeyProvider",
    "create_key_file",
    "create_provider",
    "default_backend",
    "handle_request",
    "inspect_envelope",
    "load_key_file",
    "load_provider",
    "open_store_from_environment",
    "pack_envelope",
    "parse_envelope",
    "provider_from_environment",
    "subprocess_environment",
    "validate_key_separation",
]


def __getattr__(name: str):
    """Load the private subprocess bridge only when a caller asks for it.

    Eagerly importing ``bridge`` here makes ``python -m
    thu_assistant.secure_store.bridge`` import the module before runpy executes
    it, which produces a warning and can run the entry point twice on Windows.
    """
    if name in {"handle_request", "subprocess_environment"}:
        from . import bridge

        return getattr(bridge, name)
    raise AttributeError(name)
