"""Protected storage for the consent host key and one pending signed grant.

Both files use the same protection callable as the action journal
(``ActionStore.protect`` / ``unprotect``): a 2.2 secure-store envelope when a
key is configured, otherwise Windows DPAPI.  A missing or failed provider
never downgrades to plaintext.

The grant file is named by the plan id and is removed when it is taken.  A
grant is therefore single-use even if two processes race: only the process
that wins the local lock receives the file.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from pathlib import Path

from ..bridge import CampusError
from . import protocol

GRANT_MAGIC = b"THU-CONSENT-1\0"
KEY_MAGIC = b"THU-CONSENT-KEY-1\0"
KEY_BYTES = 32
_DIRECTORY = "consent-v1"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(temporary), flags, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


class HostConsent:
    """Local host authority for one protected-state root."""

    def __init__(self, store=None, root=None):
        if store is None:
            from ..action_store import ActionStore
            store = ActionStore()
        self.store = store
        self.root = Path(root) if root is not None else Path(store.root).parent / _DIRECTORY

    # The journal's protection callable is authoritative and may be replaced by
    # an explicit test codec after construction; read it dynamically.
    @property
    def protect(self):
        return self.store.protect

    @property
    def unprotect(self):
        return self.store.unprotect

    def key_path(self) -> Path:
        return self.root / "host.key"

    def grant_path(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str):
            raise CampusError("INVALID_OPERATION_ID")
        self.store.path(operation_id)  # Reuse the exact id validation.
        return self.root / (operation_id + ".grant")

    def _read_key_file(self) -> bytes:
        path = self.key_path()
        if path.is_symlink():
            raise CampusError("ACTION_LOG_CORRUPT")
        raw = path.read_bytes()
        if not raw.startswith(KEY_MAGIC):
            raise CampusError("ACTION_LOG_CORRUPT")
        key = self.unprotect(raw[len(KEY_MAGIC):])
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
            raise CampusError("ACTION_LOG_DECRYPT_FAILED")
        return bytes(key)

    def key(self, create: bool = False) -> bytes | None:
        """Return the host signing key, optionally creating it exactly once.

        Creation happens only on an explicit human confirmation path.  A key
        that exists but cannot be decrypted is a hard failure, never a reason
        to generate a replacement silently.
        """
        path = self.key_path()
        if path.exists():
            return self._read_key_file()
        if not create:
            return None
        with self.store.lock("consent-key"):
            if path.exists():
                return self._read_key_file()
            self.root.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(KEY_BYTES)
            blob = self.protect(key)
            if not blob:
                raise CampusError("ACTION_LOG_ENCRYPT_FAILED")
            _write_private(path, KEY_MAGIC + blob)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if self._read_key_file() != key:
                raise CampusError("ACTION_LOG_ENCRYPT_FAILED")
            return key

    def issue_for(self, record: dict, *, now: float | None = None, ttl_seconds: int = 300) -> dict:
        key = self.key(create=True)
        if key is None:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        return protocol.issue(record, key, now=time.time() if now is None else now, ttl_seconds=ttl_seconds)

    def put(self, record: dict, grant: dict) -> None:
        if not isinstance(grant, dict) or grant.get("version") != protocol.VERSION:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        try:
            plaintext = json.dumps(grant, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise CampusError("ACTION_LOG_CORRUPT") from None
        blob = self.protect(plaintext)
        if not blob:
            raise CampusError("ACTION_LOG_ENCRYPT_FAILED")
        _write_private(self.grant_path(record["operation_id"]), GRANT_MAGIC + blob)

    def take(self, record: dict, *, now: float | None = None) -> dict | None:
        """Atomically consume and verify the pending grant for this plan.

        Returns ``None`` when no grant was issued.  A grant that was issued for
        another account, plan, digest or challenge is consumed and rejected:
        it can never be retried against the changed plan.
        """
        path = self.grant_path(record["operation_id"])
        try:
            with self.store.lock("consent"):
                if not path.exists():
                    return None
                blob = path.read_bytes()
                path.unlink()
        except FileNotFoundError:
            return None
        if not blob.startswith(GRANT_MAGIC):
            raise CampusError("ACTION_LOG_CORRUPT")
        plaintext = self.unprotect(blob[len(GRANT_MAGIC):])
        if plaintext is None:
            raise CampusError("ACTION_LOG_DECRYPT_FAILED")
        try:
            grant = json.loads(plaintext)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise CampusError("ACTION_LOG_CORRUPT") from None
        key = self.key()
        if key is None:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        protocol.verify(grant, record, key, now=time.time() if now is None else now)
        return grant

    def verify_signed(self, grant: dict, record: dict, *, now: float | None = None) -> dict:
        key = self.key()
        if key is None:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        return protocol.verify(grant, record, key, now=time.time() if now is None else now)

    def pending(self) -> list[str]:
        """Plan ids with an unread grant file; never decrypts or prints values."""
        if not self.root.exists():
            return []
        return sorted(path.stem for path in self.root.glob("*.grant"))
