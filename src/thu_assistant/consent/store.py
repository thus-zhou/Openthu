"""Protected storage for the consent host key and pending signed grants.

Both files use the same protection callable as the action journal
(``ActionStore.protect`` / ``unprotect``): a 2.2 secure-store envelope when a
key is configured, otherwise Windows DPAPI.  A missing or failed provider
never downgrades to plaintext.

The grant file is named by the plan id and is removed when it is taken.  A
grant is therefore single-use even if two processes race: only the process
that wins the local lock receives the file.  ``peek`` reads without consuming
so a transient preflight failure does not destroy the human confirmation.
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
    try:
        descriptor = os.open(str(temporary), flags, 0o600)
    except OSError:
        raise CampusError("STATE_WRITE_FAILED") from None
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
        for attempt in range(20):
            try:
                os.replace(str(temporary), str(path))
                break
            except OSError as error:
                if not (os.name == "nt" and getattr(error, "winerror", None) in (5, 32)) or attempt == 19:
                    raise
                time.sleep(min(0.02, 0.002 * (attempt + 1)))
    except OSError:
        raise CampusError("STATE_WRITE_FAILED") from None
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
        self.root = Path(root) if root is not None else self._default_root(store)

    @staticmethod
    def _default_root(store) -> Path:
        """Keep the production layout, isolate custom and test roots.

        The default journal root is ``<state>/actions-v1`` and the host
        authority lives beside it in ``<state>/consent-v1``.  Any other root
        (tests, an explicit CLI ``--state-dir``) keeps its consent state inside
        that root, so two stores with different roots never share a host key.
        """
        root = Path(store.root)
        if root.name == "actions-v1":
            return root.parent / _DIRECTORY
        return root / _DIRECTORY

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
        try:
            raw = path.read_bytes()
        except OSError:
            raise CampusError("STATE_READ_FAILED") from None
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
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise CampusError("STATE_WRITE_FAILED") from None
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

    def issue_for(self, record: dict, *, now: float | None = None, ttl_seconds: int = 300, reference: str | None = None) -> dict:
        key = self.key(create=True)
        if key is None:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        return protocol.issue(
            record,
            key,
            now=time.time() if now is None else now,
            ttl_seconds=ttl_seconds,
            reference=reference,
        )

    def _read_grant(self, path: Path) -> dict:
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            # A concurrent ``take``/``discard`` may consume the grant between an
            # existence check and this read; that is "no grant", not corruption.
            raise
        except OSError:
            raise CampusError("STATE_READ_FAILED") from None
        if not blob.startswith(GRANT_MAGIC):
            raise CampusError("ACTION_LOG_CORRUPT")
        plaintext = self.unprotect(blob[len(GRANT_MAGIC):])
        if plaintext is None:
            raise CampusError("ACTION_LOG_DECRYPT_FAILED")
        try:
            grant = json.loads(plaintext)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise CampusError("ACTION_LOG_CORRUPT") from None
        return grant

    def _verify(self, grant, record, now) -> dict:
        key = self.key()
        if key is None:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        protocol.verify(grant, record, key, now=time.time() if now is None else now)
        return grant

    def put(self, record: dict, grant: dict) -> bool:
        """Store one pending grant; returns True when an older grant was replaced."""
        if not isinstance(grant, dict) or grant.get("version") != protocol.VERSION:
            raise CampusError("TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED")
        try:
            plaintext = json.dumps(grant, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise CampusError("ACTION_LOG_CORRUPT") from None
        blob = self.protect(plaintext)
        if not blob:
            raise CampusError("ACTION_LOG_ENCRYPT_FAILED")
        path = self.grant_path(record["operation_id"])
        with self.store.lock("consent"):
            replaced = path.exists()
            _write_private(path, GRANT_MAGIC + blob)
        return replaced

    def peek(self, record: dict, *, now: float | None = None) -> dict | None:
        """Read and verify a pending grant without consuming it.

        The read shares the consent lock with ``put``/``take``/``discard`` so a
        concurrent consume cannot make an issued grant look corrupt.  A grant
        that disappears before the read is reported as absent, never as a state
        read failure.
        """
        path = self.grant_path(record["operation_id"])
        try:
            with self.store.lock("consent"):
                if not path.exists():
                    return None
                grant = self._read_grant(path)
        except FileNotFoundError:
            return None
        return self._verify(grant, record, now)

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
                grant = self._read_grant(path)
                path.unlink()
        except FileNotFoundError:
            return None
        except OSError:
            raise CampusError("STATE_WRITE_FAILED") from None
        return self._verify(grant, record, now)

    def verify_signed(self, grant: dict, record: dict, *, now: float | None = None) -> dict:
        return self._verify(grant, record, now)

    def discard(self, operation_id: str) -> bool:
        """Remove a pending grant for one plan; never touches a journal."""
        path = self.grant_path(operation_id)
        try:
            with self.store.lock("consent"):
                if not path.exists():
                    return False
                path.unlink()
                return True
        except FileNotFoundError:
            return False
        except OSError:
            raise CampusError("STATE_WRITE_FAILED") from None

    def pending(self) -> list[str]:
        """Plan ids with an unread grant file; never decrypts or prints values."""
        if not self.root.exists():
            return []
        return sorted(path.stem for path in self.root.glob("*.grant"))
