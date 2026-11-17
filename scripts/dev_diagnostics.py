"""First-run dependency, configuration and capability diagnostics.

The report is safe to paste into a public issue.  It contains logical check
ids, status classes, sanitized version strings, repository-relative resource
names and fix commands.  Absolute paths, host names, account identifiers,
command output and credential values never enter the report.

Statuses are ``pass``, ``warning``, ``fail`` and ``not_applicable``.  A
``fail`` blocks only when its ``blocking`` flag is true; production-auth and
browser capabilities can therefore be unavailable without pretending that a
valid offline development installation is broken.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from thu_assistant import configuration as configuration_module  # noqa: E402
from thu_assistant import paths as path_resolver  # noqa: E402

MIN_PYTHON = (3, 10)
VALIDATED_PYTHON = (3, 12)
MIN_NODE = 24
DEV_PACKAGES = ("pytest", "requests", "playwright", "build", "openthu")
PINNED_NODE_PACKAGE = "tough-cookie"
MAX_DIAGNOSTIC_OUTPUT = 4096

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
FIX_CONFIG = "检查配置项名称和类型；显式配置无效时不会回退到旧目录或另一账号。"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _safe_version(text):
    """Return ``major.minor.patch`` only; never echo arbitrary command output."""
    if text is None:
        return None
    lines = str(text).splitlines()
    first = lines[0].strip()[:80] if lines else ""
    match = _VERSION_RE.fullmatch(first)
    return ".".join(match.groups()) if match else None


def _version_tuple(text):
    safe = _safe_version(text)
    if safe is None:
        return ()
    return tuple(int(part) for part in safe.split("."))


def _lock_version(value):
    """Accept only a plain ``X.Y.Z`` lock version; never echo a raw value."""
    return _safe_version(value) if isinstance(value, str) else None


def executable_argv(path, args, *, platform_name=None):
    """Build a fixed argv for a resolved executable, including Windows .cmd.

    ``shell=True`` is never used.  A Windows batch wrapper is invoked through
    the system command processor with a quoted, fixed argument list; nothing
    from a diagnostic fix string is executed.
    """
    platform_name = platform_name or os.name
    suffix = Path(str(path)).suffix.lower()
    if platform_name == "nt" and suffix in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        command = subprocess.list2cmdline([str(path), *args])
        return [comspec, "/d", "/s", "/c", command]
    return [str(path), *args]


def _probe_version(path, *, timeout=15, max_output=MAX_DIAGNOSTIC_OUTPUT):
    """Probe one executable and return a structured, sanitized result."""
    try:
        process = subprocess.Popen(
            executable_argv(path, ["--version"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
        )
    except (OSError, ValueError):
        return {"status": "spawn_error", "version": None}
    captured = bytearray()

    def read_output():
        assert process.stdout is not None
        while len(captured) <= max_output:
            try:
                chunk = process.stdout.read(1024)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            captured.extend(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        reader.join(timeout=2)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
    if timed_out:
        return {"status": "timeout", "version": None}
    if process.returncode != 0:
        return {"status": "nonzero", "version": None}
    if len(captured) > max_output:
        return {"status": "oversize", "version": None}
    output = captured.decode("utf-8", errors="replace").strip()
    version = _safe_version(output)
    if version is None:
        return {"status": "format", "version": None}
    return {"status": "pass", "version": version}


def _command_version(path, *, timeout=15, max_output=MAX_DIAGNOSTIC_OUTPUT):
    """Compatibility wrapper returning only a validated version string."""
    result = _probe_version(path, timeout=timeout, max_output=max_output)
    return result["version"] if result["status"] == "pass" else None


def _distribution_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _browser_state():
    """Report the browser capability precisely instead of "installed or not".

    A missing Python client and a missing Chromium binary need different
    fixes, so the diagnostic names the one that is actually missing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"available": False, "reason": "client-missing"}
    try:
        with sync_playwright() as playwright:
            executable = playwright.chromium.executable_path
            if executable and Path(executable).is_file():
                return {"available": True, "reason": "ok"}
    except Exception:
        return {"available": False, "reason": "launch-failed"}
    return {"available": False, "reason": "binary-missing"}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _declared_python(root: Path):
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return MIN_PYTHON
    match = re.search(r'requires-python\s*=\s*["\']\s*>=\s*(\d+)\.(\d+)', text)
    return (int(match.group(1)), int(match.group(2))) if match else MIN_PYTHON


