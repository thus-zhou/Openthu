"""First-run dependency diagnostics for an Openthu development checkout.

The report is intended to be safe in a public issue: it contains repository-
relative names, tool versions, status codes and fix commands only.  Absolute
paths, host names, account identifiers and credential values never enter the
report.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MIN_PYTHON = (3, 10)
VALIDATED_PYTHON = (3, 12)
MIN_NODE = 24
DEV_PACKAGES = ("pytest", "requests", "playwright", "build", "openthu")
PINNED_NODE_PACKAGE = "tough-cookie"

FIX_PYTHON = (
    "安装 Python 3.12（最低 3.10），在项目目录运行 python -m venv .venv，"
    "激活后执行 python -m pip install -r requirements-dev.txt 和 python -m pip install -e ."
)
FIX_PYTHON_DEPS = "python -m pip install -r requirements-dev.txt；python -m pip install -e ."
FIX_NODE = "安装 Node.js 24（含 npm）：https://nodejs.org/ ，然后重新打开终端。"
FIX_NODE_DEPS = "python scripts/dev.py setup-node"
FIX_PLAYWRIGHT = "python -m playwright install chromium"
FIX_PLAYWRIGHT_LINUX = "python -m playwright install --with-deps chromium"
FIX_SOURCE = (
    "使用 GitHub Release 的完整源码包或 git clone；"
    "Python wheel 不含 vendor/ 与 runtime/windows/。"
)
FIX_WINDOWS_AUTH = (
    "真实登录请在 Windows 完整源码包中运行；"
    "云端不要用明文凭据替代 Windows DPAPI。"
)


def _version_tuple(text):
    parts = str(text).strip().lstrip("vV").split(".")
    values = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        values.append(int(digits) if digits else 0)
    return tuple(values)


def _command_version(path):
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None


def _distribution_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
            return bool(executable and Path(executable).is_file())
    except Exception:
        return False


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _row(check_id, label, status, detail, fix=None, required=True):
    row = {"id": check_id, "label": label, "status": status, "detail": detail}
    if fix:
        row["fix"] = fix
    if not required:
        row["required"] = False
    return row


def _node_runtime(root):
    package_dir = root / "src" / "thu_assistant"
    lock = _read_json(package_dir / "package-lock.json")
    if lock is None:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "missing",
            "找不到 src/thu_assistant/package-lock.json",
            FIX_SOURCE,
        )
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    entry = packages.get("node_modules/" + PINNED_NODE_PACKAGE)
    pinned = entry.get("version") if isinstance(entry, dict) else None
    if not pinned:
        dependencies = lock.get("dependencies") if isinstance(lock.get("dependencies"), dict) else {}
        entry = dependencies.get(PINNED_NODE_PACKAGE)
        pinned = entry.get("version") if isinstance(entry, dict) else None
    if not pinned:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "missing",
            "锁文件没有固定 " + PINNED_NODE_PACKAGE + " 版本",
            FIX_SOURCE,
        )
    installed = _read_json(
        package_dir / "node_modules" / PINNED_NODE_PACKAGE / "package.json"
    )
    if installed is None:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "missing",
            "运行依赖未安装（锁文件要求 " + PINNED_NODE_PACKAGE + " " + pinned + "）",
            FIX_NODE_DEPS,
        )
    actual = str(installed.get("version") or "未知")
    if actual != pinned:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "outdated",
            "已安装 " + PINNED_NODE_PACKAGE + " " + actual + "，锁文件要求 " + pinned,
            FIX_NODE_DEPS,
        )
    return _row(
        "node-runtime",
        "锁文件运行依赖",
        "ok",
        PINNED_NODE_PACKAGE + " " + pinned + "（与锁文件一致）",
    )


def _source_package(root):
    required = {
        "vendor/madmodel-proxy/package.json": root / "vendor" / "madmodel-proxy" / "package.json",
        "runtime/windows/access.js": root / "runtime" / "windows" / "access.js",
        "scripts/dev.py": root / "scripts" / "dev.py",
        "tests/": root / "tests",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        return _row(
            "source-package",
            "源码包完整性",
            "warning",
            "当前检出缺少完整源码包内容：" + "、".join(missing),
            FIX_SOURCE,
            required=False,
        )
    return _row(
        "source-package",
        "源码包完整性",
        "ok",
        "完整源码检出（含认证依赖、Windows 辅助与离线测试）",
    )


def _windows_auth(root, platform_name, node_present):
    if platform_name != "nt":
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "limited",
            "当前系统仅支持离线开发与测试；真实校园认证依赖 Windows DPAPI",
            FIX_WINDOWS_AUTH,
            required=False,
        )
    helper = root / "runtime" / "windows" / "access.js"
    vendor = root / "vendor" / "madmodel-proxy" / "package.json"
    missing = []
    if not helper.exists():
        missing.append("runtime/windows/access.js")
    if not vendor.exists():
        missing.append("vendor/madmodel-proxy/package.json")
    if not node_present:
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "missing",
            "Node.js 不可用，认证辅助无法启动",
            FIX_NODE,
        )
    if missing:
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "missing",
            "缺少 Windows 认证辅助：" + "、".join(missing),
            FIX_SOURCE,
        )
    return _row(
        "windows-auth",
        "Windows 生产认证",
        "ok",
        "Windows 认证辅助已就位；真实登录使用当前 Windows 用户的 DPAPI",
    )


def collect(
    root=ROOT,
    *,
    platform_name=None,
    which=None,
    distribution_version=None,
    browser_available=None,
    python_version=None,
    python_environment=None,
    command_version=None,
):
    """Collect a public-safe diagnostic report for ``root``.

    All probes are injectable so the missing-dependency and normal-state
    contracts can be tested without touching the host installation.
    """
    root = Path(root)
    platform_name = platform_name or os.name
    which = which or shutil.which
    distribution_version = distribution_version or _distribution_version
    browser_available = browser_available or _browser_available
    command_version = command_version or _command_version
    if python_version is None:
        python_version = sys.version_info[:3]
    elif isinstance(python_version, str):
        python_version = _version_tuple(python_version)
    else:
        python_version = tuple(python_version)[:3]
    python_environment = python_environment or (
        "虚拟环境" if sys.prefix != sys.base_prefix else "系统 Python"
    )

    checks = []

    python_text = ".".join(str(part) for part in python_version[:3])
    if python_version[:2] < MIN_PYTHON:
        checks.append(
            _row(
                "python",
                "Python",
                "outdated",
                "Python " + python_text + "（" + python_environment + "）；最低要求 3.10",
                FIX_PYTHON,
            )
        )
    elif python_version[:2] < VALIDATED_PYTHON:
        checks.append(
            _row(
                "python",
                "Python",
                "warning",
                "Python " + python_text + "（" + python_environment + "）；低于已验证的 3.12",
                "建议使用 Python 3.12；当前版本可以继续尝试。",
            )
        )
    else:
        checks.append(
            _row(
                "python",
                "Python",
                "ok",
                "Python " + python_text + "（" + python_environment + "）",
            )
        )

    package_versions = {name: distribution_version(name) for name in DEV_PACKAGES}
    missing_packages = [name for name in DEV_PACKAGES if not package_versions[name]]
    if missing_packages:
        checks.append(
            _row(
                "python-deps",
                "Python 开发依赖",
                "missing",
                "缺少：" + "、".join(missing_packages),
                FIX_PYTHON_DEPS,
            )
        )
    else:
        checks.append(
            _row(
                "python-deps",
                "Python 开发依赖",
                "ok",
                "pytest、requests、playwright、build、openthu 已安装",
            )
        )

    node_path = which("node")
    if not node_path:
        checks.append(_row("node", "Node.js", "missing", "未找到 node 命令", FIX_NODE))
        node_text = None
    else:
        node_text = command_version(node_path)
        node_version = _version_tuple(node_text) if node_text else ()
        if not node_version or node_version[0] < MIN_NODE:
            checks.append(
                _row(
                    "node",
                    "Node.js",
                    "outdated",
                    "Node " + (node_text or "版本未知") + "；需要 24+",
                    FIX_NODE,
                )
            )
        else:
            checks.append(_row("node", "Node.js", "ok", "Node " + node_text))

    if platform_name == "nt":
        npm_path = which("npm.cmd") or which("npm")
    else:
        npm_path = which("npm")
    if not npm_path:
        checks.append(_row("npm", "npm", "missing", "未找到 npm 命令", FIX_NODE))
    else:
        checks.append(
            _row("npm", "npm", "ok", "npm " + (command_version(npm_path) or "版本未知"))
        )

    checks.append(_node_runtime(root))

    if browser_available():
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "ok",
                "Chromium 已安装（浏览器相关测试可用）",
            )
        )
    else:
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "missing",
                "未找到可执行的 Chromium；浏览器相关测试无法运行",
                FIX_PLAYWRIGHT if platform_name == "nt" else FIX_PLAYWRIGHT_LINUX,
            )
        )

    checks.append(_windows_auth(root, platform_name, bool(node_path)))
    checks.append(_source_package(root))

    blocked = [
        row
        for row in checks
        if row["status"] in ("missing", "outdated") and row.get("required", True)
    ]
    warnings = [row for row in checks if row["status"] in ("warning", "limited")]
    passed = [row for row in checks if row["status"] == "ok"]
    return {
        "ok": not blocked,
        "mode": "offline-development",
        "summary": {
            "checks": len(checks),
            "passed": len(passed),
            "warnings": len(warnings),
            "blocked": len(blocked),
        },
        "checks": checks,
        "note": (
            "报告只含仓库相对名称、版本、状态和修复命令，不含绝对路径、主机名、"
            "账号或凭据；可直接用于公开求助。"
        ),
    }


def format_report(report):
    icons = {
        "ok": "通过",
        "missing": "缺失",
        "outdated": "过旧",
        "warning": "提醒",
        "limited": "限制",
    }
    lines = ["Openthu 开发环境诊断（离线开发模式）", ""]
    for row in report["checks"]:
        lines.append(
            "[" + icons.get(row["status"], row["status"]) + "] "
            + row["label"] + "：" + row["detail"]
        )
        if row.get("fix"):
            lines.append("       修复：" + row["fix"])
    summary = report["summary"]
    lines.append("")
    if report["ok"]:
        lines.append(
            "结论：环境可用（共 " + str(summary["checks"]) + " 项，"
            + str(summary["warnings"]) + " 项提醒/限制）。"
            "可以运行 python scripts/dev.py check 与 test。"
        )
    else:
        lines.append(
            "结论：" + str(summary["blocked"]) + " 项阻止开始，请先执行上面的修复命令。"
        )
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
