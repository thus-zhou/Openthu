"""Temporary Windows ACL diagnostic. No paths or account values are printed."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from thu_assistant.secure_store import keys


def clean(value: str) -> str:
    text = value or ""
    for marker in (str(root), os.environ.get("USERPROFILE", ""), os.environ.get("TEMP", "")):
        if marker:
            text = text.replace(marker, "<local>")
    return text.replace("\r", "\\r")[:300]


def run(label: str, argv, *, timeout: int = 8):
    env = keys._minimal_windows_env()
    env["OPENTHU_ACL_PATH"] = str(target)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        print(label, "TIMEOUT", timeout)
        print(label, "partial_out", clean((exc.stdout or "") if isinstance(exc.stdout, str) else ""))
        print(label, "partial_err", clean((exc.stderr or "") if isinstance(exc.stderr, str) else ""))
        return None
    except Exception as exc:
        print(label, "ERROR", type(exc).__name__, clean(str(exc)))
        return None
    print(label, "rc", result.returncode)
    print(label, "stdout", clean(result.stdout))
    print(label, "stderr", clean(result.stderr))
    return result


root = Path(tempfile.mkdtemp(prefix="openthu-acl-"))
(root / "keys").mkdir()
target = root / "keys" / "master.key"
target.write_bytes(b"x" * 42)
print("env_keys", sorted(keys._minimal_windows_env()))
print("where_powershell", shutil.which("powershell"))
print("where_pwsh", shutil.which("pwsh"))
print("windows_powershell_exists", Path(keys._powershell()).is_file())
print("pwsh7_exists", Path(r"C:\Program Files\PowerShell\7\pwsh.exe").is_file())

run("powershell_hi", [keys._powershell(), "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"], timeout=8)
run("pwsh_hi", [r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"], timeout=8)
run("icacls_help", ["icacls"], timeout=8)

scripts = {
    "getacl": (
        "Import-Module Microsoft.PowerShell.Security -ErrorAction Stop; "
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + (Get-Acl -LiteralPath "
        "$env:OPENTHU_ACL_PATH -ErrorAction Stop).Sddl)"
    ),
    "getitem": (
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + ((Get-Item -LiteralPath "
        "$env:OPENTHU_ACL_PATH).GetAccessControl()).Sddl)"
    ),
}
sid = ""
for label, script in scripts.items():
    result = run(label, [keys._powershell(), "-NoProfile", "-NonInteractive", "-Command", script], timeout=8)
    if result and result.stdout:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            sid = lines[0]

if sid:
    run("inherit", ["icacls", str(target), "/inheritance:r"])
    run("grant", ["icacls", str(target), "/grant:r", f"*{sid}:F"])

for label, call in (
    ("snapshot", lambda: keys._windows_acl_snapshot(target)),
    ("harden", lambda: keys._harden_windows_acl(target)),
    ("owner_only", lambda: keys._windows_acl_owner_only(target)),
):
    try:
        value = call()
        print(label, "ok", str(value)[:120])
        if label == "snapshot":
            print("snapshot_sddl", clean(str(value[1])))
        if label == "harden":
            after = keys._windows_acl_snapshot(target)
            print("after_sddl", clean(str(after[1])))
    except Exception as exc:
        print(label, "error", type(exc).__name__, getattr(exc, "detail", ""))

secret = b"s" * 32
protected = keys._dpapi("protect", secret)
print("dpapi_protect", bool(protected), len(protected or b""))
restored = keys._dpapi("unprotect", protected or b"")
print("dpapi_roundtrip", restored == secret, len(restored or b""))

key = root / "keys" / "dpapi.key"
try:
    keys.create_key_file(key, "windows-dpapi")
    raw = key.read_bytes()
    print("key_size", len(raw))
    print("key_magic", raw[: len(keys.DPAPI_KEY_MAGIC)] == keys.DPAPI_KEY_MAGIC)
    payload = raw[len(keys.DPAPI_KEY_MAGIC) :]
    print("payload_size", len(payload))
    restored = keys._dpapi("unprotect", payload)
    print("payload_unprotect", restored is not None, len(restored or b""))
    material = keys.load_key_file(key, "windows-dpapi")
    print("load_key_ok", material.backend, len(material.secret))
except Exception as exc:
    print("key_error", type(exc).__name__, getattr(exc, "detail", ""))