def _configured_node(config, which):
    if config is None:
        return which("node")
    command = getattr(getattr(config, "runtime", None), "node", "node")
    if not command:
        return None
    path = Path(str(command))
    if path.is_absolute() or path.exists():
        return str(path)
    return which(str(command))


def _probe_reason(label, status):
    return {
        "timeout": label + " 版本探测超时",
        "nonzero": label + " --version 退出非零",
        "oversize": label + " --version 输出超过限制",
        "format": label + " 版本格式无法识别",
        "spawn_error": label + " 无法启动",
    }.get(status, label + " 版本探测失败")


def _row(
    check_id,
    label,
    capability,
    status,
    reason,
    *,
    available_version=None,
    fix=None,
    blocking=False,
):
    return {
        "id": check_id,
        "label": label,
        "capability": capability,
        "status": status,
        "available_version": available_version,
        "reason": reason,
        "fix": fix,
        "blocking": bool(blocking),
    }


def _capabilities(checks) -> dict:
    """Collapse per-check status into one state per user-facing capability.

    ``unavailable`` means the capability cannot be used at all; ``degraded``
    means it works with a documented limitation.  The diagnostic therefore
    names the missing capability instead of printing a generic "install
    failed" line.
    """
    order = ("development", "readonly", "browser", "production-auth", "packaging", "config")
    result: dict[str, str] = {}
    for name in order:
        rows = [row for row in checks if row.get("capability") == name]
        if not rows:
            continue
        statuses = {row.get("status") for row in rows}
        if "fail" in statuses:
            result[name] = "unavailable"
        elif "warning" in statuses:
            result[name] = "degraded"
        elif "pass" in statuses:
            result[name] = "available"
        else:
            result[name] = "not_applicable"
    return result


def _browser_report(state) -> dict:
    """Normalise an injected bool or a structured browser probe result."""
    if isinstance(state, dict):
        return {
            "available": bool(state.get("available")),
            "reason": str(state.get("reason") or "unavailable"),
        }
    return {"available": bool(state), "reason": "ok" if state else "unavailable"}


def _release_layout(package_dir: Path):
    """Check the installed program files against the bundled layout descriptor.

    Mixing an old package tree with a newer resource layout (or the reverse)
    is exactly the failure an in-place upgrade can produce, so it is reported
    instead of being discovered later as a mysterious import error.
    """
    layout = path_resolver.bundled_layout(package_root_value=package_dir)
    if layout is None:
        return _row(
            "release-layout",
            "发行布局",
            "packaging",
            "pass",
            "源码检出布局：资源按仓库路径解析，无包内布局描述",
        )
    try:
        from thu_assistant.version import __version__ as running_version
    except Exception:
        running_version = None
    declared = str(layout.get("product_version") or "")
    if not declared or running_version is None:
        return _row(
            "release-layout",
            "发行布局",
            "packaging",
            "fail",
            "包内布局描述缺少版本信息，无法核对程序与资源是否配套",
            fix=FIX_SOURCE,
            blocking=True,
        )
    if declared != running_version:
        return _row(
            "release-layout",
            "发行布局",
            "packaging",
            "fail",
            "程序版本 " + str(running_version) + " 与资源布局 " + declared + " 不一致；请重新安装",
            available_version=declared,
            fix=FIX_SOURCE,
            blocking=True,
        )
    return _row(
        "release-layout",
        "发行布局",
        "packaging",
        "pass",
        "完整安装布局：程序版本与包内认证资源一致（" + declared + "）",
        available_version=declared,
    )


