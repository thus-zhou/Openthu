"""Controlled temporary storage for derived media.

A derived file is written to a private, owner-only directory, through a single
exclusive descriptor, into a ``.part`` name and then atomically renamed. A
failure (including a full disk) leaves no sendable half-product behind: the
partial file is unlinked and no reference is ever registered.

Cleanup only ever touches names this module created, and never follows a
symbolic link. A reference that is still in use is protected by the caller's
``keep`` set. "Cleanup" means closing descriptors and removing files; it is not
a claim of physical erasure on every storage medium.
"""
from __future__ import annotations

import atexit
import errno
import hashlib
import os
import re
import secrets
import stat
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .boundary import ensure_within
from .errors import MediaTempInsecure, MediaTempUnavailable, MediaWriteFailed

NAME_PATTERN = re.compile(r"^media-[0-9a-f]{32}(\.[A-Za-z0-9]{1,8})?(\.part)?$")
ALLOWED_SUFFIXES = {
    ".png", ".jpg", ".webp", ".pdf", ".zip", ".gz", ".json", ".csv",
    ".txt", ".md", ".docx", ".pptx", ".xlsx", ".bin",
}
_AREAS: "weakref.WeakSet[TempArea]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False


@dataclass(frozen=True)
class StoredFile:
    path: Path
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class CleanupReport:
    removed: int = 0
    kept: int = 0
    failed: int = 0
    links_removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "removed": self.removed,
            "kept": self.kept,
            "failed": self.failed,
            "links_removed": self.links_removed,
        }


