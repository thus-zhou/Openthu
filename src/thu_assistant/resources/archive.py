"""Archive writer for selected course resources.

The user chooses the destination directory.  Every file is written under that
directory with a sanitised course folder and a title plus a token over
``(uid, version)``.  A remote title cannot create a parent directory, an
absolute path or a reserved device name.

Writes go to a temporary file in the same directory, are re-read and hash
checked, then atomically renamed into place.  An existing file is never
overwritten: identical content is reused, different content is a conflict that
keeps the original file and reports the new content for a separate decision.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

from .errors import (
    ResourceDiskFull,
    ResourceFileLocked,
    ResourceHashConflict,
    ResourceInvalidArgument,
    ResourceTitleUnsafe,
    ResourceWriteFailed,
)
from .identity import archive_relative_path
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import ResourceEntry

_READ_CHUNK = 1024 * 1024


def validate_destination(value: object) -> Path:
    """An absolute destination directory, never a relative path."""
    if value is None or not isinstance(value, (str, os.PathLike)):
        raise ResourceInvalidArgument("destination")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ResourceInvalidArgument("destination_not_absolute")
    if "\x00" in str(path):
        raise ResourceInvalidArgument("destination")
    return path


def _hash_file(path: Path, *, max_bytes: int) -> tuple[str, int] | None:
    """Stream a bounded hash; ``None`` when the file is not a regular file."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None
    if info.st_size > max_bytes:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest(), info.st_size


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class Archive:
    """Writes into one explicitly chosen download directory."""

    def __init__(self, root: object, *, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
        self.root = validate_destination(root)
        self.limits = limits

    def relative_path(self, entry: ResourceEntry) -> str:
        return archive_relative_path(
            uid=entry.stable_resource_uid,
            version=entry.version,
            course_id=entry.course_id,
            course_name=entry.course_name,
            title=entry.title,
            max_title_chars=self.limits.max_title_chars,
        )

    def target(self, entry: ResourceEntry) -> Path:
        relative = self.relative_path(entry)
        candidate = self.root / relative
        # The components are already sanitised; this closes a hand-edited state
        # or a platform separator mismatch.
        resolved = candidate.resolve(strict=False)
        root = self.root.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ResourceTitleUnsafe("archive_outside_destination") from None
        if resolved == root:
            raise ResourceTitleUnsafe("archive_outside_destination")
        return resolved

    def inspect(self, entry: ResourceEntry, *, expected_sha256: str | None = None) -> dict[str, object]:
        target = self.target(entry)
        if not target.exists() and not target.is_symlink():
            return {"action": "NEW", "sha256": None, "bytes": None, "relative_path": self.relative_path(entry)}
        if target.is_symlink() or not target.is_file():
            return {
                "action": "CONFLICT",
                "sha256": None,
                "bytes": None,
                "relative_path": self.relative_path(entry),
                "reason": "TARGET_NOT_REGULAR",
            }
        hashed = _hash_file(target, max_bytes=self.limits.max_attachment_bytes)
        if hashed is None:
            return {
                "action": "CONFLICT",
                "sha256": None,
                "bytes": None,
                "relative_path": self.relative_path(entry),
                "reason": "TARGET_UNREADABLE_OR_TOO_LARGE",
            }
        digest, size = hashed
        expected = expected_sha256 if expected_sha256 is not None else entry.content_sha256
        if expected is not None:
            action = "REUSE" if digest == expected else "CONFLICT"
            return {
                "action": action,
                "sha256": digest,
                "bytes": size,
                "relative_path": self.relative_path(entry),
                "reason": None if action == "REUSE" else "CONTENT_DIFFERS",
            }
        # The same version token names the same source version, so an existing
        # file is most likely our own earlier write.  Without a known hash it
        # is still never overwritten; the caller sees a review state.
        return {
            "action": "REVIEW_EXISTING",
            "sha256": digest,
            "bytes": size,
            "relative_path": self.relative_path(entry),
            "reason": "CONTENT_UNVERIFIED",
        }

    def write(self, entry: ResourceEntry, data: bytes) -> dict[str, object]:
        if not isinstance(data, bytes):
            raise ResourceInvalidArgument("data")
        if len(data) > self.limits.max_attachment_bytes:
            raise ResourceInvalidArgument("attachment_too_large")
        digest = hashlib.sha256(data).hexdigest()
        target = self.target(entry)
        relative = self.relative_path(entry)
        existing = self.inspect(entry, expected_sha256=digest)
        if existing["action"] == "REUSE":
            return {
                "status": "reused",
                "sha256": digest,
                "bytes": len(data),
                "relative_path": relative,
                "verified": True,
            }
        if existing["action"] in ("CONFLICT", "REVIEW_EXISTING"):
            raise ResourceHashConflict("target_exists")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise ResourceWriteFailed("mkdir") from None
        parent = target.parent.resolve(strict=True)
        try:
            parent.relative_to(self.root.resolve(strict=True))
        except ValueError:
            raise ResourceTitleUnsafe("archive_outside_destination") from None
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            written_hash = _hash_file(temporary, max_bytes=self.limits.max_attachment_bytes)
            if written_hash is None or written_hash[0] != digest or written_hash[1] != len(data):
                raise ResourceWriteFailed("verify")
            if target.exists() or target.is_symlink():
                # Another writer won the race; never overwrite it.
                again = self.inspect(entry, expected_sha256=digest)
                if again["action"] == "REUSE":
                    return {
                        "status": "reused",
                        "sha256": digest,
                        "bytes": len(data),
                        "relative_path": relative,
                        "verified": True,
                    }
                raise ResourceHashConflict("target_appeared")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except (ResourceHashConflict, ResourceWriteFailed, ResourceTitleUnsafe):
            raise
        except OSError as error:
            code = getattr(error, "errno", None)
            if code in (getattr(os, "ENOSPC", 28), getattr(os, "EDQUOT", 122)):
                raise ResourceDiskFull("no_space") from None
            if code in (getattr(os, "EACCES", 13), getattr(os, "EPERM", 1), getattr(os, "EBUSY", 16)):
                raise ResourceFileLocked("locked") from None
            raise ResourceWriteFailed("io") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink()
            except OSError:
                pass
        return {
            "status": "written",
            "sha256": digest,
            "bytes": len(data),
            "relative_path": relative,
            "verified": True,
        }


__all__ = ["Archive", "validate_destination"]