def _node_runtime(package_dir: Path):
    lock = _read_json(package_dir / "package-lock.json")
    if not isinstance(lock, dict):
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "development",
            "fail",
            "找不到 src/thu_assistant/package-lock.json",
            fix=FIX_SOURCE,
            blocking=True,
        )
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    entry = packages.get("node_modules/" + PINNED_NODE_PACKAGE)
    pinned_raw = entry.get("version") if isinstance(entry, dict) else None
    if not pinned_raw:
        dependencies = (
            lock.get("dependencies") if isinstance(lock.get("dependencies"), dict) else {}
        )
        entry = dependencies.get(PINNED_NODE_PACKAGE)
        pinned_raw = entry.get("version") if isinstance(entry, dict) else None
    if not pinned_raw:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "development",
            "fail",
            "锁文件没有固定 " + PINNED_NODE_PACKAGE + " 版本",
            fix=FIX_SOURCE,
            blocking=True,
        )
    pinned = _lock_version(pinned_raw)
    if pinned is None:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "development",
            "fail",
            "锁文件中的 " + PINNED_NODE_PACKAGE + " 版本格式无效",
            fix=FIX_SOURCE,
            blocking=True,
        )
    installed = _read_json(
        package_dir / "node_modules" / PINNED_NODE_PACKAGE / "package.json"
    )
    if not isinstance(installed, dict):
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "development",
            "fail",
            "运行依赖未安装（锁文件要求 " + PINNED_NODE_PACKAGE + " " + pinned + "）",
            available_version=pinned,
            fix=FIX_NODE_DEPS,
            blocking=True,
        )
    actual = _safe_version(installed.get("version")) or "未知"
    if actual != pinned:
        return _row(
            "node-runtime",
            "锁文件运行依赖",
            "development",
            "fail",
            "已安装 " + PINNED_NODE_PACKAGE + " " + actual + "，锁文件要求 " + pinned,
            available_version=actual,
            fix=FIX_NODE_DEPS,
            blocking=True,
        )
    return _row(
        "node-runtime",
        "锁文件运行依赖",
        "development",
        "pass",
        PINNED_NODE_PACKAGE + " " + pinned + "（与锁文件一致）",
        available_version=pinned,
    )


def _readonly_catalog(package_dir: Path):
    services = _read_json(package_dir / "services.json")
    if isinstance(services, list):
        rows = services
    elif isinstance(services, dict) and isinstance(services.get("services"), list):
        rows = services["services"]
    else:
        return _row(
            "readonly-catalog",
            "只读校园服务目录",
            "readonly",
            "fail",
            "services.json 缺失或不是有效 JSON",
            fix=FIX_SOURCE,
            blocking=True,
        )
    return _row(
        "readonly-catalog",
        "只读校园服务目录",
        "readonly",
        "pass",
        "静态服务目录可读（不证明登录、权限或业务成功）",
        available_version=str(len(rows)),
    )


def _source_package(package_dir: Path, source):
    manifest = path_resolver.resource_manifest(
        package_root_value=package_dir, source_root_value=source
    )
    package_missing = [
        row["relative_path"]
        for row in manifest
        if row["required"] and not row["available"] and row["scope"] == "package"
    ]
    bundled = [
        row["id"]
        for row in manifest
        if row["scope"] == "source" and row.get("location") == "bundled"
    ]
    source_missing = [
        row["relative_path"]
        for row in manifest
        if row["required"] and not row["available"] and row["scope"] == "source"
    ]
    if package_missing:
        return _row(
            "source-package",
            "源码包完整性",
            "packaging",
            "fail",
            "包资源缺失：" + "、".join(str(item) for item in package_missing),
            fix=FIX_SOURCE,
            blocking=True,
        )
    if source is None:
        if not source_missing and bundled:
            return _row(
                "source-package",
                "源码包完整性",
                "packaging",
                "pass",
                "完整安装布局：认证辅助与固定上游代码随包安装，不依赖源码目录"
                "（离线测试仍只在源码包中）",
                available_version=str(len(bundled)) + " 项",
            )
        return _row(
            "source-package",
            "源码包完整性",
            "packaging",
            "warning",
            "当前是包安装布局；缺少认证辅助与离线测试，不代表安装损坏",
            fix=FIX_SOURCE,
        )
    if source_missing:
        return _row(
            "source-package",
            "源码包完整性",
            "packaging",
            "warning",
            "当前源码检出缺少：" + "、".join(str(item) for item in source_missing),
            fix=FIX_SOURCE,
        )
    return _row(
        "source-package",
        "源码包完整性",
        "packaging",
        "pass",
        "完整源码检出（含认证依赖、Windows 辅助与离线测试）",
    )


