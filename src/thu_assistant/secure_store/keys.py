"""Platform key material: owner-only files and Windows DPAPI wrapping.

Linux (and other POSIX hosts) use a random 32-byte master key in a separate
owner-only file.  Windows wraps the same random secret with the current user's
DPAPI.  Neither backend derives a key from a host name, user name or machine
identifier, and neither falls back to plaintext.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import (
    SecureBackendUnsupported,
    SecureKeyExists,
    SecureKeyFormatError,
    SecureKeyLocationError,
    SecureKeyNotConfigured,
    SecureKeyPermissionError,
    SecureKeyUnreadable,
)

FILE_KEY_MAGIC = b"THU-KEY-1\0"
DPAPI_KEY_MAGIC = b"THU-DPAPI-KEY-1\0"
KEY_BYTES = 32
MAX_KEY_FILE_BYTES = 4096
_SID = re.compile(r"S-1-[0-9-]+")


@dataclass(frozen=True)
class KeyMaterial:
    secret: bytes
    key_id: str
    backend: str
    created_at: str


def default_backend() -> str:
    return "windows-dpapi" if os.name == "nt" else "file-key"


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise SecureKeyLocationError("key path must be absolute")
    return value


def _key_id(secret: bytes) -> str:
    return "key_" + hashlib.sha256(secret).hexdigest()[:24]


def _created_at(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def _check_posix_parent(path: Path) -> None:
    try:
        info = path.parent.stat()
    except OSError:
        raise SecureKeyNotConfigured("key directory is unavailable") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SecureKeyPermissionError("key directory is not owned by the current user")
    if info.st_mode & 0o022:
        raise SecureKeyPermissionError("key directory is writable by another user")


def _check_posix_file(path: Path, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SecureKeyPermissionError("key is not a regular file")
    if info.st_uid != os.geteuid():
        raise SecureKeyPermissionError("key is not owned by the current user")
    if info.st_mode & 0o077:
        raise SecureKeyPermissionError("key permissions are broader than owner-only")


def _read_posix(path: Path) -> bytes:
    _check_posix_parent(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise SecureKeyNotConfigured("key file is missing") from None
    _check_posix_file(path, before)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise SecureKeyPermissionError("key file cannot be opened safely") from None
    try:
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise SecureKeyPermissionError("key file changed while opening")
        if after.st_size > MAX_KEY_FILE_BYTES:
            raise SecureKeyFormatError("key file is too large")
        chunks = []
        remaining = MAX_KEY_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_windows(path: Path) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SecureKeyNotConfigured("key file is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SecureKeyPermissionError("key is not a regular file")
    if info.st_size > MAX_KEY_FILE_BYTES:
        raise SecureKeyFormatError("key file is too large")
    if not _windows_acl_owner_only(path):
        raise SecureKeyPermissionError("key ACL is not limited to the current user")
    try:
        return path.read_bytes()
    except OSError:
        raise SecureKeyUnreadable("key file cannot be read") from None


def _read_key_bytes(path: Path) -> bytes:
    if os.name == "nt":
        return _read_windows(path)
    return _read_posix(path)


def _minimal_windows_env() -> dict[str, str]:
    names = (
        "SystemRoot",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PSModulePath",
    )
    env = {name: os.environ[name] for name in names if name in os.environ}
    root = env.setdefault("SystemRoot", r"C:\Windows")
    system32 = str(Path(root) / "System32")
    current_path = env.get("PATH", "")
    if system32.casefold() not in current_path.casefold():
        env["PATH"] = current_path + (os.pathsep if current_path else "") + system32
    return env


def _powershell() -> str:
    """Prefer PowerShell 7; fall back to Windows PowerShell on older hosts."""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        Path(program_files) / "PowerShell" / "7" / "pwsh.exe",
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "powershell.exe"


def _windows_acl_snapshot(path: Path) -> tuple[str, str]:
    """Return the current user SID and the file's SDDL through PowerShell."""
    script = (
        "[Console]::Out.Write("
        "([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
        " + \"`n\" + (Get-Acl -LiteralPath $env:OPENTHU_ACL_PATH -ErrorAction Stop).Sddl)"
    )
    env = _minimal_windows_env()
    env["OPENTHU_ACL_PATH"] = str(path)
    try:
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise SecureKeyPermissionError("acl-snapshot-process") from None
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 2:
        raise SecureKeyPermissionError("acl-snapshot-query")
    return lines[0], lines[1]


