"""Temporary Windows ACL diagnostic. No paths or account values are printed."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from thu_assistant.secure_store import keys


def clean(value: str) -> str:
    text = value or ""
    for marker in (str(root), os.environ.get("USERPROFILE", ""), os.environ.get("TEMP", "")):
        if marker:
            text = text.replace(marker, "<local>")
    return text.replace("\r", "\\r")[:400]


root = Path(tempfile.mkdtemp(prefix="openthu-acl-"))
(root / "keys").mkdir()
target = root / "keys" / "master.key"
target.write_bytes(b"x" * 42)
print("env_keys", sorted(keys._minimal_windows_env()))
print("powershell_exists", Path(keys._powershell()).is_file())

scripts = {
    "getacl": (
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + (Get-Acl -LiteralPath "
        "$env:OPENTHU_ACL_PATH).Sddl)"
    ),
    "getitem": (
        "[Console]::Out.Write(([System.Security.Principal.WindowsIdentity]::"
        "GetCurrent().User.Value) + \"`n\" + ((Get-Item -LiteralPath "
        "$env:OPENTHU_ACL_PATH).GetAccessControl()).Sddl)"
    ),
}
sid = ""
for label, script in scripts.items():
    env = keys._minimal_windows_env()
    env["OPENTHU_ACL_PATH"] = str(target)
    result = subprocess.run(
        [keys._powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=env,
    )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if lines and not sid:
        sid = lines[0]
    print(label, "rc", result.returncode, "lines", len(lines))
    print(label, "stdout", clean(result.stdout))
    print(label, "stderr", clean(result.stderr))

for label, args in (
    ("inherit", ["icacls", str(target), "/inheritance:r"]),
    ("grant", ["icacls", str(target), "/grant:r", f"*{sid}:F"]),
):
    env = keys._minimal_windows_env()
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=env,
    )
    print(label, "rc", result.returncode)
    print(label, "stdout", clean(result.stdout))
    print(label, "stderr", clean(result.stderr))

for label, call in (
    ("snapshot", lambda: keys._windows_acl_snapshot(target)),
    ("harden", lambda: keys._harden_windows_acl(target)),
):
    try:
        value = call()
        print(label, "ok", str(value)[:120])
    except Exception as exc:
        print(label, "error", type(exc).__name__, getattr(exc, "detail", ""))