def _windows_auth(package_dir: Path, source, node_present: bool, platform_name: str):
    if platform_name != "nt":
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "production-auth",
            "not_applicable",
            "当前系统仅支持离线开发与测试；真实校园认证依赖 Windows DPAPI",
            fix=FIX_WINDOWS_AUTH,
        )
    helper = path_resolver.resolve_resource(
        "source.windows_auth",
        package_root_value=package_dir,
        source_root_value=source,
    )
    vendor = path_resolver.resolve_resource(
        "source.madmodel_proxy",
        package_root_value=package_dir,
        source_root_value=source,
    )
    if not node_present:
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "production-auth",
            "fail",
            "Node.js 不可用，认证辅助无法启动",
            fix=FIX_NODE,
            blocking=True,
        )
    missing = [row.relative_path for row in (helper, vendor) if not row.available]
    if missing:
        return _row(
            "windows-auth",
            "Windows 生产认证",
            "production-auth",
            "fail",
            "缺少 Windows 认证辅助：" + "、".join(missing),
            fix=FIX_SOURCE,
            blocking=True,
        )
    return _row(
        "windows-auth",
        "Windows 生产认证",
        "production-auth",
        "pass",
        "Windows 认证辅助已就位；真实登录使用当前 Windows 用户的 DPAPI",
    )


def _config_row(config, config_error):
    if config_error is not None:
        return _row(
            "configuration",
            "统一配置",
            "config",
            "fail",
            "配置无效：" + config_error.item + "（" + config_error.code + "）",
            fix=FIX_CONFIG,
            blocking=True,
        )
    return _row(
        "configuration",
        "统一配置",
        "config",
        "pass",
        "配置有效（来源：" + config.public_dict()["config_file"] + "）",
    )


def _storage_row(config, config_error):
    if config_error is not None:
        return _row(
            "storage-paths",
            "私有目录边界",
            "config",
            "not_applicable",
            "配置无效，未使用其中的目录",
        )
    return _row(
        "storage-paths",
        "私有目录边界",
        "config",
        "pass",
        "状态、下载、临时与日志目录已通过范围和目录类型检查",
    )