def _windows_acl_owner_only(path: Path) -> bool:
    try:
        current, sddl = _windows_acl_snapshot(path)
    except SecureKeyPermissionError:
        return False
    owner = re.search(r"O:(S-1-[0-9-]+)", sddl)
    if not owner or owner.group(1) != current:
        return False
    dacl = re.search(r"D:([^GS]*)", sddl)
    if not dacl or "NO_ACCESS_CONTROL" in sddl:
        return False
    allowed = {current, "S-1-5-18"}  # current user and the local SYSTEM account
    aces = re.findall(r"\((?:A|OA);([^)]*)\)", sddl)
    if not aces:
        return False
    for body in aces:
        sid = body.rsplit(";", 1)[-1].strip()
        if not _SID.fullmatch(sid) or sid not in allowed:
            return False
    return True


def _harden_windows_acl(path: Path) -> None:
    try:
        current, _ = _windows_acl_snapshot(path)
    except SecureKeyPermissionError:
        raise
    env = _minimal_windows_env()
    try:
        first = subprocess.run(
            ["icacls", str(path), "/inheritance:r"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        second = subprocess.run(
            ["icacls", str(path), "/grant:r", f"*{current}:F"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise SecureKeyPermissionError("cannot harden key ACL") from None
    if first.returncode or second.returncode:
        raise SecureKeyPermissionError(
            f"acl-harden-inheritance-{first.returncode}-grant-{second.returncode}"
        )


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def create_key_file(
    path: str | os.PathLike[str], backend: str | None = None
) -> Path:
    """Create a new random master key without overwriting an existing one."""
    target = _absolute(path)
    chosen = backend or default_backend()
    if chosen not in {"file-key", "windows-dpapi"}:
        raise SecureBackendUnsupported("unknown key backend")
    if chosen == "windows-dpapi" and os.name != "nt":
        raise SecureBackendUnsupported("Windows DPAPI is unavailable on this host")
    if os.name != "nt":
        _check_posix_parent(target) if target.parent.exists() else None
    if target.exists() or target.is_symlink():
        raise SecureKeyExists("key file already exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise SecureKeyPermissionError("key directory cannot be created") from None
    if os.name != "nt":
        _check_posix_parent(target)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            raise SecureKeyPermissionError("key directory permissions cannot be set") from None

    secret = os.urandom(KEY_BYTES)
    if chosen == "windows-dpapi":
        protected = _dpapi("protect", secret)
        if protected is None:
            raise SecureKeyUnreadable("Windows DPAPI could not protect the key")
        payload = DPAPI_KEY_MAGIC + protected
    else:
        payload = FILE_KEY_MAGIC + secret

    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            raise SecureKeyExists("key file already exists") from None
        created = True
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(target, 0o600)
        else:
            _harden_windows_acl(target)
    except SecureKeyExists:
        raise
    except (OSError, SecureKeyPermissionError):
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        raise SecureKeyPermissionError("key file cannot be created safely") from None
    return target


def load_key_file(
    path: str | os.PathLike[str], backend: str | None = None
) -> KeyMaterial:
    """Load and validate a key file; never create or repair it implicitly."""
    target = _absolute(path)
    raw = _read_key_bytes(target)
    if raw.startswith(FILE_KEY_MAGIC):
        detected = "file-key"
    elif raw.startswith(DPAPI_KEY_MAGIC):
        detected = "windows-dpapi"
    else:
        raise SecureKeyFormatError("key file has no supported header")
    chosen = backend or detected
    if chosen not in {"file-key", "windows-dpapi"}:
        raise SecureBackendUnsupported("unknown key backend")
    if chosen != detected:
        raise SecureKeyFormatError("key backend does not match its file")
    if chosen == "windows-dpapi":
        if os.name != "nt":
            raise SecureBackendUnsupported("Windows DPAPI is unavailable on this host")
        protected = raw[len(DPAPI_KEY_MAGIC) :]
        secret = _dpapi("unprotect", protected)
        if secret is None:
            raise SecureKeyUnreadable("Windows DPAPI could not unprotect the key")
    else:
        secret = raw[len(FILE_KEY_MAGIC) :]
    if len(secret) != KEY_BYTES:
        raise SecureKeyFormatError("key material has the wrong length")
    return KeyMaterial(
        secret=secret,
        key_id=_key_id(secret),
        backend=chosen,
        created_at=_created_at(target),
    )


def _dpapi(mode: str, raw: bytes) -> bytes | None:
    if os.name != "nt":
        return None
    from learn_assistant.auth import _dpapi_protect, _dpapi_unprotect

    try:
        if mode == "protect":
            return _dpapi_protect(raw)
        return _dpapi_unprotect(raw)
    except Exception:
        return None


__all__ = [
    "DPAPI_KEY_MAGIC",
    "FILE_KEY_MAGIC",
    "KeyMaterial",
    "create_key_file",
    "default_backend",
    "load_key_file",
]
