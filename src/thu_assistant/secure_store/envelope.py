"""Versioned authenticated-encryption envelope for Openthu local state.

The logical header follows ``contracts/v0.2/storage-header.schema.json``.  The
binary layout is::

    b"THU-SECURE-1\\0" | uint32 header length (big endian) | UTF-8 JSON | ciphertext

AES-256-GCM authenticates both ciphertext and the canonical AAD.  The AAD
contains the purpose, account/service domains, opaque account reference,
format version and revision.  A header alone is never treated as trusted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import (
    SecureAccountMismatch,
    SecureEnvelopeCorrupt,
    SecureInputError,
    SecurePurposeMismatch,
    SecureServiceMismatch,
)

MAGIC = b"THU-SECURE-1\0"
FORMAT_VERSION = 1
CIPHER_SUITE = "AES-256-GCM"
MAX_HEADER_BYTES = 4096
MAX_PLAINTEXT_BYTES = 128 * 1024 * 1024
MAX_NONCE_BYTES = 12
MIN_CIPHERTEXT_BYTES = 16  # GCM authentication tag

PURPOSES = frozenset(
    {"CREDENTIAL", "SESSION", "ACTION", "INDEX", "MEDIA", "EVENT_CURSOR"}
)
ACCOUNT_DOMAINS = frozenset({"CAMPUS", "MODEL", "LOCAL", "SYSTEM", "UNKNOWN"})
SERVICE_DOMAINS = frozenset(
    {"INFO", "LEARN", "LIBRARY", "SPORTS", "MADMODEL", "LOCAL", "UNKNOWN"}
)
BACKENDS = frozenset({"windows-dpapi", "file-key"})

_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ACCOUNT_REF = re.compile(r"^(?:none|acct_[A-Za-z0-9_-]{16,64})$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str, *, maximum: int = MAX_HEADER_BYTES) -> bytes:
    if not isinstance(value, str) or not _B64URL.fullmatch(value):
        raise SecureEnvelopeCorrupt("invalid base64url field")
    if len(value) > maximum * 2:
        raise SecureEnvelopeCorrupt("base64url field is too long")
    padding = "=" * (-len(value) % 4)
    try:
        data = base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError):
        raise SecureEnvelopeCorrupt("invalid base64url encoding") from None
    if len(data) > maximum:
        raise SecureEnvelopeCorrupt("decoded field is too long")
    return data


@dataclass(frozen=True)
class StorageContext:
    """Non-secret scope bound into the authenticated data."""

    purpose: str
    account_domain: str
    service_domain: str
    account_ref: str = "none"
    revision: int = 0

    def __post_init__(self) -> None:
        if self.purpose not in PURPOSES:
            raise SecureInputError("unknown storage purpose")
        if self.account_domain not in ACCOUNT_DOMAINS:
            raise SecureInputError("unknown account domain")
        if self.service_domain not in SERVICE_DOMAINS:
            raise SecureInputError("unknown service domain")
        if not isinstance(self.account_ref, str) or not _ACCOUNT_REF.fullmatch(
            self.account_ref
        ):
            raise SecureInputError("invalid account reference")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 0 <= self.revision <= 2**31 - 1
        ):
            raise SecureInputError("invalid revision")


@dataclass(frozen=True)
class Envelope:
    header: dict[str, Any]
    ciphertext: bytes
    context: StorageContext
    aad: bytes


@dataclass(frozen=True)
class Unsealed:
    plaintext: bytes
    header: dict[str, Any]
    context: StorageContext


def canonical_aad(context: StorageContext) -> bytes:
    """Canonical JSON used both as AES-GCM AAD and as the header ``aad`` value."""
    payload = {
        "account_domain": context.account_domain,
        "account_ref": context.account_ref,
        "format_version": FORMAT_VERSION,
        "purpose": context.purpose,
        "revision": context.revision,
        "service_domain": context.service_domain,
    }
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise SecureEnvelopeCorrupt(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SecureEnvelopeCorrupt(f"{field} is invalid") from None
    if parsed.tzinfo is None:
        raise SecureEnvelopeCorrupt(f"{field} must include a timezone")
    return parsed


def _validate_header(header: Any) -> tuple[StorageContext, bytes]:
    if not isinstance(header, dict):
        raise SecureEnvelopeCorrupt("header is not an object")
    required = {
        "magic",
        "format_version",
        "purpose",
        "account_domain",
        "service_domain",
        "key_id",
        "nonce",
        "aad",
        "cipher_suite",
        "ciphertext_sha256",
        "plaintext_length",
        "created_at",
    }
    if not required <= set(header):
        raise SecureEnvelopeCorrupt("header is missing required fields")
    if header.get("magic") != MAGIC.decode("ascii"):
        raise SecureEnvelopeCorrupt("header magic is invalid")
    if header.get("format_version") != FORMAT_VERSION:
        raise SecureEnvelopeCorrupt("unsupported format version")
    if header.get("cipher_suite") != CIPHER_SUITE:
        raise SecureEnvelopeCorrupt("unsupported cipher suite")
    if not isinstance(header.get("key_id"), str) or not _KEY_ID.fullmatch(
        header["key_id"]
    ):
        raise SecureEnvelopeCorrupt("key identifier is invalid")
    if header.get("backend") is not None and header["backend"] not in BACKENDS:
        raise SecureEnvelopeCorrupt("storage backend is unknown")

    nonce = b64url_decode(header.get("nonce"), maximum=MAX_NONCE_BYTES)
    if len(nonce) != MAX_NONCE_BYTES:
        raise SecureEnvelopeCorrupt("nonce length is invalid")

    aad_bytes = b64url_decode(header.get("aad"), maximum=MAX_HEADER_BYTES)
    try:
        aad_payload = json.loads(aad_bytes.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise SecureEnvelopeCorrupt("aad is not canonical JSON") from None
    if not isinstance(aad_payload, dict):
        raise SecureEnvelopeCorrupt("aad is not an object")
    try:
        context = StorageContext(
            purpose=aad_payload["purpose"],
            account_domain=aad_payload["account_domain"],
            service_domain=aad_payload["service_domain"],
            account_ref=aad_payload["account_ref"],
            revision=aad_payload["revision"],
        )
    except (KeyError, TypeError, SecureInputError):
        raise SecureEnvelopeCorrupt("aad scope is invalid") from None
    if aad_payload.get("format_version") != FORMAT_VERSION:
        raise SecureEnvelopeCorrupt("aad format version is invalid")
    if canonical_aad(context) != aad_bytes:
        raise SecureEnvelopeCorrupt("aad is not canonical")
    if context.purpose != header.get("purpose"):
        raise SecureEnvelopeCorrupt("header purpose does not match aad")
    if context.account_domain != header.get("account_domain"):
        raise SecureEnvelopeCorrupt("header account domain does not match aad")
    if context.service_domain != header.get("service_domain"):
        raise SecureEnvelopeCorrupt("header service domain does not match aad")

    digest = header.get("ciphertext_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise SecureEnvelopeCorrupt("ciphertext digest is invalid")
    length = header.get("plaintext_length")
    if (
        not isinstance(length, int)
        or isinstance(length, bool)
        or not 0 <= length <= MAX_PLAINTEXT_BYTES
    ):
        raise SecureEnvelopeCorrupt("plaintext length is invalid")
    _parse_timestamp(header.get("created_at"), "created_at")
    if header.get("key_created_at") is not None:
        _parse_timestamp(header["key_created_at"], "key_created_at")
    return context, aad_bytes


def parse_envelope(data: bytes) -> Envelope:
    """Parse and structurally verify an envelope without decrypting it."""
    if not isinstance(data, bytes):
        raise SecureEnvelopeCorrupt("envelope must be bytes")
    if len(data) < len(MAGIC) + 4 + 2 + MIN_CIPHERTEXT_BYTES:
        raise SecureEnvelopeCorrupt("envelope is truncated")
    if data[: len(MAGIC)] != MAGIC:
        raise SecureEnvelopeCorrupt("envelope magic is invalid")
    header_length = int.from_bytes(
        data[len(MAGIC) : len(MAGIC) + 4], "big", signed=False
    )
    if not 2 <= header_length <= MAX_HEADER_BYTES:
        raise SecureEnvelopeCorrupt("header length is invalid")
    header_start = len(MAGIC) + 4
    header_end = header_start + header_length
    if header_end > len(data):
        raise SecureEnvelopeCorrupt("envelope is truncated")
    try:
        header = json.loads(data[header_start:header_end].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SecureEnvelopeCorrupt("header is not UTF-8 JSON") from None
    context, aad_bytes = _validate_header(header)
    ciphertext = data[header_end:]
    if len(ciphertext) < MIN_CIPHERTEXT_BYTES:
        raise SecureEnvelopeCorrupt("ciphertext is truncated")
    if hashlib.sha256(ciphertext).hexdigest() != header["ciphertext_sha256"]:
        raise SecureEnvelopeCorrupt("ciphertext digest mismatch")
    return Envelope(header=header, ciphertext=ciphertext, context=context, aad=aad_bytes)


def pack_envelope(header: dict[str, Any], ciphertext: bytes) -> bytes:
    """Serialize a header and ciphertext.  Used by the provider and tests."""
    try:
        header_bytes = json.dumps(
            header, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise SecureEnvelopeCorrupt("header cannot be serialized") from None
    if not 2 <= len(header_bytes) <= MAX_HEADER_BYTES:
        raise SecureEnvelopeCorrupt("header is too large")
    return MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes + ciphertext


def public_header(header: dict[str, Any]) -> dict[str, Any]:
    """Return the inspectable header only; ciphertext is never included."""
    keys = (
        "format_version",
        "backend",
        "purpose",
        "account_domain",
        "service_domain",
        "key_id",
        "revision",
        "account_ref",
        "plaintext_length",
        "created_at",
        "key_created_at",
    )
    result = {key: header[key] for key in keys if key in header}
    return result


def expected_context(
    *,
    purpose: str,
    account_domain: str,
    service_domain: str,
    account_ref: str,
    revision: int = 0,
) -> StorageContext:
    return StorageContext(
        purpose=purpose,
        account_domain=account_domain,
        service_domain=service_domain,
        account_ref=account_ref,
        revision=revision,
    )


def check_expected(
    actual: StorageContext,
    expected: StorageContext | None,
    expected_revision: int | None = None,
) -> None:
    """Compare caller expectations with the authenticated scope."""
    if expected is not None:
        if actual.purpose != expected.purpose:
            raise SecurePurposeMismatch("purpose differs")
        if actual.account_domain != expected.account_domain:
            raise SecureAccountMismatch("account domain differs")
        if actual.service_domain != expected.service_domain:
            raise SecureServiceMismatch("service domain differs")
        if actual.account_ref != expected.account_ref:
            raise SecureAccountMismatch("account reference differs")
    if expected_revision is not None and actual.revision != expected_revision:
        # Local import avoids an import cycle with errors at module load.
        from .errors import SecureRevisionConflict

        raise SecureRevisionConflict("revision differs")


def inspect_envelope(data: bytes) -> dict[str, Any]:
    envelope = parse_envelope(data)
    header = public_header(envelope.header)
    header["revision"] = envelope.context.revision
    header["account_ref"] = envelope.context.account_ref
    return header
