"""Build and verify the complete release layout from committed Git content.

The layout has two shapes of the same product:

``Openthu-<version>-source.{zip,tar.gz}``
    A deterministic archive of the committed tree.  Bytes are read from Git
    objects, so a working tree with different line endings, file modes or
    modification times produces the same archive.

``openthu-<version>-py3-none-any.whl`` / ``openthu-<version>.tar.gz``
    The Python build.  ``setup.py`` materialises the declared runtime
    resources (Windows authentication helper, pinned upstream proxy, licenses)
    from ``build_support/distribution.json`` so the installed wheel is the
    complete layout and no longer a reduced one.

Commands::

    python scripts/package_release.py build        # candidate artifacts
    python scripts/package_release.py verify       # re-check ``dist/``
    python scripts/package_release.py install-check
    python scripts/package_release.py manifest

Nothing here publishes a release, creates a tag, merges a branch or contacts
the network.  Artifacts are written to ``dist/`` and described by
``release-manifest.json`` plus ``SHA256SUMS``.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
_SUPPORT = ROOT / "build_support"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))

import materialize  # noqa: E402  (shared build helper)

ZIP_DATE = (2026, 1, 1, 0, 0, 0)
RELEASE_MANIFEST = "release-manifest.json"


class ReleaseError(RuntimeError):
    """A release rule the build must not skip."""

    def __init__(self, code: str, item: str = ""):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}" if item else code)


# --------------------------------------------------------------------------
# Git-sourced content
# --------------------------------------------------------------------------

def _git(*args: str, binary: bool = False):
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()[:200]
        raise ReleaseError("GIT_COMMAND_FAILED", f"{args[0]}: {message}")
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def git_commit() -> str:
    return _git("rev-parse", "HEAD").strip()


def git_commit_time() -> int:
    try:
        return int(_git("show", "-s", "--format=%ct", "HEAD").strip())
    except (ReleaseError, ValueError):
        return 0


def require_clean_tracked_tree() -> None:
    dirty = _git("status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        raise ReleaseError("RELEASE_TREE_DIRTY", dirty.splitlines()[0][:120])


def committed_entries() -> list[dict]:
    """Return ``[{mode, sha, path}]`` for every committed file, sorted by path."""
    raw = _git("ls-files", "-s", "-z", binary=True).decode("utf-8", "replace")
    rows = []
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) != 3 or not path:
            continue
        mode, sha, _stage = fields
        rows.append({"mode": mode, "sha": sha, "path": path})
    rows.sort(key=lambda row: row["path"])
    if not rows:
        raise ReleaseError("RELEASE_SOURCE_EMPTY", "commit the release source first")
    return rows


def read_blobs(shas: list[str]) -> dict[str, bytes]:
    """Read committed blobs by object id, never from the working tree."""
    unique = list(dict.fromkeys(shas))
    process = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input="".join(sha + "\n" for sha in unique).encode("ascii"),
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise ReleaseError("GIT_BLOBS_UNAVAILABLE", "git cat-file --batch")
    output = process.stdout
    blobs: dict[str, bytes] = {}
    offset = 0
    for sha in unique:
        newline = output.index(b"\n", offset)
        header = output[offset:newline].split()
        if len(header) != 3 or header[1] != b"blob":
            raise ReleaseError("GIT_BLOB_MISSING", sha)
        size = int(header[2])
        start = newline + 1
        blobs[sha] = output[start:start + size]
        offset = start + size + 1
    return blobs


def committed_sources(manifest: dict | None = None) -> dict[str, bytes]:
    """Committed file bytes for the whole tree, honouring archive excludes."""
    entries = committed_entries()
    names = {row["sha"] for row in entries}
    blobs: dict[str, bytes] = {}
    for index in range(0, len(names), 512):
        blobs.update(read_blobs(sorted(names)[index:index + 512]))
    excluded = (manifest or {}).get("artifact_excludes", [])
    files: dict[str, bytes] = {}
    for row in entries:
        if excluded and materialize.matches_any(row["path"], excluded):
            continue
        files[row["path"]] = blobs[row["sha"]]
    return files


# --------------------------------------------------------------------------
# Deterministic archives
# --------------------------------------------------------------------------

def write_source_zip(path: Path, prefix: str, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(prefix + "/" + name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])


def write_source_tar(path: Path, prefix: str, files: dict[str, bytes]) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
                for name in sorted(files):
                    data = files[name]
                    info = tarfile.TarInfo(prefix + "/" + name)
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(data))


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def declared_version() -> str:
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = spec.get("project", {})
    if "version" in project:
        return project["version"]
    dynamic = project.get("dynamic", [])
    if "version" not in dynamic:
        raise ReleaseError("RELEASE_VERSION_MISSING", "project.version")
    attr = spec["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    module_name, _, attribute = attr.rpartition(".")
    if not module_name:
        raise ReleaseError("RELEASE_VERSION_MISSING", "project.dynamic.version")
    module_path = ROOT / "src" / Path(*module_name.split(".")).with_suffix(".py")
    namespace: dict[str, object] = {}
    exec(compile(module_path.read_text(encoding="utf-8"), str(module_path), "exec"), namespace)  # noqa: S102 - build metadata only
    return str(namespace[attribute or "__version__"])


def stage_tree(files: dict[str, bytes], destination: Path) -> None:
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o644)


def run_build(staging: Path, *, commit: str, commit_time: int) -> None:
    env = dict(os.environ)
    env.update(
        {
            "SOURCE_DATE_EPOCH": str(commit_time or 0),
            "OPENTHU_BUILD_COMMIT": commit,
            "PYTHONPATH": "",
            "PYTHONUTF8": "1",
            # Keep user-level pip/setuptools configuration out of a release
            # build so two accounts produce the same tree.
            "HOME": str(staging),
            "USERPROFILE": str(staging),
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(staging / "dist")],
        cwd=staging,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        sys.stderr.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        tail = [
            line.strip()
            for line in (result.stdout + result.stderr).splitlines()
            if line.strip()
        ][-3:]
        raise ReleaseError("RELEASE_BUILD_FAILED", " | ".join(tail)[:300])


def normalize_sdist(path: Path, *, epoch: int) -> None:
    """Rewrite a produced sdist with fixed owners, times, modes and order.

    ``python -m build`` keeps the building account's uid, user name and file
    times in the tar header.  Those are host details that must not leak and
    that make two builds of one commit differ, so the archive is rebuilt from
    its own members with normalized metadata.
    """
    members: list[tuple[str, bytes]] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ReleaseError("RELEASE_ARCHIVE_LINK", member.name)
            if member.isfile():
                handle = archive.extractfile(member)
                members.append((member.name, handle.read() if handle else b""))
    members.sort()
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = epoch
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    path.write_bytes(buffer.getvalue())


def wheel_resource_records(path: Path) -> tuple[list[dict], int]:
    """Logical resource name, install location and hash, read from the wheel."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        layout_name = "thu_assistant/_bundled/layout.json"
        if layout_name not in names:
            raise ReleaseError("RELEASE_WHEEL_LAYOUT_MISSING", layout_name)
        layout = json.loads(archive.read(layout_name))
        rows = []
        for resource_id, relative in sorted(layout.get("resources", {}).items()):
            member = "thu_assistant/_bundled/" + relative
            if member not in names:
                raise ReleaseError("RELEASE_WHEEL_RESOURCE_MISSING", f"{resource_id}: {relative}")
            data = archive.read(member)
            rows.append(
                {
                    "id": resource_id,
                    "install": member,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
        bundled = sum(1 for name in names if name.startswith("thu_assistant/_bundled/"))
    return rows, bundled


def artifact_record(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "name": path.name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def build_candidate(dist: Path = DIST) -> dict:
    require_clean_tracked_tree()
    manifest = materialize.load_manifest(ROOT)
    version = declared_version()
    commit = git_commit()
    files = committed_sources(manifest)
    findings = materialize.scan_forbidden(files, manifest)
    if findings:
        raise ReleaseError("RELEASE_FORBIDDEN_FILE", findings[0]["file"])
    missing_licenses = [name for name in manifest["license_files"] if name not in files]
    if missing_licenses:
        raise ReleaseError("RELEASE_LICENSE_MISSING", missing_licenses[0])

    dist.mkdir(exist_ok=True)
    for stale in dist.iterdir():
        if stale.is_file() and (
            stale.name in {RELEASE_MANIFEST, "SHA256SUMS"}
            or stale.name.startswith(("openthu-", "Openthu-"))
        ):
            stale.unlink()

    prefix = f"Openthu-{version}"
    source_zip = dist / f"{prefix}-source.zip"
    source_tar = dist / f"{prefix}-source.tar.gz"
    write_source_zip(source_zip, prefix, files)
    write_source_tar(source_tar, prefix, files)

    with tempfile.TemporaryDirectory(prefix="openthu-release-") as tmp:
        staging = Path(tmp) / "tree"
        stage_tree(files, staging)
        run_build(staging, commit=commit, commit_time=git_commit_time())
        for produced in sorted((staging / "dist").iterdir()):
            shutil.copy2(produced, dist / produced.name)
        for produced in sorted(dist.glob("openthu-*.tar.gz")):
            normalize_sdist(produced, epoch=git_commit_time())

    artifacts = sorted(path for path in dist.iterdir() if path.is_file())
    artifacts = [path for path in artifacts if path.name not in {RELEASE_MANIFEST, "SHA256SUMS"}]
    records = [artifact_record(path) for path in artifacts]
    wheel = next((path for path in artifacts if path.suffix == ".whl"), None)
    resources, bundled_files = wheel_resource_records(wheel) if wheel else ([], 0)
    source_archive = next(
        (row for row in records if row["name"].startswith("Openthu-") and row["name"].endswith("-source.zip")),
        None,
    )
    source_summary = {
        "files": sum(1 for name in files if not name.startswith(".git")),
        "bytes": sum(len(data) for data in files.values()),
        "archive_sha256": source_archive["sha256"] if source_archive else None,
    }
    release = {
        "schema_version": "openthu-release-manifest-v1",
        "product": "openthu",
        "version": version,
        "commit": commit,
        "python_requires": manifest["product"]["python_requires"],
        "validated_python": manifest["product"]["validated_python"],
        "node_requires": manifest["product"]["node_requires"],
        "artifact_files": records,
        "source": source_summary,
        "resources": resources,
        "bundled_files": bundled_files,
        "dependencies": manifest["dependencies"],
        "licenses": manifest["licenses"],
        "release_status": "candidate-not-released",
        "published": False,
    }
    (dist / RELEASE_MANIFEST).write_text(
        json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sums = "".join(
        row["sha256"] + "  " + row["name"] + "\n"
        for row in sorted(records, key=lambda row: row["name"])
    )
    (dist / "SHA256SUMS").write_text(sums, encoding="ascii")
    return release


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _wheel_version(archive: zipfile.ZipFile) -> tuple[str, str]:
    metadata = next(
        (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")), None
    )
    if metadata is None:
        raise ReleaseError("RELEASE_WHEEL_METADATA_MISSING", "METADATA")
    text = archive.read(metadata).decode("utf-8", "replace")
    fields = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), value.strip())
    return fields.get("name", ""), fields.get("version", "")


def verify_wheel(path: Path, release: dict, manifest: dict) -> dict:
    report = {"name": path.name, "status": "pass", "checks": []}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        name, version = _wheel_version(archive)
        if name != "openthu" or version != release["version"]:
            raise ReleaseError("RELEASE_WHEEL_VERSION_MISMATCH", f"{name} {version}")
        findings = materialize.scan_forbidden(names, manifest)
        if findings:
            raise ReleaseError("RELEASE_WHEEL_FORBIDDEN_FILE", findings[0]["file"])
        if any(name.startswith(("tests/", "plans/", "vendor/")) for name in names):
            raise ReleaseError("RELEASE_WHEEL_UNEXPECTED_TREE", "tests/plans/vendor at top level")
        layout_name = "thu_assistant/_bundled/layout.json"
        if layout_name not in names:
            raise ReleaseError("RELEASE_WHEEL_LAYOUT_MISSING", layout_name)
        layout = json.loads(archive.read(layout_name))
        if layout.get("product_version") != release["version"]:
            raise ReleaseError("RELEASE_WHEEL_LAYOUT_VERSION", layout.get("product_version", ""))
        for resource, relative in manifest["resource_map"].items():
            member = "thu_assistant/_bundled/" + relative
            if member not in names:
                raise ReleaseError("RELEASE_WHEEL_RESOURCE_MISSING", f"{resource}: {relative}")
        for record in release.get("resources", []):
            member = record.get("install", "")
            if member not in names:
                raise ReleaseError("RELEASE_WHEEL_RESOURCE_MISSING", record.get("id", ""))
            if hashlib.sha256(archive.read(member)).hexdigest() != record.get("sha256"):
                raise ReleaseError("RELEASE_RESOURCE_HASH_MISMATCH", record.get("id", ""))
        for license_file in manifest["license_files"]:
            if not any(
                name.endswith("/" + PurePosixPath(license_file).name) for name in names
            ):
                raise ReleaseError("RELEASE_WHEEL_LICENSE_MISSING", license_file)
        entry_points = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")), None
        )
        if entry_points is None:
            raise ReleaseError("RELEASE_WHEEL_ENTRY_POINTS_MISSING", "entry_points.txt")
        report["checks"] = [
            "metadata",
            "forbidden-files",
            "bundled-layout",
            "resources",
            "licenses",
            "entry-points",
        ]
        report["members"] = len(names)
    return report


def verify_sdist(path: Path, release: dict, manifest: dict) -> dict:
    report = {"name": path.name, "status": "pass", "checks": []}
    with tarfile.open(path) as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    root = names[0].split("/", 1)[0] if names else ""
    relative = {name.split("/", 1)[1] for name in names if "/" in name}
    findings = materialize.scan_forbidden(sorted(relative), manifest)
    if findings:
        raise ReleaseError("RELEASE_SDIST_FORBIDDEN_FILE", findings[0]["file"])
    for required in ("pyproject.toml", "setup.py", "MANIFEST.in", "build_support/distribution.json"):
        if required not in relative:
            raise ReleaseError("RELEASE_SDIST_INCOMPLETE", required)
    for bundle in manifest["bundles"]:
        for required_member in bundle["must_include"]:
            if f"{bundle['source']}/{required_member}" not in relative:
                raise ReleaseError(
                    "RELEASE_SDIST_INCOMPLETE", f"{bundle['source']}/{required_member}"
                )
    if f"{root}/PKG-INFO" not in names:
        raise ReleaseError("RELEASE_SDIST_METADATA_MISSING", "PKG-INFO")
    report["checks"] = ["metadata", "forbidden-files", "build-support", "bundled-sources"]
    report["members"] = len(names)
    return report


def verify(dist: Path = DIST) -> dict:
    manifest = materialize.load_manifest(ROOT)
    manifest_path = dist / RELEASE_MANIFEST
    if not manifest_path.is_file():
        raise ReleaseError("RELEASE_MANIFEST_MISSING", RELEASE_MANIFEST)
    release = json.loads(manifest_path.read_text(encoding="utf-8"))
    if release.get("version") != declared_version():
        raise ReleaseError("RELEASE_VERSION_MISMATCH", release.get("version", ""))
    notes = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if release["version"] not in notes[:600]:
        raise ReleaseError("RELEASE_NOTES_MISSING", "CHANGELOG.md")
    expected = {row["name"]: row for row in release.get("artifact_files", [])}
    present = {path.name for path in dist.iterdir() if path.is_file()}
    missing = sorted(set(expected) - present)
    if missing:
        raise ReleaseError("RELEASE_ARTIFACT_MISSING", missing[0])
    sums_path = dist / "SHA256SUMS"
    if not sums_path.is_file():
        raise ReleaseError("RELEASE_CHECKSUM_MISSING", "SHA256SUMS")
    sums = {}
    for line in sums_path.read_text(encoding="ascii").splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            sums[name] = digest
    for name, row in expected.items():
        digest = hashlib.sha256((dist / name).read_bytes()).hexdigest()
        if digest != row["sha256"] or sums.get(name) != row["sha256"]:
            raise ReleaseError("RELEASE_CHECKSUM_MISMATCH", name)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    sdists = [path for path in sdists if not path.name.startswith("Openthu-")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError("RELEASE_ARTIFACT_SET", f"{len(wheels)} wheel(s), {len(sdists)} sdist(s)")
    return {
        "ok": True,
        "version": release["version"],
        "commit": release["commit"],
        "artifacts": len(expected),
        "wheel": verify_wheel(wheels[0], release, manifest),
        "sdist": verify_sdist(sdists[0], release, manifest),
    }


# --------------------------------------------------------------------------
# Install check outside the repository
# --------------------------------------------------------------------------

PROBE = """
import json, os, sys
from pathlib import Path
import thu_assistant
from thu_assistant import paths

required = {ids!r}
manifest = paths.resource_manifest()
by_id = {{row["id"]: row for row in manifest}}
missing = [rid for rid in required if not by_id[rid]["available"]]
print(json.dumps({{
    "file": thu_assistant.__file__,
    "version": thu_assistant.__version__,
    "source_root": str(paths.source_root()) if paths.source_root() else None,
    "locations": {{rid: by_id[rid]["location"] for rid in sorted(by_id)}},
    "missing": missing,
    "cwd": os.getcwd(),
}}))
"""


def _install_environment() -> dict[str, str]:
    """Child environment without the ambient import path of the checkout."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"}
    }
    return env


def install_check(wheel: Path, *, python: str | None = None) -> dict:
    python = python or sys.executable
    required = ["source.windows_auth", "source.madmodel_proxy"]
    env = _install_environment()
    with tempfile.TemporaryDirectory(prefix="openthu-install-") as tmp:
        root = Path(tmp)
        environment = root / "venv"
        created = subprocess.run(
            [python, "-m", "venv", "--system-site-packages", str(environment)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if created.returncode:
            raise ReleaseError("INSTALL_VENV_FAILED", created.stderr.strip()[:200])
        target_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = subprocess.run(
            [
                str(target_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                "--disable-pip-version-check",
                str(wheel),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if installed.returncode:
            raise ReleaseError("INSTALL_PIP_FAILED", installed.stderr.strip()[:300])
        workdir = root / "elsewhere"
        workdir.mkdir()
        probe = subprocess.run(
            [str(target_python), "-I", "-c", PROBE.format(ids=required)],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if probe.returncode:
            raise ReleaseError("INSTALL_PROBE_FAILED", probe.stderr.strip()[:300])
        report = json.loads(probe.stdout)
        if report["missing"]:
            raise ReleaseError("INSTALL_RESOURCE_MISSING", ",".join(report["missing"]))
        if report["source_root"] is not None:
            raise ReleaseError("INSTALL_SOURCE_FALLBACK", report["source_root"])
        if not all(report["locations"][rid] == "bundled" for rid in required):
            raise ReleaseError("INSTALL_NOT_BUNDLED", json.dumps(report["locations"], sort_keys=True))
        smoke = subprocess.run(
            [str(target_python), "-I", "-m", "thu_assistant.mcp_stdio"],
            cwd=workdir,
            input="".join(
                json.dumps(message) + "\n"
                for message in (
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "list_services", "arguments": {}},
                    },
                )
            ),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if smoke.returncode:
            raise ReleaseError("INSTALL_MCP_FAILED", smoke.stderr.strip()[:300])
        rows = {}
        for line in smoke.stdout.splitlines():
            if line.strip():
                message = json.loads(line)
                rows[message.get("id")] = message
        tools = rows.get(2, {}).get("result", {}).get("tools", [])
        if len(tools) != 29 or rows.get(3, {}).get("result", {}).get("isError"):
            raise ReleaseError("INSTALL_MCP_TOOLS", f"{len(tools)} tools")
        report["mcp_tools"] = len(tools)
        report["venv"] = "temporary"
        return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", nargs="?", choices=["build", "verify", "install-check", "manifest"],
        default="build",
    )
    parser.add_argument("--dist", type=Path, default=DIST, help="artifact directory")
    parser.add_argument("--wheel", type=Path, default=None, help="wheel for install-check")
    args = parser.parse_args(argv)
    if args.command == "manifest":
        print(json.dumps(materialize.load_manifest(ROOT), ensure_ascii=False, indent=2))
        return 0
    if args.command == "build":
        release = build_candidate(args.dist)
        result = verify(args.dist)
        print(json.dumps({"build": release, "verify": result}, ensure_ascii=False))
        return 0
    if args.command == "verify":
        print(json.dumps(verify(args.dist), ensure_ascii=False))
        return 0
    wheel = args.wheel or next(iter(sorted(args.dist.glob("*.whl"))), None)
    if wheel is None:
        raise ReleaseError("RELEASE_ARTIFACT_MISSING", "*.whl")
    print(json.dumps(install_check(wheel), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
