"""The only local file boundary used by the media pipeline.

Every read is checked before any byte reaches a decoder:

* the path must be a plain path, never a URL or a data URL;
* the final component must not be a symbolic link and must be a regular file;
* the current user must have read permission;
* the size must be known and within the caller's limit;
* the descriptor is opened once and re-checked with ``fstat`` so a file swapped
  between the check and the open is rejected instead of read;
* reads are bounded, so a file that grows during the read cannot exceed the
  limit.

``ensure_within`` is the complementary rule for files this module wrote itself:
a derived file must stay under the controlled temporary root, with no symbolic
link anywhere on the path.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Callable, Iterator

from .errors import (
    MediaEmptyFile,
    MediaFileNotFound,
    MediaFileUnreadable,
    MediaInvalidArgument,
    MediaNotRegularFile,
    MediaPathInvalid,
    MediaSizeLimit,
    MediaSourceKindInvalid,
)

URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
CHUNK_SIZE = 1024 * 1024
HEAD_SIZE = 4096
_CONTROL = re.compile(
    r"[\x00-\x1f\x7f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)


def validate_local_path(value: object) -> Path:
    """Return a ``Path`` or raise a stable input error.

    The caller must state that a value is a local path. A URL in the path field
    is a caller bug, not something to download.
    """
    if isinstance(value, bytes):
        raise MediaInvalidArgument("bytes_path")
    if not isinstance(value, (str, os.PathLike)):
        raise MediaInvalidArgument("path_type")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw:
        raise MediaInvalidArgument("empty_path")
    if "\x00" in raw:
        raise MediaPathInvalid("nul_byte")
    if URL_LIKE.match(raw) or raw.lower().startswith(("data:", "http:", "https:")):
        raise MediaSourceKindInvalid("url_in_path")
    return Path(raw)


def sanitize_display_name(value: object, *, fallback: str = "unnamed") -> str:
    """A display label that can never be used as a path or identity.

    Directory components are dropped, control characters removed and the result
    bounded. ``display_name`` is cosmetic; ``media_id`` is the identity.
    """
    text = str(value) if value is not None else ""
    text = text.replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    text = _CONTROL.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text in (".", ".."):
        text = fallback
    return text[:200]


def _readable(st_mode: int) -> bool:
    return bool(st_mode & 0o444)


def _is_reparse(st: os.stat_result) -> bool:
    return bool(
        getattr(st, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


class VerifiedFile:
    """An open, bounded, identity-checked local file."""

    def __init__(
        self,
        path: Path,
        fd: int,
        *,
        size: int,
        mode: int,
        head: bytes,
        display_name: str,
    ) -> None:
        self.path = path
        self.fd = fd
        self.size = size
        self.mode = mode
        self.head = head
        self.display_name = display_name
        self.consumed = 0
        self.digest: str | None = None
        self._closed = False

    def __enter__(self) -> "VerifiedFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                os.close(self.fd)
            except OSError:
                pass

    def iter_chunks(self, *, max_bytes: int | None = None) -> Iterator[bytes]:
        """Yield the file once, enforcing an optional streaming bound."""
        limit = self.size if max_bytes is None else max_bytes
        total = 0
        while True:
            chunk = os.read(self.fd, CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise MediaSizeLimit(f"streamed={total > limit}")
            yield chunk
        self.consumed = total
        if total != self.size:
            # The file changed under us after fstat; refuse rather than guess.
            raise MediaPathInvalid("size_changed")

    def read_all(self) -> bytes:
        return b"".join(self.iter_chunks())

    def copy_to(self, sink: Callable[[bytes], None]) -> tuple[int, str]:
        """Stream into ``sink`` while hashing; returns ``(bytes, sha256)``."""
        hasher = hashlib.sha256()
        total = 0
        for chunk in self.iter_chunks():
            hasher.update(chunk)
            total += len(chunk)
            sink(chunk)
        self.digest = hasher.hexdigest()
        return total, self.digest


def open_verified(
    value: object,
    *,
    max_bytes: int,
    display_name: str | None = None,
) -> VerifiedFile:
    path = validate_local_path(value)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise MediaFileNotFound("missing") from None
    except PermissionError:
        raise MediaFileUnreadable("parent_dir") from None
    except OSError:
        raise MediaFileNotFound("stat_failed") from None

    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise MediaPathInvalid("symlink")
    if not stat.S_ISREG(info.st_mode):
        kind = (
            "directory" if stat.S_ISDIR(info.st_mode)
            else "device" if stat.S_ISCHR(info.st_mode) or stat.S_ISBLK(info.st_mode)
            else "special"
        )
        raise MediaNotRegularFile(kind)
    if info.st_size == 0:
        raise MediaEmptyFile("zero_bytes")
    if info.st_size > max_bytes:
        raise MediaSizeLimit(f"declared={info.st_size > max_bytes}")
    if os.name != "nt" and not _readable(info.st_mode):
        raise MediaFileUnreadable("mode")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow)
    except OSError as error:
        if getattr(error, "errno", None) in (getattr(os, "ELOOP", 40),):
            raise MediaPathInvalid("symlink_race") from None
        if getattr(error, "errno", None) in (13, 1):
            raise MediaFileUnreadable("open") from None
        raise MediaFileNotFound("open_failed") from None

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
            raise MediaPathInvalid("changed_type")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise MediaPathInvalid("changed_identity")
        if opened.st_size != info.st_size:
            raise MediaPathInvalid("changed_size")
    except BaseException:
        os.close(fd)
        raise

    head = b""
    try:
        head = os.read(fd, HEAD_SIZE)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        os.close(fd)
        raise MediaFileNotFound("read_failed") from None

    return VerifiedFile(
        path,
        fd,
        size=info.st_size,
        mode=info.st_mode,
        head=head,
        display_name=sanitize_display_name(
            display_name if display_name is not None else path.name
        ),
    )


def ensure_within(base: Path, candidate: Path) -> Path:
    """Return the resolved ``candidate`` if it is safely under ``base``."""
    base_resolved = Path(base).resolve(strict=True)
    candidate = Path(candidate)
    if candidate.is_symlink():
        raise MediaPathInvalid("derived_symlink")
    current = candidate.parent
    while True:
        if current.is_symlink():
            raise MediaPathInvalid("parent_symlink")
        if current == base_resolved or current.parent == current:
            break
        current = current.parent
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise MediaPathInvalid("outside_root") from None
    return resolved


__all__ = [
    "CHUNK_SIZE",
    "HEAD_SIZE",
    "VerifiedFile",
    "ensure_within",
    "open_verified",
    "sanitize_display_name",
    "validate_local_path",
]
