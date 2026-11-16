"""Atomic protected-record store, key rotation and legacy DPAPI recognition.

Records live in an active ``records-*`` directory selected by an atomically
replaced ``store.json`` pointer.  Rotation writes and verifies a complete new
directory before switching that pointer; the previous directory and key are
retained for rollback.  No operation deletes old ciphertext automatically.
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .envelope import (
    MAX_PLAINTEXT_BYTES,
    StorageContext,
    inspect_envelope,
    parse_envelope,
    public_header,
)
from .errors import (
    SecureDataDirError,
    SecureDirPermissionError,
    SecureEnvelopeCorrupt,
    SecureInputError,
    SecureInvalidRecordId,
    SecureKeyLocationError,
    SecureKeyMismatch,
    SecureKeyNotConfigured,
    SecureKeyUnavailable,
    SecureLegacyFormat,
    SecureLegacyUnavailable,
    SecurePayloadInvalid,
    SecureRecordNotFound,
    SecureRevisionConflict,
    SecureRollbackUnavailable,
    SecureRotationIncomplete,
    SecureRotationNoop,
    SecureStoreBusy,
    SecureStoreCorrupt,
    SecureStoreError,
    SecureWriteError,
)
from .providers import AeadProvider, load_provider, provider_from_environment

_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECORDS_DIR = re.compile(r"^records-[A-Za-z0-9_-]{1,120}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAX_POINTER_BYTES = 64 * 1024
_LOCK_TIMEOUT = 5.0
_NAMESPACE_CONTEXT = StorageContext(
    purpose="INDEX",
    account_domain="SYSTEM",
    service_domain="LOCAL",
    account_ref="none",
    revision=1,
)

_replace = os.replace
_fsync_file = os.fsync


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        _fsync_file(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _atomic_write(path: Path, data: bytes) -> None:
    """Durably replace one file; a failure leaves the previous file in place."""
    if not isinstance(data, bytes):
        raise SecureWriteError("data must be bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created = True
        try:
            _write_all(fd, data)
            _fsync_file(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        _replace(temporary, path)
        _fsync_directory(path.parent)
    except SecureStoreError:
        raise
    except (OSError, ValueError):
        raise SecureWriteError("atomic write failed") from None
    finally:
        if created:
            try:
                temporary.unlink()
            except OSError:
                pass


class _Lock:
    """Small cross-process lock; the data directory is already owner-only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = open(self.path, "a+b")
            self._file.seek(0, os.SEEK_END)
            if not self._file.tell():
                self._file.write(b"0")
                self._file.flush()
            if os.name == "nt":
                import msvcrt

                deadline = _monotonic() + _LOCK_TIMEOUT
                while True:
                    try:
                        self._file.seek(0)
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                        return self
                    except OSError:
                        if _monotonic() >= deadline:
                            raise SecureStoreBusy("store is busy") from None
                        import time

                        time.sleep(0.05)
            else:
                import fcntl
                import time

                deadline = _monotonic() + _LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(
                            self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                        return self
                    except OSError:
                        if _monotonic() >= deadline:
                            raise SecureStoreBusy("store is busy") from None
                        time.sleep(0.05)
        except SecureStoreError:
            if self._file:
                self._file.close()
                self._file = None
            raise
        except OSError:
            if self._file:
                self._file.close()
                self._file = None
            raise SecureWriteError("store lock cannot be acquired") from None

    def __exit__(self, exc_type, exc, tb):
        file = self._file
        self._file = None
        if file is None:
            return False
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            file.close()
        return False


def _monotonic() -> float:
    import time

    return time.monotonic()


def validate_key_separation(key_path: Path, data_dir: Path) -> None:
    key = Path(key_path).expanduser()
    data = Path(data_dir).expanduser()
    if not key.is_absolute() or not data.is_absolute():
        raise SecureKeyLocationError("key and data locations must be absolute")
    key_resolved = key.resolve(strict=False)
    data_resolved = data.resolve(strict=False)
    if key_resolved == data_resolved or data_resolved in key_resolved.parents:
        raise SecureKeyLocationError("key file must live outside the data directory")


@dataclass(frozen=True)
class LegacyRecord:
    format: str
    plaintext: bytes


class SecureStore:
    """Protected records with authenticated context and explicit key rotation."""

    def __init__(
        self,
        provider: AeadProvider,
        data_dir: str | os.PathLike[str],
        *,
        retired_providers: tuple[AeadProvider, ...] = (),
    ) -> None:
        if not hasattr(provider, "key_id") or not hasattr(provider, "seal"):
            raise SecureInputError("a secure provider is required")
        self.provider = provider
        self.data_dir = Path(data_dir).expanduser()
        if not self.data_dir.is_absolute():
            raise SecureDataDirError("data directory must be absolute")
        key_path = getattr(provider, "key_path", None)
        if key_path is not None:
            validate_key_separation(Path(key_path), self.data_dir)
        self._providers: dict[str, AeadProvider] = {provider.key_id: provider}
        for retired in retired_providers:
            self._providers.setdefault(retired.key_id, retired)
        self._prepare_data_dir()
        self._pointer_path = self.data_dir / "store.json"
        self._pointer = self._load_or_create_pointer()

    @property
    def records_dir(self) -> Path:
        directory = self.data_dir / self._pointer["records_dir"]
        if directory.is_symlink() or not directory.is_dir():
            raise SecureStoreCorrupt("active records directory is unavailable")
        return directory

    def _prepare_data_dir(self) -> None:
        if self.data_dir.is_symlink():
            raise SecureDataDirError("data directory cannot be a link")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise SecureDataDirError("data directory cannot be created") from None
        try:
            info = self.data_dir.lstat()
        except OSError:
            raise SecureDataDirError("data directory cannot be inspected") from None
        if not stat.S_ISDIR(info.st_mode):
            raise SecureDataDirError("data directory is not a directory")
        if os.name != "nt":
            if info.st_uid != os.geteuid():
                raise SecureDirPermissionError("data directory owner is wrong")
            if info.st_mode & 0o022:
                raise SecureDirPermissionError("data directory is writable by others")

    def _load_or_create_pointer(self) -> dict[str, Any]:
        if self._pointer_path.is_symlink():
            raise SecureStoreCorrupt("store pointer cannot be a link")
        if self._pointer_path.exists():
            try:
                raw = self._pointer_path.read_bytes()
            except OSError:
                raise SecureStoreCorrupt("store pointer cannot be read") from None
            if len(raw) > _MAX_POINTER_BYTES:
                raise SecureStoreCorrupt("store pointer is too large")
            try:
                pointer = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise SecureStoreCorrupt("store pointer is not JSON") from None
            self._validate_pointer(pointer)
            directory = self.data_dir / pointer["records_dir"]
            if directory.is_symlink() or not directory.is_dir():
                raise SecureStoreCorrupt("active records directory is missing")
            self._namespace_key = self._open_namespace(pointer)
            return pointer
        if list(self.data_dir.glob("records-*")):
            raise SecureStoreCorrupt("records exist without a pointer")
        directory = self.data_dir / (
            f"records-{self.provider.key_id}-{_stamp()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            directory.mkdir(mode=0o700)
        except OSError:
            raise SecureStoreCorrupt("records directory cannot be created") from None
        self._namespace_key = os.urandom(32)
        namespace = self.provider.seal(self._namespace_key, _NAMESPACE_CONTEXT)
        pointer = {
            "format_version": 1,
            "active_key_id": self.provider.key_id,
            "records_dir": directory.name,
            "namespace_b64": base64.b64encode(namespace).decode("ascii"),
            "previous": None,
            "updated_at": _utc_now(),
        }
        self._write_pointer(pointer)
        return pointer

    def _open_namespace(self, pointer: dict[str, Any]) -> bytes:
        provider = self._providers.get(pointer["active_key_id"])
        if provider is None:
            raise SecureKeyUnavailable("active store key is not available")
        try:
            raw = base64.b64decode(pointer["namespace_b64"], validate=True)
            if len(raw) > _MAX_POINTER_BYTES:
                raise ValueError
            plaintext = provider.unseal(raw, _NAMESPACE_CONTEXT).plaintext
        except SecureStoreError:
            raise
        except (ValueError, TypeError):
            raise SecureStoreCorrupt("account namespace is invalid") from None
        if len(plaintext) != 32:
            raise SecureStoreCorrupt("account namespace has the wrong length")
        return plaintext

    @staticmethod
    def _validate_pointer(pointer: Any) -> None:
        if not isinstance(pointer, dict) or pointer.get("format_version") != 1:
            raise SecureStoreCorrupt("store pointer format is invalid")
        if not isinstance(pointer.get("active_key_id"), str) or not _KEY_ID.fullmatch(
            pointer["active_key_id"]
        ):
            raise SecureStoreCorrupt("store pointer key is invalid")
        if not isinstance(pointer.get("records_dir"), str) or not _RECORDS_DIR.fullmatch(
            pointer["records_dir"]
        ):
            raise SecureStoreCorrupt("store pointer directory is invalid")
        namespace = pointer.get("namespace_b64")
        if not isinstance(namespace, str) or not namespace:
            raise SecureStoreCorrupt("store pointer namespace is missing")
        try:
            base64.b64decode(namespace, validate=True)
        except (ValueError, TypeError):
            raise SecureStoreCorrupt("store pointer namespace is invalid") from None
        previous = pointer.get("previous")
        if previous is not None:
            if not isinstance(previous, dict):
                raise SecureStoreCorrupt("store pointer previous entry is invalid")
            if not isinstance(previous.get("key_id"), str) or not _KEY_ID.fullmatch(
                previous["key_id"]
            ):
                raise SecureStoreCorrupt("store pointer previous key is invalid")
            if not isinstance(
                previous.get("records_dir"), str
            ) or not _RECORDS_DIR.fullmatch(previous["records_dir"]):
                raise SecureStoreCorrupt("store pointer previous directory is invalid")
            previous_namespace = previous.get("namespace_b64")
            if not isinstance(previous_namespace, str) or not previous_namespace:
                raise SecureStoreCorrupt("store pointer previous namespace is missing")
            try:
                base64.b64decode(previous_namespace, validate=True)
            except (ValueError, TypeError):
                raise SecureStoreCorrupt(
                    "store pointer previous namespace is invalid"
                ) from None

    def _write_pointer(self, pointer: dict[str, Any]) -> None:
        self._validate_pointer(pointer)
        try:
            payload = json.dumps(
                pointer, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        except (TypeError, ValueError):
            raise SecureStoreCorrupt("store pointer cannot be serialized") from None
        _atomic_write(self._pointer_path, payload)

    def _lock_path(self) -> Path:
        return self.data_dir / ".store.lock"

    def _record_path(self, record_id: str) -> Path:
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            raise SecureInvalidRecordId("record identifier is invalid")
        return self.records_dir / (record_id + ".secure")

    @staticmethod
    def _check_record_file(path: Path) -> None:
        if path.is_symlink():
            raise SecureStoreCorrupt("record cannot be a link")
        if path.exists() and not path.is_file():
            raise SecureStoreCorrupt("record is not a regular file")

    def _ensure_active_key(self) -> None:
        if self._pointer["active_key_id"] != self.provider.key_id:
            raise SecureKeyMismatch("provider does not match the active store key")

    def inspect(self, record_id: str) -> dict[str, Any]:
        path = self._record_path(record_id)
        self._check_record_file(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SecureRecordNotFound("record is missing") from None
        except OSError:
            raise SecureStoreCorrupt("record cannot be read") from None
        result = inspect_envelope(raw)
        result["record_id"] = record_id
        return result

    def revision(self, record_id: str) -> int:
        return int(self.inspect(record_id)["revision"])

    def write(
        self,
        record_id: str,
        plaintext: bytes,
        *,
        purpose: str,
        account_domain: str,
        service_domain: str,
        account: str = "",
        expected_revision: int | None = None,
    ) -> int:
        path = self._record_path(record_id)
        if not isinstance(plaintext, bytes):
            raise SecureInputError("plaintext must be bytes")
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise SecureInputError("plaintext is too large")
        with _Lock(self._lock_path()):
            self._ensure_active_key()
            self._check_record_file(path)
            previous_revision = 0
            if path.exists():
                try:
                    existing = parse_envelope(path.read_bytes())
                except OSError:
                    raise SecureStoreCorrupt("existing record cannot be read") from None
                previous_revision = existing.context.revision
                if (
                    expected_revision is not None
                    and expected_revision != previous_revision
                ):
                    raise SecureRevisionConflict("revision differs")
            elif expected_revision not in (None, 0):
                raise SecureRevisionConflict("record does not exist")
            account_ref = self.provider.account_reference(
                account,
                purpose=purpose,
                account_domain=account_domain,
                service_domain=service_domain,
                namespace_key=self._namespace_key,
            )
            context = StorageContext(
                purpose=purpose,
                account_domain=account_domain,
                service_domain=service_domain,
                account_ref=account_ref,
                revision=previous_revision + 1,
            )
            sealed = self.provider.seal(plaintext, context)
            _atomic_write(path, sealed)
            return context.revision

    def read(
        self,
        record_id: str,
        *,
        purpose: str,
        account_domain: str,
        service_domain: str,
        account: str = "",
        expected_revision: int | None = None,
    ) -> bytes:
        path = self._record_path(record_id)
        self._check_record_file(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise SecureRecordNotFound("record is missing") from None
        except OSError:
            raise SecureStoreCorrupt("record cannot be read") from None
        envelope = parse_envelope(raw)
        provider = self._providers.get(envelope.header.get("key_id"))
        if provider is None:
            raise SecureKeyUnavailable("record key is not available")
        account_ref = provider.account_reference(
            account,
            purpose=purpose,
            account_domain=account_domain,
            service_domain=service_domain,
            namespace_key=self._namespace_key,
        )
        expected = StorageContext(
            purpose=purpose,
            account_domain=account_domain,
            service_domain=service_domain,
            account_ref=account_ref,
            revision=envelope.context.revision,
        )
        return provider.unseal(
            raw, expected, expected_revision=expected_revision
        ).plaintext

    def write_json(
        self,
        record_id: str,
        value: Any,
        *,
        purpose: str,
        account_domain: str,
        service_domain: str,
        account: str = "",
        expected_revision: int | None = None,
    ) -> int:
        try:
            payload = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise SecureInputError("value is not JSON-serializable") from None
        return self.write(
            record_id,
            payload,
            purpose=purpose,
            account_domain=account_domain,
            service_domain=service_domain,
            account=account,
            expected_revision=expected_revision,
        )

    def read_json(
        self,
        record_id: str,
        *,
        purpose: str,
        account_domain: str,
        service_domain: str,
        account: str = "",
        expected_revision: int | None = None,
    ) -> Any:
        raw = self.read(
            record_id,
            purpose=purpose,
            account_domain=account_domain,
            service_domain=service_domain,
            account=account,
            expected_revision=expected_revision,
        )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SecurePayloadInvalid("record is not UTF-8 JSON") from None

    def preview_rotation(self, new_provider: AeadProvider) -> dict[str, Any]:
        if new_provider.key_id == self._pointer["active_key_id"]:
            raise SecureRotationNoop("new provider is already active")
        purposes: dict[str, int] = {}
        total_bytes = 0
        count = 0
        with _Lock(self._lock_path()):
            for path in sorted(self.records_dir.glob("*.secure")):
                raw = path.read_bytes()
                envelope = parse_envelope(raw)
                purposes[envelope.context.purpose] = (
                    purposes.get(envelope.context.purpose, 0) + 1
                )
                total_bytes += len(raw)
                count += 1
        return {
            "active_key_id": self._pointer["active_key_id"],
            "target_key_id": new_provider.key_id,
            "records": count,
            "bytes": total_bytes,
            "purposes": purposes,
            "requires_rewrite": count,
        }

    def rotate(self, new_provider: AeadProvider) -> dict[str, Any]:
        if new_provider.key_id == self._pointer["active_key_id"]:
            raise SecureRotationNoop("new provider is already active")
        with _Lock(self._lock_path()):
            current = dict(self._pointer)
            source_dir = self.records_dir
            target_dir = self.data_dir / (
                f"records-{new_provider.key_id}-{_stamp()}-{uuid.uuid4().hex[:8]}"
            )
            try:
                target_dir.mkdir(mode=0o700)
            except OSError:
                raise SecureRotationIncomplete(0, 0) from None
            files = sorted(source_dir.glob("*.secure"))
            total = len(files)
            migrated = 0
            try:
                for source in files:
                    raw = source.read_bytes()
                    envelope = parse_envelope(raw)
                    old_provider = self._providers.get(
                        envelope.header.get("key_id")
                    )
                    if old_provider is None:
                        raise SecureKeyUnavailable("old key is not available")
                    plaintext = old_provider.unseal(raw).plaintext
                    context = StorageContext(
                        purpose=envelope.context.purpose,
                        account_domain=envelope.context.account_domain,
                        service_domain=envelope.context.service_domain,
                        account_ref=envelope.context.account_ref,
                        revision=envelope.context.revision,
                    )
                    sealed = new_provider.seal(plaintext, context)
                    verified = new_provider.unseal(sealed, expected=context)
                    if verified.plaintext != plaintext:
                        raise SecureEnvelopeCorrupt("rotation verification failed")
                    _atomic_write(target_dir / source.name, sealed)
                    migrated += 1
                _fsync_directory(target_dir)
                pointer = {
                    "format_version": 1,
                    "active_key_id": new_provider.key_id,
                    "records_dir": target_dir.name,
                    "namespace_b64": base64.b64encode(
                        new_provider.seal(self._namespace_key, _NAMESPACE_CONTEXT)
                    ).decode("ascii"),
                    "previous": {
                        "key_id": current["active_key_id"],
                        "records_dir": current["records_dir"],
                        "namespace_b64": current["namespace_b64"],
                    },
                    "updated_at": _utc_now(),
                }
                self._write_pointer(pointer)
            except Exception:
                raise SecureRotationIncomplete(migrated, total) from None
            self._providers.setdefault(new_provider.key_id, new_provider)
            self.provider = new_provider
            self._pointer = pointer
            return {
                "ok": True,
                "migrated": migrated,
                "target_key_id": new_provider.key_id,
            }

    def rollback(self) -> dict[str, Any]:
        with _Lock(self._lock_path()):
            current = dict(self._pointer)
            previous = current.get("previous")
            if not isinstance(previous, dict):
                raise SecureRollbackUnavailable("no previous store pointer")
            directory = self.data_dir / previous["records_dir"]
            if directory.is_symlink() or not directory.is_dir():
                raise SecureRollbackUnavailable("previous store directory is missing")
            provider = self._providers.get(previous["key_id"])
            if provider is None:
                raise SecureRollbackUnavailable("previous key is not available")
            pointer = {
                "format_version": 1,
                "active_key_id": previous["key_id"],
                "records_dir": previous["records_dir"],
                "namespace_b64": previous["namespace_b64"],
                "previous": None,
                "updated_at": _utc_now(),
            }
            self._write_pointer(pointer)
            self.provider = provider
            self._namespace_key = self._open_namespace(pointer)
            self._pointer = pointer
            return {"ok": True, "active_key_id": pointer["active_key_id"]}

    def health(self) -> dict[str, Any]:
        count = 0
        corrupt = 0
        for path in sorted(self.records_dir.glob("*.secure")):
            count += 1
            try:
                parse_envelope(path.read_bytes())
            except SecureStoreError:
                corrupt += 1
        return {
            "ok": corrupt == 0,
            "backend": self.provider.backend,
            "format_version": self.provider.format_version,
            "cipher_suite": self.provider.cipher_suite,
            "active_key_id": self._pointer["active_key_id"],
            "provider_key_id": self.provider.key_id,
            "records": count,
            "corrupt_records": corrupt,
        }


class LegacyDpapiReader:
    """Explicit recognition of old DPAPI records; never rewrites them."""

    MAGICS = {
        b"LA-DPAPI-1\0": "SESSION",
        b"THU-ACTION-1\0": "ACTION",
        b"THU-TODOS-1\0": "TODO",
    }

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()

    @classmethod
    def identify(cls, data: bytes) -> str | None:
        for magic, label in cls.MAGICS.items():
            if data.startswith(magic):
                return label
        return None

    def read(self) -> LegacyRecord:
        try:
            data = self.path.read_bytes()
        except OSError:
            raise SecureLegacyFormat("legacy record is missing") from None
        label = self.identify(data)
        if label is None:
            raise SecureLegacyFormat("legacy record format is unknown")
        if os.name != "nt":
            raise SecureLegacyUnavailable(
                "Windows DPAPI is required to read this legacy record"
            )
        from learn_assistant.auth import _dpapi_unprotect

        for magic in self.MAGICS:
            if data.startswith(magic):
                plaintext = _dpapi_unprotect(data[len(magic) :])
                if plaintext is None:
                    raise SecureLegacyUnavailable(
                        "legacy record cannot be decrypted for this user"
                    )
                return LegacyRecord(format=label, plaintext=plaintext)
        raise SecureLegacyFormat("legacy record format is unknown")


def open_store_from_environment(
    env: dict[str, str] | None = None,
) -> SecureStore:
    source = os.environ if env is None else env
    key_path = source.get("THU_SECURE_KEY_FILE")
    data_dir = source.get("THU_SECURE_DATA_DIR")
    if not key_path or not data_dir:
        raise SecureKeyNotConfigured(
            "THU_SECURE_KEY_FILE and THU_SECURE_DATA_DIR must be configured"
        )
    provider = provider_from_environment(source)
    return SecureStore(provider, data_dir)


__all__ = [
    "LegacyDpapiReader",
    "LegacyRecord",
    "SecureStore",
    "open_store_from_environment",
    "validate_key_separation",
]
