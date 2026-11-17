"""Trusted-host authorization protocol (``host-authorization-v1``).

A grant is issued only after a local host integration observed an explicit
human confirmation for one exact plan.  The grant binds the plan id, account,
business digest, adapter version, one-time challenge, issue time and expiry and
is signed with a host key that never leaves the local protected store.

This module contains pure functions: no filesystem, network or process access.
It never logs a grant, and no grant value is returned by public action results.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid

from ..bridge import CampusError

VERSION = "host-authorization-v1"
CHALLENGE_PATTERN = r"[a-f0-9]{64}"
SIGNATURE_PATTERN = r"[a-f0-9]{64}"
_CHALLENGE = re.compile(CHALLENGE_PATTERN)
_SIGNATURE = re.compile(SIGNATURE_PATTERN)
BOUND_FIELDS = (
    "version",
    "plan_id",
    "account",
    "digest",
    "adapter_version",
    "challenge",
    "source",
    "reference",
    "issued_at",
    "expires_at",
)


def new_challenge() -> str:
    """Return a fresh one-time plan challenge (64 lowercase hex characters)."""
    return secrets.token_hex(32)


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def signed_payload(grant: dict) -> dict:
    return {key: grant.get(key) for key in BOUND_FIELDS}


def sign(payload: dict, key: bytes) -> str:
    return hmac.new(bytes(key), canonical(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def issue(record: dict, key: bytes, *, now: float, ttl_seconds: int = 300, reference: str | None = None) -> dict:
    """Create one signed grant for ``record``; never writes or sends anything."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise CampusError("ACTION_LOG_ENCRYPT_FAILED")
    challenge = record.get("challenge")
    if not isinstance(challenge, str) or not _CHALLENGE.fullmatch(challenge):
        raise CampusError("ACTION_LOG_CORRUPT")
    try:
        issued_at = float(now)
        plan_expiry = float(record["expires_at"])
    except (KeyError, TypeError, ValueError):
        raise CampusError("ACTION_LOG_CORRUPT") from None
    expires_at = min(issued_at + float(ttl_seconds), plan_expiry)
    if expires_at <= issued_at:
        raise CampusError("ACTION_PLAN_EXPIRED")
    payload = {
        "version": VERSION,
        "plan_id": record["operation_id"],
        "account": record["account"],
        "digest": record["digest"],
        "adapter_version": record["adapter_version"],
        "challenge": challenge,
        "source": "human",
        "reference": reference or uuid.uuid4().hex,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    if not isinstance(payload["reference"], str) or not payload["reference"]:
        raise CampusError("SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED")
    return {**payload, "signature": sign(payload, key)}


def verify(grant: dict, record: dict, key: bytes, *, now: float) -> dict:
    """Verify a signed grant against exactly one plan.

    The signature is checked before any binding comparison, so a tampered
    grant never reaches the account, adapter or digest checks.  Every failure
    is a distinct, stable error code; no grant field is copied into the error.
    """
    if not isinstance(grant, dict) or grant.get("version") != VERSION:
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    signature = grant.get("signature")
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    try:
        expected = sign(signed_payload(grant), key)
    except (TypeError, ValueError):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED") from None
    if not hmac.compare_digest(expected, signature):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    if grant.get("account") != record.get("account"):
        raise CampusError("ACTION_ACCOUNT_MISMATCH")
    if grant.get("adapter_version") != record.get("adapter_version"):
        raise CampusError("ACTION_ADAPTER_CHANGED")
    if (
        grant.get("plan_id") != record.get("operation_id")
        or grant.get("digest") != record.get("digest")
        or grant.get("challenge") != record.get("challenge")
    ):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    if grant.get("source") != "human":
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    reference = grant.get("reference")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    issued_at = grant.get("issued_at")
    expires_at = grant.get("expires_at")
    if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    current = float(now)
    if issued_at > current + 60:
        raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    if expires_at <= current:
        raise CampusError("ACTION_PLAN_EXPIRED")
    try:
        if expires_at > float(record["expires_at"]) + 1:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
    except (KeyError, TypeError, ValueError):
        raise CampusError("ACTION_LOG_CORRUPT") from None
    return signed_payload(grant)