def _register_exit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    _ATEXIT_REGISTERED = True

    def _flush() -> None:  # pragma: no cover - exercised through close()
        for area in list(_AREAS):
            try:
                area.close()
            except Exception:
                continue

    atexit.register(_flush)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(
        getattr(st, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class TempArea:
    """A single controlled directory with bounded write and cleanup rules."""

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown path
        try:
            if not getattr(self, "_closed", True):
                self.close()
        except Exception:
            pass

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        write_hook: Callable[[int, bytes], int] | None = None,
    ) -> None:
        self.root = self._prepare_root(root)
        self._write_hook = write_hook or os.write
        self.keep_provider: Callable[[], set[Path]] | None = None
        self._files: set[Path] = set()
        self._persistent: set[Path] = set()
        self._closed = False
        self.written = 0
        self.deleted = 0
        _AREAS.add(self)
        _register_exit()

    # -- construction -------------------------------------------------
    @staticmethod
    def _prepare_root(value: object) -> Path:
        try:
            root = Path(os.fspath(value))  # type: ignore[arg-type]
        except TypeError:
            raise MediaTempInsecure("root_type") from None
        if not root.is_absolute():
            raise MediaTempInsecure("relative_root")
        if root.exists():
            info = os.lstat(root)
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise MediaTempInsecure("root_symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise MediaTempInsecure("root_not_directory")
        else:
            try:
                root.mkdir(parents=True, mode=0o700, exist_ok=True)
            except OSError:
                raise MediaTempUnavailable("mkdir") from None
        if os.name != "nt":
            info = os.lstat(root)
            if info.st_uid != os.getuid():
                raise MediaTempInsecure("root_owner")
            if info.st_mode & 0o077:
                # Refuse rather than chmod: a wider directory may be shared on
                # purpose, and silently narrowing it would change user state.
                raise MediaTempInsecure("root_mode")
        elif not os.access(root, os.W_OK | os.X_OK):
            raise MediaTempInsecure("root_not_writable")
        return root

    # -- writing ------------------------------------------------------
    def _suffix(self, suffix: str | None) -> str:
        if not suffix:
            return ".bin"
        suffix = suffix.lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        if suffix not in ALLOWED_SUFFIXES:
            raise MediaTempInsecure("suffix")
        return suffix

    def _new_name(self, suffix: str) -> str:
        return f"media-{secrets.token_hex(16)}{self._suffix(suffix)}"

    def write_bytes(self, data: bytes, *, suffix: str = ".bin") -> StoredFile:
        if not isinstance(data, (bytes, bytearray)):
            raise MediaTempInsecure("payload_type")
        return self.write_stream((bytes(data),), suffix=suffix)

    def write_stream(
        self, chunks: Iterable[bytes], *, suffix: str = ".bin"
    ) -> StoredFile:
        if self._closed:
            raise MediaTempUnavailable("closed")
        final_name = self._new_name(suffix)
        final_path = self.root / final_name
        part_path = self.root / (final_name + ".part")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(part_path, flags, 0o600)
        except OSError:
            raise MediaTempUnavailable("create") from None
        hasher = hashlib.sha256()
        total = 0
        try:
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise MediaTempInsecure("chunk_type")
                if not chunk:
                    continue
                hasher.update(chunk)
                self._write_all(fd, bytes(chunk))
                total += len(chunk)
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            self._unlink_quietly(part_path)
            raise
        os.close(fd)
        try:
            os.replace(part_path, final_path)
        except OSError:
            self._unlink_quietly(part_path)
            raise MediaWriteFailed("rename") from None
        _fsync_directory(self.root)
        if os.name != "nt":
            try:
                os.chmod(final_path, 0o600)
            except OSError:
                pass
        self._files.add(final_path)
        self.written += 1
        return StoredFile(final_path, total, hasher.hexdigest())

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = self._write_hook(fd, view)
            except OSError as error:
                code = "no_space" if error.errno == errno.ENOSPC else "io"
                raise MediaWriteFailed(code) from None
            if not isinstance(written, int) or written <= 0:
                raise MediaWriteFailed("short_write")
            view = view[written:]

    def _unlink_quietly(self, path: Path) -> None:
        try:
            os.unlink(path)
            self.deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # -- lifecycle ----------------------------------------------------
    def mark_persistent(self, path: Path) -> None:
        self._persistent.add(Path(path))

    def discard(self, path: Path) -> bool:
        target = ensure_within(self.root, Path(path))
        self._files.discard(target)
        self._persistent.discard(target)
        before = self.deleted
        self._unlink_quietly(target)
        return self.deleted > before

    def sweep(self, *, keep: Iterable[Path] = (), min_age_seconds: float = 0) -> CleanupReport:
        keep_resolved = {Path(item) for item in keep}
        removed = kept = failed = links = 0
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            raise MediaTempUnavailable("list") from None
        now = _now()
        for entry in entries:
            name = entry.name
            if not NAME_PATTERN.fullmatch(name):
                # Not ours: never delete something a user put here.
                kept += 1
                continue
            try:
                info = os.lstat(entry)
            except OSError:
                failed += 1
                continue
            if stat.S_ISLNK(info.st_mode):
                self._unlink_quietly(entry)
                links += 1
                continue
            if entry in keep_resolved or entry in self._persistent:
                kept += 1
                continue
            if name.endswith(".part"):
                if min_age_seconds and now - info.st_mtime < min_age_seconds:
                    kept += 1
                    continue
                self._unlink_quietly(entry)
                self._files.discard(entry)
                removed += 1
                continue
            if entry in self._files and min_age_seconds:
                if now - info.st_mtime < min_age_seconds:
                    kept += 1
                    continue
            if not stat.S_ISREG(info.st_mode):
                kept += 1
                continue
            before = self.deleted
            self._unlink_quietly(entry)
            self._files.discard(entry)
            if self.deleted > before:
                removed += 1
            else:
                failed += 1
        return CleanupReport(removed, kept, failed, links)

    def close(self, *, keep: Iterable[Path] | None = None) -> CleanupReport:
        """Best-effort process-exit cleanup; never touches foreign files.

        ``keep`` protects derived files whose retention outlives this process
        (``EXPLICIT_DELETE``/``PROJECT_CACHE``). ``SESSION`` files are removed.
        """
        if self._closed:
            return CleanupReport()
        if keep is None:
            try:
                keep = self.keep_provider() if self.keep_provider else ()
            except Exception:
                keep = ()
        report = self.sweep(keep=keep)
        self._closed = True
        return report

    def contains(self, path: Path) -> bool:
        try:
            ensure_within(self.root, Path(path))
        except Exception:
            return False
        return True


def _now() -> float:
    import time

    return time.time()


__all__ = [
    "ALLOWED_SUFFIXES",
    "CleanupReport",
    "NAME_PATTERN",
    "StoredFile",
    "TempArea",
]
