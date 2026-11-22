"""Bounded JSON-over-stdio boundary for a private Node child process.

Only fixed actions are accepted.  The key location and data directory come from
the child's minimal environment, never from the JSON payload.  Plaintext is
returned only on this private pipe; it is not an MCP response.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

from .envelope import StorageContext, inspect_envelope
from .errors import (
    SecureInputError,
    SecureKeyNotConfigured,
    SecureStoreError,
)
from .providers import provider_from_environment
from .store import validate_key_separation

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PLAINTEXT_BYTES = 1024 * 1024
ACTIONS = frozenset({"health", "inspect", "seal", "unseal"})


def _plaintext(value: Any) -> bytes:
    if not isinstance(value, str):
        raise SecureInputError("plaintext_b64 is required")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise SecureInputError("plaintext_b64 is invalid") from None
    if len(raw) > MAX_PLAINTEXT_BYTES:
        raise SecureInputError("plaintext is too large for the bridge")
    return raw


def _envelope(value: Any) -> bytes:
    if not isinstance(value, str):
        raise SecureInputError("envelope_b64 is required")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise SecureInputError("envelope_b64 is invalid") from None
    if len(raw) > MAX_REQUEST_BYTES:
        raise SecureInputError("envelope is too large for the bridge")
    return raw


def _context(request: dict[str, Any], account_ref: str) -> StorageContext:
    return StorageContext(
        purpose=request.get("purpose"),
        account_domain=request.get("account_domain"),
        service_domain=request.get("service_domain"),
        account_ref=account_ref,
        revision=request.get("revision", 0),
        record_id=request.get("record_id"),
    )


def _provider_and_paths(env: dict[str, str]):
    key_path = env.get("THU_SECURE_KEY_FILE")
    data_dir = env.get("THU_SECURE_DATA_DIR")
    if not key_path or not data_dir:
        raise SecureKeyNotConfigured(
            "THU_SECURE_KEY_FILE and THU_SECURE_DATA_DIR are required"
        )
    validate_key_separation(Path(key_path), Path(data_dir))
    return provider_from_environment(env)


def handle_request(
    request: Any, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Handle one bridge request and return a JSON-safe response."""
    source = os.environ if env is None else env
    try:
        if not isinstance(request, dict):
            raise SecureInputError("request must be an object")
        action = request.get("action")
        if action not in ACTIONS:
            raise SecureInputError("unknown bridge action")
        provider = _provider_and_paths(source)
        if action == "health":
            return {"ok": True, "health": provider.health()}
        if action == "inspect":
            return {
                "ok": True,
                "header": inspect_envelope(_envelope(request.get("envelope_b64"))),
            }
        account = request.get("account", "")
        account_ref = provider.account_reference(
            account,
            purpose=request.get("purpose"),
            account_domain=request.get("account_domain"),
            service_domain=request.get("service_domain"),
        )
        if action == "seal":
            revision = request.get("revision", 0)
            context = _context(request, account_ref)
            sealed = provider.seal(_plaintext(request.get("plaintext_b64")), context)
            return {"ok": True, "envelope_b64": base64.b64encode(sealed).decode("ascii")}
        expected = _context(request, account_ref)
        expected_revision = request.get("expected_revision")
        if expected_revision is not None and (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise SecureInputError("expected_revision is invalid")
        unsealed = provider.unseal(
            _envelope(request.get("envelope_b64")),
            expected,
            expected_revision=expected_revision,
        )
        return {
            "ok": True,
            "plaintext_b64": base64.b64encode(unsealed.plaintext).decode("ascii"),
        }
    except SecureStoreError as exc:
        return {"ok": False, "error": exc.as_error()}
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "SECURE_BRIDGE_FAILED",
                "category": "INTERNAL",
                "retryable": False,
                "automatic_replay_allowed": False,
                "user_action": "CONTACT_SUPPORT",
            },
            "detail": type(exc).__name__,
        }


def subprocess_environment(
    key_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    *,
    backend: str | None = None,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal environment a private Node helper may inherit."""
    source = dict(os.environ)
    if base:
        source.update(base)
    allowed = (
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "USERNAME",
        "USERDOMAIN",
        "PYTHONPATH",
        "PYTHONUTF8",
        "PYTHONNOUSERSITE",
    )
    env = {name: source[name] for name in allowed if name in source}
    env.update(
        {
            "THU_SECURE_KEY_FILE": str(key_path),
            "THU_SECURE_DATA_DIR": str(data_dir),
            "PYTHONUTF8": "1",
        }
    )
    if backend:
        env["THU_SECURE_BACKEND"] = backend
    return env


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        response = {
            "ok": False,
            "error": {
                "code": "SECURE_INVALID_INPUT",
                "category": "INPUT",
                "retryable": False,
                "automatic_replay_allowed": False,
                "user_action": "FIX_INPUT",
            },
        }
        print(json.dumps(response, ensure_ascii=True))
        return 1
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        response = {
            "ok": False,
            "error": {
                "code": "SECURE_INVALID_INPUT",
                "category": "INPUT",
                "retryable": False,
                "automatic_replay_allowed": False,
                "user_action": "FIX_INPUT",
            },
        }
        print(json.dumps(response, ensure_ascii=True))
        return 1
    response = handle_request(request)
    print(json.dumps(response, ensure_ascii=True, sort_keys=True))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