def collect(
    root=None,
    *,
    platform_name=None,
    which=None,
    distribution_version=None,
    browser_available=None,
    python_version=None,
    python_environment=None,
    command_version=None,
    command_probe=None,
    config=None,
    config_error=None,
    config_file=None,
    env=None,
):
    """Collect a public-safe diagnostic report.

    Probes and the configuration object are injectable so synthetic tests can
    exercise missing packages, private command output and Windows layouts
    without touching the host.
    """
    platform_name = platform_name or os.name
    if root is None:
        root = path_resolver.source_root() or path_resolver.package_root()
    root = Path(root)
    which = which or shutil.which
    distribution_version = distribution_version or _distribution_version
    browser_available = browser_available or _browser_state
    if config is None and config_error is None:
        try:
            config = configuration_module.load_config(
                config_file=config_file, env=env, platform_name=platform_name
            )
        except configuration_module.ConfigurationError as error:
            config_error = error

    if command_probe is None:
        if command_version is not None:
            def command_probe(path):
                value = command_version(path)
                version = _safe_version(value)
                return {
                    "status": "pass" if version else "format",
                    "version": version,
                }
        else:
            probe_timeout = 15
            probe_limit = MAX_DIAGNOSTIC_OUTPUT
            if config is not None:
                runtime = getattr(config, "runtime", None)
                if runtime is not None:
                    probe_timeout = min(
                        max(1, int(getattr(runtime, "command_timeout_seconds", 15))),
                        15,
                    )
                    probe_limit = max(
                        1024,
                        min(
                            int(getattr(runtime, "max_output_bytes", MAX_DIAGNOSTIC_OUTPUT)),
                            1024 * 1024,
                        ),
                    )

            def command_probe(path):
                return _probe_version(
                    path, timeout=probe_timeout, max_output=probe_limit
                )

    package_dir = (
        Path(config.resources.root)
        if config is not None
        else root / "src" / "thu_assistant"
        if (root / "src" / "thu_assistant").is_dir()
        else root
    )
    source = (
        root
        if (root / "src" / "thu_assistant").is_dir()
        or (root / "runtime").is_dir()
        or (root / "vendor").is_dir()
        else None
    )

    checks = []

    if python_version is None:
        python_version = sys.version_info[:3]
    elif isinstance(python_version, str):
        parsed = _version_tuple(python_version)
        python_version = parsed if parsed else (0, 0, 0)
    else:
        python_version = tuple(python_version)[:3]
    python_environment = python_environment or (
        "虚拟环境" if sys.prefix != sys.base_prefix else "系统 Python"
    )
    python_text = ".".join(str(part) for part in python_version[:3])
    minimum = _declared_python(root)
    if python_version[:2] < minimum:
        checks.append(
            _row(
                "python",
                "Python",
                "development",
                "fail",
                "Python " + python_text + "；项目要求 " + ".".join(map(str, minimum)) + "+",
                available_version=python_text,
                fix=FIX_PYTHON,
                blocking=True,
            )
        )
    elif python_version[:2] < VALIDATED_PYTHON:
        checks.append(
            _row(
                "python",
                "Python",
                "development",
                "warning",
                "Python " + python_text + "；项目要求 " + ".".join(map(str, minimum)) + "+，已验证 3.12",
                available_version=python_text,
            )
        )
    elif python_environment != "虚拟环境":
        checks.append(
            _row(
                "python",
                "Python",
                "development",
                "warning",
                "Python " + python_text + "（系统 Python）；可用，但建议使用虚拟环境隔离",
                available_version=python_text,
            )
        )
    else:
        checks.append(
            _row(
                "python",
                "Python",
                "development",
                "pass",
                "Python " + python_text + "（虚拟环境；项目要求 " + ".".join(map(str, minimum)) + "+）",
                available_version=python_text,
            )
        )

    package_versions = {name: distribution_version(name) for name in DEV_PACKAGES}
    missing_packages = [name for name in DEV_PACKAGES if not package_versions[name]]
    if missing_packages:
        checks.append(
            _row(
                "python-deps",
                "Python 开发依赖",
                "development",
                "fail",
                "缺少：" + "、".join(missing_packages),
                fix=FIX_PYTHON_DEPS,
                blocking=True,
            )
        )
    else:
        checks.append(
            _row(
                "python-deps",
                "Python 开发依赖",
                "development",
                "pass",
                "pytest、requests、playwright、build、openthu 已安装",
                available_version=str(len(DEV_PACKAGES)) + "/" + str(len(DEV_PACKAGES)),
            )
        )

    node_path = _configured_node(config, which)
    if not node_path:
        node_text = None
        node_ok = False
        checks.append(
            _row(
                "node",
                "Node.js",
                "development",
                "fail",
                "未找到 node 命令：校园网关、浏览器办理与真实认证无法启动"
                "（静态目录和离线库调用仍可用）",
                fix=FIX_NODE,
                blocking=True,
            )
        )
    else:
        node_probe = command_probe(node_path)
        node_text = node_probe.get("version")
        node_ok = node_probe.get("status") == "pass"
        if not node_ok:
            checks.append(
                _row(
                    "node",
                    "Node.js",
                    "development",
                    "fail",
                    _probe_reason("Node", node_probe.get("status")),
                    available_version=node_text,
                    fix=FIX_NODE,
                    blocking=True,
                )
            )
        else:
            node_version = _version_tuple(node_text) if node_text else ()
            if not node_version or node_version[0] < MIN_NODE:
                checks.append(
                    _row(
                        "node",
                        "Node.js",
                        "development",
                        "fail",
                        "Node " + (node_text or "版本未知") + "；需要 24+",
                        available_version=node_text,
                        fix=FIX_NODE,
                        blocking=True,
                    )
                )
                node_ok = False
            else:
                checks.append(
                    _row(
                        "node",
                        "Node.js",
                        "development",
                        "pass",
                        "Node " + node_text,
                        available_version=node_text,
                    )
                )

    if platform_name == "nt":
        npm_path = which("npm.cmd") or which("npm")
    else:
        npm_path = which("npm")
    if not npm_path:
        checks.append(
            _row(
                "npm",
                "npm",
                "development",
                "fail",
                "未找到 npm 命令",
                fix=FIX_NODE,
                blocking=True,
            )
        )
    else:
        npm_probe = command_probe(npm_path)
        npm_text = npm_probe.get("version")
        if npm_probe.get("status") == "pass":
            checks.append(
                _row(
                    "npm",
                    "npm",
                    "development",
                    "pass",
                    "npm " + npm_text,
                    available_version=npm_text,
                )
            )
        else:
            checks.append(
                _row(
                    "npm",
                    "npm",
                    "development",
                    "warning",
                    _probe_reason("npm", npm_probe.get("status")),
                    fix=FIX_NODE,
                )
            )

    checks.append(_node_runtime(package_dir))
    checks.append(_readonly_catalog(package_dir))

    browser_state = _browser_report(browser_available())
    if browser_state["available"]:
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "browser",
                "pass",
                "Chromium 已安装（浏览器相关测试可用）",
            )
        )
    elif browser_state["reason"] == "client-missing":
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "browser",
                "warning",
                "浏览器能力不可用：未安装 Playwright 客户端；静态目录、离线库调用与命令行仍可用",
                fix=FIX_PLAYWRIGHT if platform_name == "nt" else FIX_PLAYWRIGHT_LINUX,
            )
        )
    elif browser_state["reason"] == "binary-missing":
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "browser",
                "warning",
                "浏览器能力不可用：Playwright 客户端已安装但缺少 Chromium 可执行文件；"
                "校园页面操作不可用，离线开发仍可继续",
                fix=FIX_PLAYWRIGHT if platform_name == "nt" else FIX_PLAYWRIGHT_LINUX,
            )
        )
    else:
        checks.append(
            _row(
                "playwright-browser",
                "Playwright Chromium",
                "browser",
                "warning",
                "浏览器能力不可用：Chromium 启动失败（浏览器相关测试不可用，离线开发仍可继续）",
                fix=FIX_PLAYWRIGHT if platform_name == "nt" else FIX_PLAYWRIGHT_LINUX,
            )
        )

    checks.append(_windows_auth(package_dir, source, node_ok, platform_name))
    checks.append(_source_package(package_dir, source))
    checks.append(_release_layout(package_dir))
    checks.append(_config_row(config, config_error))
    checks.append(_storage_row(config, config_error))

    blocking_failures = [
        row for row in checks if row["status"] == "fail" and row["blocking"]
    ]
    warnings = [row for row in checks if row["status"] == "warning"]
    failed = [row for row in checks if row["status"] == "fail"]
    not_applicable = [row for row in checks if row["status"] == "not_applicable"]
    passed = [row for row in checks if row["status"] == "pass"]
    return {
        "ok": not blocking_failures,
        "mode": "offline-development",
        "summary": {
            "checks": len(checks),
            "passed": len(passed),
            "warnings": len(warnings),
            "failed": len(failed),
            "not_applicable": len(not_applicable),
            "blocking": len(blocking_failures),
        },
        "capabilities": _capabilities(checks),
        "checks": checks,
        "note": (
            "报告只含逻辑检查项、状态、脱敏版本、仓库相对资源名和修复命令，"
            "不含绝对路径、主机名、账号、凭据或命令原始输出；可直接用于公开求助。"
        ),
    }


