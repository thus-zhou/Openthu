"""Materialise the declared distribution resource layout.

One manifest (``build_support/distribution.json``) describes which repository
files are installed where.  The wheel build and the source-archive build both
read this module, so the two layouts cannot drift into two hand-maintained
copies of the gateway and helper code.

The module is deliberately stdlib-only: ``setup.py`` imports it during a build
where project dependencies (and even ``setuptools`` internals) are not the
subject under test.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

MANIFEST_RELATIVE = "build_support/distribution.json"
BUNDLE_DIRNAME = "_bundled"
LAYOUT_NAME = "layout.json"
LAYOUT_SCHEMA_VERSION = "openthu-bundled-layout-v1"


class DistributionError(RuntimeError):
    """A layout or manifest rule that the release build must not ignore."""

    def __init__(self, code: str, item: str):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}")


def load_manifest(root: Path | str = ".") -> dict:
    path = Path(root) / MANIFEST_RELATIVE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DistributionError("DISTRIBUTION_MANIFEST_MISSING", MANIFEST_RELATIVE) from None
    if not isinstance(data, dict) or data.get("schema_version") != "openthu-distribution-manifest-v1":
        raise DistributionError("DISTRIBUTION_MANIFEST_INVALID", MANIFEST_RELATIVE)
    for key in ("bundles", "resource_map", "forbidden", "licenses"):
        if key not in data:
            raise DistributionError("DISTRIBUTION_MANIFEST_INVALID", key)
    return data


def _translate(pattern: str) -> re.Pattern[str]:
    """Translate a small, documented glob subset into a regular expression.

    Supported: ``**/`` (any depth, including none), ``**`` (anything),
    ``*`` (one path segment), ``?``.  Setuptools/globstar rules are not
    reused because the same translator must behave identically on both
    platforms and in every supported Python version.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        rest = pattern[index:]
        if rest.startswith("**/"):
            out.append("(?:.*/)?")
            index += 3
        elif rest.startswith("**"):
            out.append(".*")
            index += 2
        elif rest[0] == "*":
            out.append("[^/]*")
            index += 1
        elif rest[0] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(rest[0]))
            index += 1
    return re.compile("^" + "".join(out) + "$", re.DOTALL)


def _matches_any(relpath: str, patterns) -> bool:
    return any(_translate(pattern).match(relpath) for pattern in patterns or ())


def matches_any(relpath: str, patterns) -> bool:
    """Public wrapper used by the archive build for its exclude rules."""
    return _matches_any(relpath, patterns)


def bundle_files(root: Path | str, bundle: dict) -> list[str]:
    """Return sorted repository-relative POSIX paths for one bundle."""
    root = Path(root)
    source = root / bundle["source"]
    if not source.is_dir():
        raise DistributionError("DISTRIBUTION_BUNDLE_MISSING", bundle["source"])
    include = bundle.get("include") or ["**/*"]
    exclude = bundle.get("exclude") or []
    rows: list[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if _matches_any(rel, exclude) or not _matches_any(rel, include):
            continue
        rows.append(rel)
    for required in bundle.get("must_include") or ():
        if required not in rows:
            raise DistributionError(
                "DISTRIBUTION_BUNDLE_INCOMPLETE",
                f"{bundle['source']}/{required}",
            )
    if not rows:
        raise DistributionError("DISTRIBUTION_BUNDLE_EMPTY", bundle["source"])
    return rows


def forbidden_finding(name: str, manifest: dict) -> str | None:
    """Return the rule name that rejects ``name``, or ``None``."""
    forbidden = manifest["forbidden"]
    posix = PurePosixPath(name)
    parts = set(posix.parts)
    for directory in forbidden.get("directories", ()):
        if directory in parts:
            return f"directory:{directory}"
    base = posix.name.lower()
    if base in {row.lower() for row in forbidden.get("names", ())}:
        return f"name:{base}"
    if (base.startswith(".env.") and base != ".env.example") or base == ".env":
        return "name:.env"
    for suffix in forbidden.get("suffixes", ()):
        if base.endswith(suffix.lower()):
            return f"suffix:{suffix}"
    return None


def scan_forbidden(names, manifest: dict) -> list[dict]:
    rows = []
    for name in names:
        rule = forbidden_finding(name, manifest)
        if rule is not None:
            rows.append({"file": name, "rule": rule})
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize(
    root: Path | str,
    package_dir: Path | str,
    manifest: dict,
    *,
    version: str,
    commit: str | None = None,
) -> dict:
    """Copy the declared bundles into ``package_dir/_bundled`` and describe them.

    Files are copied read-only.  Nothing outside ``package_dir`` is created and
    no state, credential or cache directory is ever consulted.
    """
    root = Path(root)
    target = Path(package_dir) / BUNDLE_DIRNAME
    copied = 0
    total = 0
    hashes: dict[str, str] = {}
    for bundle in manifest["bundles"]:
        for rel in bundle_files(root, bundle):
            source = root / bundle["source"] / rel
            data = source.read_bytes()
            destination = target / bundle["install"] / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.chmod(destination, 0o644)
            hashes[f"{bundle['install']}/{rel}"] = hashlib.sha256(data).hexdigest()
            copied += 1
            total += len(data)
    layout = {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "product_version": version,
        "commit": commit,
        "resources": dict(manifest["resource_map"]),
        "bundles": [
            {"id": bundle["id"], "install": bundle["install"]}
            for bundle in manifest["bundles"]
        ],
        "file_count": copied,
    }
    (target / LAYOUT_NAME).write_text(
        json.dumps(layout, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(target / LAYOUT_NAME, 0o644)
    return {
        "files": copied,
        "bytes": total,
        "hashes": hashes,
        "install_root": BUNDLE_DIRNAME,
        "layout": layout,
    }


def read_layout(package_dir: Path | str) -> dict | None:
    """Read an installed layout descriptor; ``None`` when absent or unusable.

    A malformed descriptor is ignored on purpose: resource resolution then
    reports the individual resource as unavailable instead of raising from an
    unrelated code path.
    """
    path = Path(package_dir) / BUNDLE_DIRNAME / LAYOUT_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("resources"), dict):
        return None
    return data
