"""Private line-oriented encryption service for the owned Node gateway.

The Node process spawns this module with a fixed interpreter and module name.
Only the four frozen bridge actions are accepted.  Key location, data directory
and backend come from the child environment, never from a request.  Requests
carry an opaque identifier so a response can be matched to its caller and a
cancelled or timed-out request is never answered twice.

Plaintext exists only on this private pipe.  Nothing here writes to stderr,
logs request bodies or returns plaintext in an error.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any

from .secure_store.bridge import ACTIONS, MAX_REQUEST_BYTES, handle_request
from .secure_store.envelope import parse_envelope

MAX_ID_BYTES = 128
MAX_RESPONSE_BYTES = MAX_REQUEST_BYTES + 64 * 1024
_FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "argv",
        "backend_path",
        "data_dir",
        "env",
        "executable",
        "key_file",
        "key_path",
        "module",
        "path",
        "program",
        "python",
    }
)


def _error(code: str, *, category: str = "INPUT", detail: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "category": category,
            "retryable": False,
            "automatic_replay_allowed": False,
            "user_action": "FIX_INPUT",
        },
    }
    if detail:
        response["detail"] = detail
    return response


def _bound_metadata(action: str, request: dict[str, Any], response: dict[str, Any]) -> None:
    """Attach non-secret envelope metadata used to verify local state."""
    raw = None
    if action == "seal" and isinstance(response.get("envelope_b64"), str):
        import base64

        try:
            raw = base64.b64decode(response["envelope_b64"], validate=True)
        except (ValueError, TypeError):
            return
    elif action in {"unseal", "inspect"} and isinstance(request.get("envelope_b64"), str):
        import base64

        try:
            raw = base64.b64decode(request["envelope_b64"], validate=True)
        except (ValueError, TypeError):
            return
    if raw is None:
        return
    try:
        envelope = parse_envelope(raw)
    except Exception:
        return
    response["header"] = envelope.header
    response["context"] = asdict(envelope.context)


def handle_line(raw: bytes, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Handle one bounded request line and return a JSON-safe response."""
    if len(raw) > MAX_REQUEST_BYTES:
        return _error("SECURE_INVALID_INPUT", detail="request is too large")
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error("SECURE_INVALID_INPUT", detail="request is not JSON")
    if not isinstance(request, dict):
        return _error("SECURE_INVALID_INPUT", detail="request must be an object")
    request_id = request.get("id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id.encode("utf-8")) > MAX_ID_BYTES
    ):
        return _error("SECURE_INVALID_INPUT", detail="request id is invalid")
    action = request.get("action")
    if action not in ACTIONS:
        response = _error("SECURE_INVALID_INPUT", detail="unknown bridge action")
        response["id"] = request_id
        return response
    if _FORBIDDEN_REQUEST_KEYS.intersection(request):
        response = _error(
            "SECURE_INVALID_INPUT", detail="request may not select a program or path"
        )
        response["id"] = request_id
        return response
    response = handle_request(request, env)
    if response.get("ok"):
        _bound_metadata(action, request, response)
    response["id"] = request_id
    return response


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        raw = stdin.readline(MAX_REQUEST_BYTES + 2)
        if not raw:
            return 0
        if len(raw) > MAX_REQUEST_BYTES + 1 or not raw.endswith(b"\n"):
            response = _error("SECURE_INVALID_INPUT", detail="request line is too long")
            stdout.write(json.dumps(response, ensure_ascii=True, sort_keys=True).encode("ascii") + b"\n")
            stdout.flush()
            return 1
        response = handle_line(raw[:-1])
        encoded = json.dumps(response, ensure_ascii=True, sort_keys=True).encode("ascii")
        if len(encoded) > MAX_RESPONSE_BYTES:
            oversized = _error("SECURE_RESPONSE_TOO_LARGE", category="INTERNAL")
            oversized["id"] = response.get("id")
            encoded = json.dumps(oversized, ensure_ascii=True, sort_keys=True).encode("ascii")
        stdout.write(encoded + b"\n")
        stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
