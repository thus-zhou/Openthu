"""Authenticated-encryption providers with explicit key ownership."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Any

from .envelope import (
    CIPHER_SUITE,
    FORMAT_VERSION,
    MAX_NONCE_BYTES,
    MAX_PLAINTEXT_BYTES,
    StorageContext,
    Unsealed,
    b64url,
    b64url_decode,
    canonical_aad,
    check_expected,
    pack_envelope,
    parse_envelope,
)
from .errors import (
    SecureAccountMismatch,
    SecureBackendUnsupported,
    SecureCryptoUnavailable,
    SecureDecryptError,
    SecureEnvelopeCorrupt,
    SecureInputError,
    SecureKeyMismatch,
    SecureKeyNotConfigured,
)
from .keys import KeyMaterial, create_key_file, default_backend, load_key_file

try:  # cryptography is a proposed direct dependency; keep import failure explicit.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in a broken installation
    InvalidTag = Exception  # type: ignore[assignment,misc]
    hashes = None  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]
    HKDF = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

_HKDF_SALT = b"openthu-secure-store-v1"


def _account_bytes(account: str | None) -> bytes:
    if account is None:
        return b""
    if not isinstance(account, str):
        raise SecureInputError("account must be a string")
    if len(account) > 512:
        raise SecureInputError("account is too long")
    return account.encode("utf-8")


class AeadProvider:
    """AES-256-GCM provider over a random 32-byte master key."""

    format_version = FORMAT_VERSION
    cipher_suite = CIPHER_SUITE

    def __init__(self, material: KeyMaterial) -> None:
        if not CRYPTO_AVAILABLE:
            raise SecureCryptoUnavailable()
        if not isinstance(material.secret, bytes) or len(material.secret) != 32:
            raise SecureBackendUnsupported("invalid key material")
        self.backend = material.backend
        self.key_id = material.key_id
        self.key_created_at = material.created_at
        self._secret = material.secret
        self._subkeys: dict[bytes, bytes] = {}

    def _subkey(self, info: bytes) -> bytes:
        if info not in self._subkeys:
            self._subkeys[info] = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=_HKDF_SALT,
                info=info,
            ).derive(self._secret)
        return self._subkeys[info]

    def account_reference(
        self,
        account: str | None,
        *,
        purpose: str,
        account_domain: str,
        service_domain: str,
        namespace_key: bytes | None = None,
    ) -> str:
        """Derive a purpose-scoped pseudonym; the raw account is never stored.

        ``namespace_key`` is the store's protected account namespace.  It stays
        stable across data-key rotation; without it the provider key is used.
        """
        if purpose not in {"CREDENTIAL", "SESSION", "ACTION", "INDEX", "MEDIA", "EVENT_CURSOR"}:
            raise SecureInputError("unknown storage purpose")
        if account_domain not in {"CAMPUS", "MODEL", "LOCAL", "SYSTEM", "UNKNOWN"}:
            raise SecureInputError("unknown account domain")
        if service_domain not in {"INFO", "LEARN", "LIBRARY", "SPORTS", "MADMODEL", "LOCAL", "UNKNOWN"}:
            raise SecureInputError("unknown service domain")
        raw = _account_bytes(account)
        if not raw:
            return "none"
        if namespace_key is not None:
            if not isinstance(namespace_key, bytes) or len(namespace_key) != 32:
                raise SecureInputError("account namespace key is invalid")
            namespace = namespace_key
        else:
            namespace = self._secret
        reference_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=b"account-reference",
        ).derive(namespace)
        scope = b"\0".join(
            (
                purpose.encode("ascii"),
                account_domain.encode("ascii"),
                service_domain.encode("ascii"),
                raw,
            )
        )
        digest = hmac.new(reference_key, scope, hashlib.sha256).digest()
        return "acct_" + b64url(digest[:24])

    def seal(self, plaintext: bytes, context: StorageContext) -> bytes:
        if not isinstance(plaintext, bytes):
            raise SecureInputError("plaintext must be bytes")
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise SecureInputError("plaintext is too large")
        aad = canonical_aad(context)
        nonce = os.urandom(MAX_NONCE_BYTES)
        ciphertext = AESGCM(self._subkey(b"aead:" + context.purpose.encode("ascii"))).encrypt(
            nonce, plaintext, aad
        )
        header: dict[str, Any] = {
            "magic": b"THU-SECURE-1\0".decode("ascii"),
            "format_version": FORMAT_VERSION,
            "backend": self.backend,
            "purpose": context.purpose,
            "account_domain": context.account_domain,
            "service_domain": context.service_domain,
            "key_id": self.key_id,
            "nonce": b64url(nonce),
            "aad": b64url(aad),
            "cipher_suite": CIPHER_SUITE,
            "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "plaintext_length": len(plaintext),
            "created_at": _now(),
        }
        if self.key_created_at:
            header["key_created_at"] = self.key_created_at
        return pack_envelope(header, ciphertext)

    def unseal(
        self,
        data: bytes,
        expected: StorageContext | None = None,
        *,
        expected_revision: int | None = None,
    ) -> Unsealed:
        envelope = parse_envelope(data)
        if envelope.header.get("key_id") != self.key_id:
            raise SecureKeyMismatch("envelope was sealed by another key")
        check_expected(envelope.context, expected, expected_revision)
        try:
            nonce = b64url_decode(envelope.header["nonce"], maximum=MAX_NONCE_BYTES)
            plaintext = AESGCM(
                self._subkey(b"aead:" + envelope.context.purpose.encode("ascii"))
            ).decrypt(nonce, envelope.ciphertext, envelope.aad)
        except InvalidTag:
            raise SecureDecryptError("authentication failed") from None
        except SecureDecryptError:
            raise
        except Exception:
            raise SecureDecryptError("authentication failed") from None
        if len(plaintext) != envelope.header["plaintext_length"]:
            raise SecureEnvelopeCorrupt("plaintext length mismatch")
        return Unsealed(
            plaintext=plaintext, header=envelope.header, context=envelope.context
        )

    def health(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "format_version": self.format_version,
            "cipher_suite": self.cipher_suite,
            "key_id": self.key_id,
            "key_present": True,
            "key_created_at": self.key_created_at or None,
        }


class FileKeyProvider(AeadProvider):
    """Linux/desktop backend using an owner-only random key file."""

    def __init__(self, key_path: str | os.PathLike[str]) -> None:
        material = load_key_file(key_path, "file-key")
        self.key_path = Path(key_path).expanduser()
        super().__init__(material)


class WindowsDpapiKeyProvider(AeadProvider):
    """Windows backend using current-user DPAPI to wrap the random key."""

    def __init__(self, key_path: str | os.PathLike[str]) -> None:
        material = load_key_file(key_path, "windows-dpapi")
        self.key_path = Path(key_path).expanduser()
        super().__init__(material)


def load_provider(
    key_path: str | os.PathLike[str], backend: str | None = None
) -> AeadProvider:
    material = load_key_file(key_path, backend)
    if material.backend == "windows-dpapi":
        return WindowsDpapiKeyProvider(key_path)
    return FileKeyProvider(key_path)


def create_provider(
    key_path: str | os.PathLike[str], backend: str | None = None
) -> AeadProvider:
    create_key_file(key_path, backend or default_backend())
    return load_provider(key_path, backend)


def provider_from_environment(env: dict[str, str] | None = None) -> AeadProvider:
    source = os.environ if env is None else env
    key_path = source.get("THU_SECURE_KEY_FILE")
    if not key_path:
        raise SecureKeyNotConfigured("THU_SECURE_KEY_FILE is not set")
    backend = source.get("THU_SECURE_BACKEND") or None
    return load_provider(key_path, backend)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "AeadProvider",
    "CRYPTO_AVAILABLE",
    "FileKeyProvider",
    "WindowsDpapiKeyProvider",
    "create_provider",
    "load_provider",
    "provider_from_environment",
]