def format_report(report):
    icons = {
        "pass": "通过",
        "warning": "警告",
        "fail": "失败",
        "not_applicable": "不适用",
    }
    lines = ["Openthu 开发环境诊断（离线开发模式）", ""]
    for row in report["checks"]:
        version = (
            "（" + row["available_version"] + "）"
            if row.get("available_version")
            else ""
        )
        lines.append(
            "["
            + icons.get(row["status"], row["status"])
            + "] "
            + row["label"]
            + " ["
            + row["capability"]
            + "]："
            + row["reason"]
            + version
        )
        if row.get("fix"):
            lines.append("       修复：" + row["fix"])
    summary = report["summary"]
    lines.append("")
    capabilities = report.get("capabilities") or {}
    if capabilities:
        labels = {
            "available": "可用",
            "degraded": "降级",
            "unavailable": "不可用",
            "not_applicable": "不适用",
        }
        lines.append(
            "能力状态："
            + "；".join(
                name + "=" + labels.get(state, state) for name, state in capabilities.items()
            )
        )
    if report["ok"]:
        lines.append(
            "结论：环境可用（共 "
            + str(summary["checks"])
            + " 项，"
            + str(summary["warnings"])
            + " 项警告，"
            + str(summary["not_applicable"])
            + " 项不适用）。可以运行 python scripts/dev.py check 与 test。"
        )
    else:
        lines.append(
            "结论："
            + str(summary["blocking"])
            + " 项阻止开始，请先执行上面的修复命令。"
        )
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--config", metavar="PATH", help="显式配置文件")
    args = parser.parse_args(argv)
    report = collect(config_file=args.config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
