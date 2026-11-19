"""Materialise the declared distribution resource layout.

One manifest (``build_support/distribution.json``) describes which repository
files are installed where.  The wheel build and the source-archive build both
read this module, so the two layouts cannot drift into two hand-maintained
copies of the gateway and helper code.

The module is deliberately stdlib-only: ``setup.py`` imports it during a build
where project dependencies (and even ``setuptools`` internals) are not the
subject under test.

Every path from the manifest is treated as untrusted input: a ``source`` or
``install`` entry that is absolute, contains ``..`` or resolves outside its
documented root fails the build instead of writing outside the bundle.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil

MANIFEST_RELATIVE = "build_support/distribution.json"
BUNDLE_DIRNAME = "_bundled"
LAYOUT_NAME = "layout.json"
LAYOUT_SCHEMA_VERSION = "openthu-bundled-layout-v1"

#: Files are shipped read-only so a running process cannot rewrite program
#: resources.  Windows only exposes the read-only attribute through chmod and
#: its build tools must be able to replace the tree, so the strict mode is
#: applied on POSIX and the platform default is kept on Windows.
RESOURCE_FILE_MODE = 0o644 if os.name == "nt" else 0o444


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


def _safe_relative(value, item: str) -> str:
    """Return a normalized POSIX relative path or fail the build."""
    text = str(value or "").replace("\\", "/")
    if not text or text.startswith("/") or any(
        part in {"..", ""} for part in text.split("/")
    ):
        raise DistributionError("DISTRIBUTION_PATH_INVALID", item)
    pure = PurePosixPath(text)
    if pure.is_absolute() or not pure.parts:
        raise DistributionError("DISTRIBUTION_PATH_INVALID", item)
    return pure.as_posix()


def _bundle_root(root: Path, bundle: dict) -> Path:
    relative = _safe_relative(bundle.get("source"), f"{bundle.get('id', '?')}.source")
    source = (root / relative).resolve()
    try:
        source.relative_to(root.resolve())
    except ValueError:
        raise DistributionError("DISTRIBUTION_PATH_OUTSIDE", str(bundle.get("source"))) from None
    if not source.is_dir():
        raise DistributionError("DISTRIBUTION_BUNDLE_MISSING", relative)
    return source


def bundle_files(root: Path | str, bundle: dict) -> list[str]:
    """Return sorted repository-relative POSIX paths for one bundle."""
    root = Path(root)
    source = _bundle_root(root, bundle)
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


def _force_remove(path: Path) -> None:
    """Remove a previously materialised bundle, including read-only files."""
    if not path.exists():
        return

    def _retry(function, target, _error):
        os.chmod(target, 0o700)
        function(target)

    shutil.rmtree(path, onexc=_retry)


def materialize(
    root: Path | str,
    package_dir: Path | str,
    manifest: dict,
    *,
    version: str,
    commit: str | None = None,
) -> dict:
    """Copy the declared bundles into ``package_dir/_bundled`` and describe them.

    The target directory is rebuilt from scratch: a file deleted from the
    repository must not survive as a stale resource in an incremental build.
    Files are read-only, nothing outside ``package_dir/_bundled`` is created and
    no state, credential or cache directory is ever consulted.
    """
    root = Path(root)
    package = Path(package_dir)
    target = package / BUNDLE_DIRNAME
    _force_remove(target)
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    total = 0
    hashes: dict[str, str] = {}
    installs: dict[str, str] = {}
    for bundle in manifest["bundles"]:
        install = _safe_relative(bundle.get("install"), f"{bundle.get('id', '?')}.install")
        bundle_target = (target / install).resolve()
        try:
            bundle_target.relative_to(target.resolve())
        except ValueError:
            raise DistributionError("DISTRIBUTION_PATH_OUTSIDE", str(bundle.get("install"))) from None
        installs[bundle["id"]] = install
        source_root = _bundle_root(root, bundle)
        for rel in bundle_files(root, bundle):
            data = (source_root / rel).read_bytes()
            destination = target / install / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            os.chmod(destination, RESOURCE_FILE_MODE)
            hashes[f"{install}/{rel}"] = hashlib.sha256(data).hexdigest()
            copied += 1
            total += len(data)
    layout = {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "product_version": version,
        "commit": commit,
        "resources": dict(manifest["resource_map"]),
        "bundles": [
            {"id": bundle["id"], "install": installs[bundle["id"]]}
            for bundle in manifest["bundles"]
        ],
        "file_count": copied,
    }
    layout_path = target / LAYOUT_NAME
    layout_path.write_text(
        json.dumps(layout, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(layout_path, RESOURCE_FILE_MODE)
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
