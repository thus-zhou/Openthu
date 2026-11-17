"""Installed-layout diagnostic that runs from a wheel.

``scripts/dev.py doctor`` checks the development environment (pytest, build,
Node, Chromium and the source tree).  A wheel has none of those, so this module
checks what an installation can actually promise: the version, the bundled
resource layout, the required package and authentication resources, and a
configuration that resolves.  It never logs in, reads a credential or writes a
state file.

Usage::

    openthu-doctor
    python -m thu_assistant.doctor --json
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from . import paths
from . import version as version_module
from . import configuration

_CHECK_IDS = (
    "version",
    "release-layout",
    "package-resources",
    "runtime-resources",
    "configuration",
)


def _row(check_id: str, status: str, reason: str, *, blocking: bool = False,
         capability: str = "packaging") -> dict:
    return {
        "id": check_id,
        "capability": capability,
        "status": status,
        "reason": reason,
        "blocking": bool(blocking),
    }


def _distribution_version() -> str | None:
    try:
        return importlib.metadata.version("openthu")
    except Exception:
        return None


def _version_row(installed: str | None) -> dict:
    running = version_module.__version__
    if installed is None:
        return _row(
            "version",
            "pass",
            "从源码运行（未读取发行元数据）；程序版本 " + running,
        )
    if installed != running:
        return _row(
            "version",
            "fail",
            f"安装元数据版本 {installed} 与程序版本 {running} 不一致；请重新安装",
            blocking=True,
        )
    return _row("version", "pass", "程序版本与安装元数据一致（" + running + "）")


def _layout_row(package_dir: Path, source_root: Path | None) -> dict:
    layout = paths.bundled_layout(package_root_value=package_dir)
    if layout is None:
        if source_root is not None:
            return _row(
                "release-layout",
                "pass",
                "源码检出布局：资源按仓库路径解析，无包内布局描述",
            )
        return _row(
            "release-layout",
            "fail",
            "安装包缺少包内资源布局（thu_assistant/_bundled/layout.json）；"
            "该安装不完整，请重新安装完整 wheel",
            blocking=True,
        )
    declared = str(layout.get("product_version") or "")
    if declared != version_module.__version__:
        return _row(
            "release-layout",
            "fail",
            f"程序版本 {version_module.__version__} 与资源布局 {declared or '未知'} 不一致；请重新安装",
            blocking=True,
        )
    return _row(
        "release-layout",
        "pass",
        "完整安装布局：程序版本与包内资源一致（" + declared + "）",
    )


def _package_rows(package_dir: Path, source_root: Path | None) -> list[dict]:
    rows = paths.resource_manifest(
        package_root_value=package_dir, source_root_value=source_root
    )
    missing_package = [
        row["relative_path"]
        for row in rows
        if row["scope"] == "package" and row["required"] and not row["available"]
    ]
    missing_runtime = [
        row["id"]
        for row in rows
        if row["scope"] == "source" and row["required"] and not row["available"]
    ]
    bundled = [row for row in rows if row["location"] == "bundled"]
    result = [
        _row(
            "package-resources",
            "fail" if missing_package else "pass",
            "包资源缺失：" + "、".join(missing_package)
            if missing_package
            else "包内静态资源完整（gateway、服务目录、安全与 Cookie 脚本）",
            blocking=bool(missing_package),
            capability="readonly",
        )
    ]
    if missing_runtime:
        result.append(
            _row(
                "runtime-resources",
                "fail",
                "认证运行资源缺失：" + "、".join(missing_runtime)
                + "；真实认证不可用，请使用完整安装包",
                blocking=source_root is None,
                capability="production-auth",
            )
        )
    elif bundled:
        result.append(
            _row(
                "runtime-resources",
                "pass",
                "认证资源随包安装（" + str(len(bundled)) + " 项逻辑资源），不依赖源码目录",
                capability="production-auth",
            )
        )
    else:
        result.append(
            _row(
                "runtime-resources",
                "pass",
                "认证资源来自源码检出（runtime/windows、vendor/madmodel-proxy）",
                capability="production-auth",
            )
        )
    return result


def _configuration_row(config_file: str | None, env: dict[str, str] | None) -> dict:
    try:
        config = configuration.load_config(config_file=config_file, env=env)
    except configuration.ConfigurationError as error:
        return _row(
            "configuration",
            "fail",
            f"配置无效：{error.item}（{error.code}）",
            blocking=True,
            capability="config",
        )
    return _row(
        "configuration",
        "pass",
        "配置有效（来源：" + config.public_dict()["config_file"] + "）",
        capability="config",
    )


def collect(
    *,
    package_dir: Path | str | None = None,
    source_root=...,
    config_file: str | None = None,
    env: dict[str, str] | None = None,
    distribution_version=...,
) -> dict:
    """Collect a public-safe report for an installed or source layout.

    ``source_root=None`` explicitly means "no checkout"; omitting it asks the
    package layout to detect one.
    """
    package = Path(package_dir) if package_dir is not None else paths.package_root()
    if source_root is ...:
        detected = paths.source_root()
    else:
        detected = Path(source_root) if source_root else None
    installed = (
        _distribution_version() if distribution_version is ... else distribution_version
    )
    checks = [
        _version_row(installed),
        _layout_row(package, detected),
        *_package_rows(package, detected),
        _configuration_row(config_file, env),
    ]
    blocking = [row for row in checks if row["status"] == "fail" and row["blocking"]]
    capabilities: dict[str, str] = {}
    for row in checks:
        state = {"fail": "unavailable", "warning": "degraded", "pass": "available"}.get(
            row["status"], row["status"]
        )
        current = capabilities.get(row["capability"])
        if current == "unavailable" or (current == "degraded" and state == "available"):
            continue
        capabilities[row["capability"]] = state
    return {
        "ok": not blocking,
        "mode": "installed" if detected is None else "source",
        "product": "openthu",
        "version": version_module.__version__,
        "checks": checks,
        "capabilities": capabilities,
        "note": (
            "只检查安装布局、资源与配置；不登录、不读取凭据、不写入状态。"
            "开发环境（pytest、Node、Chromium）另见源码包中的 python scripts/dev.py doctor。"
        ),
    }


def format_report(report: dict) -> str:
    icons = {"pass": "通过", "warning": "警告", "fail": "失败"}
    lines = ["Openthu 安装诊断", ""]
    for row in report["checks"]:
        lines.append(f"[{icons.get(row['status'], row['status'])}] {row['id']}：{row['reason']}")
    lines.append("")
    lines.append("总体：" + ("可用" if report["ok"] else "需要处理"))
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--config", metavar="PATH", help="显式配置文件")
    args = parser.parse_args(argv)
    report = collect(config_file=args.config)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
