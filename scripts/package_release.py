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
    """Every bundled file with its install location and hash, read from the wheel."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        layout_name = "thu_assistant/_bundled/layout.json"
        if layout_name not in names:
            raise ReleaseError("RELEASE_WHEEL_LAYOUT_MISSING", layout_name)
        layout = json.loads(archive.read(layout_name))
        logical = {
            "thu_assistant/_bundled/" + relative: resource_id
            for resource_id, relative in layout.get("resources", {}).items()
        }
        rows = []
        for member in sorted(names):
            if not member.startswith("thu_assistant/_bundled/"):
                continue
            if member.endswith("/" + materialize.LAYOUT_NAME):
                continue
            data = archive.read(member)
            rows.append(
                {
                    "id": logical.get(member, "bundled:" + member.split("/_bundled/", 1)[1]),
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
    """Build every artifact, then swap the finished directory into place.

    A failed build leaves the previous candidate untouched: everything is
    written to a sibling temporary directory and only a complete, verified set
    replaces ``dist``.
    """
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

    dist = Path(dist)
    dist.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".openthu-dist-", dir=dist.parent))
    try:
        prefix = f"Openthu-{version}"
        source_zip = work / f"{prefix}-source.zip"
        source_tar = work / f"{prefix}-source.tar.gz"
        write_source_zip(source_zip, prefix, files)
        write_source_tar(source_tar, prefix, files)

        with tempfile.TemporaryDirectory(prefix="openthu-release-") as tmp:
            staging = Path(tmp) / "tree"
            stage_tree(files, staging)
            run_build(staging, commit=commit, commit_time=git_commit_time())
            for produced in sorted((staging / "dist").iterdir()):
                shutil.copy2(produced, work / produced.name)

        epoch = git_commit_time()
        for produced in sorted(work.glob("openthu-*.tar.gz")):
            normalize_sdist(produced, epoch=epoch)
        for produced in sorted(work.glob("*.whl")):
            harden_bundled_modes(produced)

        artifacts = sorted(path for path in work.iterdir() if path.is_file())
        records = [artifact_record(path) for path in artifacts]
        wheel = next((path for path in artifacts if path.suffix == ".whl"), None)
        resources, bundled_files = wheel_resource_records(wheel) if wheel else ([], 0)
        source_archive = next(
            (
                row
                for row in records
                if row["name"].startswith("Openthu-") and row["name"].endswith("-source.zip")
            ),
            None,
        )
        source_summary = {
            "files": len(files),
            "archive_members": len(files),
            "bytes": sum(len(data) for data in files.values()),
            "archive_sha256": source_archive["sha256"] if source_archive else None,
        }
        release = {
            "schema_version": "openthu-release-manifest-v1",
            "product": "openthu",
            "version": version,
            "commit": commit,
            "dirty": False,
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
        (work / RELEASE_MANIFEST).write_text(
            json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sums = "".join(
            row["sha256"] + "  " + row["name"] + "\n"
            for row in sorted(records, key=lambda row: row["name"])
        )
        (work / "SHA256SUMS").write_text(sums, encoding="ascii")

        backup = dist.with_name(dist.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if dist.exists():
            dist.rename(backup)
        try:
            work.rename(dist)
        except OSError:
            if backup.exists():
                backup.rename(dist)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return release


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _metadata_fields(archive: zipfile.ZipFile) -> dict[str, list[str]]:
    """Parse every METADATA header, keeping repeated fields such as Requires-Dist."""
    metadata = next(
        (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")), None
    )
    if metadata is None:
        raise ReleaseError("RELEASE_WHEEL_METADATA_MISSING", "METADATA")
    text = archive.read(metadata).decode("utf-8", "replace")
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), []).append(value.strip())
    return fields


def _wheel_version(archive: zipfile.ZipFile) -> tuple[str, str]:
    fields = _metadata_fields(archive)
    name = (fields.get("name") or [""])[0]
    version = (fields.get("version") or [""])[0]
    return name, version


def declared_scripts() -> dict[str, str]:
    """The console entry points declared in pyproject, as ``name -> target``."""
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = spec.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict) or not scripts:
        raise ReleaseError("RELEASE_ENTRY_POINTS_MISSING", "[project.scripts]")
    return {str(name): str(target) for name, target in scripts.items()}


def _parse_requires_dist(value: str) -> tuple[str, str | None]:
    """Split one Requires-Dist into ``pkg==version`` plus its extra marker."""
    requirement, _, marker = value.partition(";")
    requirement = requirement.strip()
    marker = marker.strip()
    extra = None
    if marker:
        parts = marker.replace(" ", "").split("==")
        if len(parts) == 2 and parts[0] == 'extra':
            extra = parts[1].strip('"\'')
    return requirement, extra


def expected_license_members(manifest: dict, version: str) -> list[str]:
    """Map each declared licence file to the exact wheel member that must hold it."""
    members: list[str] = []
    for name in manifest["license_files"]:
        posix = PurePosixPath(name)
        if len(posix.parts) == 1:
            members.append(f"openthu-{version}.dist-info/licenses/{posix.name}")
        elif posix.parts[0] == "src":
            members.append("/".join(posix.parts[1:]))
        else:
            members.append("thu_assistant/_bundled/" + posix.as_posix())
    return members


def harden_bundled_modes(path: Path) -> None:
    """Mark bundled resources read-only inside the wheel.

    ``materialize`` writes them read-only in the build tree; the wheel carries
    the mode in the zip entry, so the installed files stay read-only as well.
    Content and RECORD hashes are untouched.
    """
    staging = path.with_name(path.name + ".staging")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(
        staging, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.startswith("thu_assistant/_bundled/"):
                info.external_attr = 0o100444 << 16
                info.create_system = 3
            target.writestr(info, data)
    staging.replace(path)


def _head_commit() -> str | None:
    try:
        return git_commit()
    except ReleaseError:
        return None


def verify_wheel(path: Path, release: dict, manifest: dict) -> dict:
    report = {"name": path.name, "status": "pass", "checks": []}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        fields = _metadata_fields(archive)
        name = (fields.get("name") or [""])[0]
        version = (fields.get("version") or [""])[0]
        if name != "openthu" or version != release["version"]:
            raise ReleaseError("RELEASE_WHEEL_VERSION_MISMATCH", f"{name} {version}")
        requires_python = (fields.get("requires-python") or [""])[0]
        if requires_python != manifest["product"]["python_requires"]:
            raise ReleaseError("RELEASE_WHEEL_PYTHON_REQUIREMENT", requires_python)

        runtime = manifest["dependencies"]["runtime"]
        optional = manifest["dependencies"].get("optional", {})
        seen: dict[str, set[str | None]] = {}
        for value in fields.get("requires-dist") or []:
            requirement, extra = _parse_requires_dist(value)
            seen.setdefault(requirement, set()).add(extra)
        for package, pin in runtime.items():
            requirement = f"{package}=={pin}"
            if requirement not in seen or seen[requirement] != {None}:
                raise ReleaseError("RELEASE_WHEEL_DEPENDENCY_MISSING", requirement)
        for extra_name, packages in optional.items():
            for package, pin in packages.items():
                requirement = f"{package}=={pin}"
                if extra_name not in (seen.get(requirement) or set()):
                    raise ReleaseError(
                        "RELEASE_WHEEL_OPTIONAL_DEPENDENCY", f"{extra_name}:{requirement}"
                    )
        development = manifest["dependencies"].get("development", {})
        allowed = {name.lower() for name in runtime}
        for packages in optional.values():
            allowed.update(name.lower() for name in packages)
        for package in development:
            if package.lower() in allowed:
                continue
            for requirement, extras in seen.items():
                if requirement.split("==")[0].lower() != package.lower():
                    continue
                if None in extras:
                    # A development tool declared as a hard runtime dependency
                    # would make a normal install pull the test framework.
                    raise ReleaseError("RELEASE_WHEEL_DEV_DEPENDENCY", package)

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
        for member in expected_license_members(manifest, release["version"]):
            if member not in names:
                raise ReleaseError("RELEASE_WHEEL_LICENSE_MISSING", member)
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
        bundled_members = [name for name in names if name.startswith("thu_assistant/_bundled/")]
        recorded = [name for name in bundled_members if not name.endswith("/" + materialize.LAYOUT_NAME)]
        if len(recorded) != len(release.get("resources", [])):
            raise ReleaseError(
                "RELEASE_RESOURCE_RECORD_COUNT",
                f"{len(recorded)} members, {len(release.get('resources', []))} records",
            )
        if os.name != "nt":
            for member in bundled_members:
                mode = (archive.getinfo(member).external_attr >> 16) & 0o777
                if mode & 0o200:
                    raise ReleaseError("RELEASE_RESOURCE_WRITABLE", member)
        entry_points = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")), None
        )
        if entry_points is None:
            raise ReleaseError("RELEASE_WHEEL_ENTRY_POINTS_MISSING", "entry_points.txt")
        published: dict[str, str] = {}
        section = None
        for line in archive.read(entry_points).decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if section != "console_scripts" or not stripped or stripped.startswith("#"):
                continue
            entry, _, target = stripped.partition("=")
            published[entry.strip()] = target.strip()
        expected_scripts = declared_scripts()
        if published != expected_scripts:
            missing = sorted(set(expected_scripts) - set(published))
            extra = sorted(set(published) - set(expected_scripts))
            raise ReleaseError(
                "RELEASE_WHEEL_ENTRY_POINTS_MISMATCH",
                f"missing={missing} extra={extra}",
            )
        report["checks"] = [
            "metadata",
            "requires-python",
            "requires-dist",
            "forbidden-files",
            "bundled-layout",
            "resources",
            "resource-modes",
            "licenses",
            "entry-points",
        ]
        report["members"] = len(names)
        report["bundled_members"] = len(bundled_members)
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
    with tarfile.open(path) as archive:
        handle = archive.extractfile(f"{root}/PKG-INFO")
        pkg_info = handle.read().decode("utf-8", "replace") if handle else ""
    fields: dict[str, str] = {}
    for line in pkg_info.splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), value.strip())
    if fields.get("version") != release["version"]:
        raise ReleaseError("RELEASE_SDIST_VERSION_MISMATCH", fields.get("version", ""))
    if fields.get("requires-python") != manifest["product"]["python_requires"]:
        raise ReleaseError("RELEASE_SDIST_PYTHON_REQUIREMENT", fields.get("requires-python", ""))
    report["checks"] = [
        "metadata",
        "version",
        "python-requirement",
        "forbidden-files",
        "build-support",
        "bundled-sources",
    ]
    report["members"] = len(names)
    return report


def verify(dist: Path = DIST, *, expected_commit: str | None = None) -> dict:
    manifest = materialize.load_manifest(ROOT)
    manifest_path = dist / RELEASE_MANIFEST
    if not manifest_path.is_file():
        raise ReleaseError("RELEASE_MANIFEST_MISSING", RELEASE_MANIFEST)
    release = json.loads(manifest_path.read_text(encoding="utf-8"))
    if release.get("version") != declared_version():
        raise ReleaseError("RELEASE_VERSION_MISMATCH", release.get("version", ""))
    head = expected_commit or _head_commit()
    if head is None:
        raise ReleaseError("RELEASE_COMMIT_UNKNOWN", "pass --expected-commit")
    if release.get("commit") != head:
        raise ReleaseError(
            "RELEASE_COMMIT_MISMATCH", f"artifact {release.get('commit')} != HEAD {head}"
        )
    if release.get("dirty") is not False:
        raise ReleaseError("RELEASE_DIRTY_ARTIFACT", str(release.get("dirty")))
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
import cryptography
import PIL
import requests
import thu_assistant
from thu_assistant import paths

prefix = Path(sys.prefix).resolve()
base_prefix = Path(sys.base_prefix).resolve()
deps = {{
    "cryptography": Path(cryptography.__file__).resolve(),
    "Pillow": Path(PIL.__file__).resolve(),
    "requests": Path(requests.__file__).resolve(),
}}
pkg_file = Path(thu_assistant.__file__).resolve()

def _inside(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False

required = {ids!r}
manifest = paths.resource_manifest()
by_id = {{row["id"]: row for row in manifest}}
missing = [rid for rid in required if not by_id[rid]["available"]]
outside = [name for name, loc in deps.items() if not _inside(loc, prefix)]
if not _inside(pkg_file, prefix):
    outside.append("openthu")
print(json.dumps({{
    "file": thu_assistant.__file__,
    "version": thu_assistant.__version__,
    "source_root": str(paths.source_root()) if paths.source_root() else None,
    "locations": {{rid: by_id[rid]["location"] for rid in sorted(by_id)}},
    "missing": missing,
    "cwd": os.getcwd(),
    "isolated_venv": prefix != base_prefix,
    "outside_prefix": outside,
    "installed_dependencies": sorted(deps),
}}))
"""


STAGE_WHEELHOUSE_SCRIPT = r"""
import base64, csv, email.parser, hashlib, importlib.metadata, io, json, re, sys, zipfile
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, canonicalize_version
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.tags import sys_tags
    from pip._vendor.packaging.utils import canonicalize_name, canonicalize_version

wheel_path = Path(sys.argv[1])
dest_dir = Path(sys.argv[2])


def fail(reason):
    print(json.dumps({"ok": False, "error": reason}))
    raise SystemExit(1)


def parse_wheel_tag(dist):
    raw = dist.read_text("WHEEL") or ""
    tags = []
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "tag":
            tags.append(value.strip())
    if not tags:
        return "py3-none-any"
    if len(tags) == 1:
        return tags[0]
    triples = [tuple(tag.split("-", 2)) for tag in tags if tag.count("-") == 2]
    if triples:
        pys = sorted({p for p, _, _ in triples})
        abis = sorted({a for _, a, _ in triples})
        plats = sorted({pl for _, _, pl in triples})
        if len(pys) * len(abis) * len(plats) == len(triples):
            return f"{'.'.join(pys)}-{'.'.join(abis)}-{'.'.join(plats)}"
    supported = [str(tag) for tag in sys_tags()]
    return next((tag for tag in supported if tag in tags), tags[0])


def parse_record(dist):
    text = dist.read_text("RECORD")
    if not text:
        return None
    expected = {}
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        rel = row[0].replace("\\", "/")
        digest = row[1] if len(row) > 1 else ""
        size = row[2] if len(row) > 2 else ""
        expected[rel] = (digest, size)
    return expected


def pack_dist(dist, out_dir):
    name = canonicalize_name(dist.metadata["Name"]).replace("-", "_")
    version = re.sub(r"[^A-Za-z0-9.]+", "_", str(canonicalize_version(dist.version)))
    tag = parse_wheel_tag(dist)
    out_whl = out_dir / f"{name}-{version}-{tag}.whl"
    files = list(dist.files or ())
    if not files:
        fail(f"missing-files:{dist.metadata['Name']}")
    expected_record = parse_record(dist)
    record_rel = None
    for f in files:
        posix = f.as_posix()
        if posix.endswith(".dist-info/RECORD"):
            record_rel = posix
            break
    if record_rel is None:
        dist_info = next((f.parts[0] for f in files if f.parts and f.parts[0].endswith(".dist-info")), None)
        if not dist_info:
            fail(f"missing-dist-info:{dist.metadata['Name']}")
        record_rel = f"{dist_info}/RECORD"
    skip_dist_info = {"RECORD", "RECORD.jws", "RECORD.p7s", "INSTALLER", "REQUESTED", "direct_url.json"}
    rows = []
    with zipfile.ZipFile(out_whl, "w", zipfile.ZIP_DEFLATED) as archive:
        seen_paths = set()
        for f in files:
            parts = f.parts
            if not parts or ".." in parts or f.is_absolute():
                continue
            if "__pycache__" in parts or f.suffix in {".pyc", ".pyo"}:
                continue
            if parts[0].endswith(".dist-info") and len(parts) == 2 and parts[1] in skip_dist_info:
                continue
            rel = f.as_posix()
            if rel in seen_paths:
                continue
            full = Path(dist.locate_file(f))
            if full.is_symlink() or not full.is_file():
                continue
            data = full.read_bytes()
            if expected_record is not None and rel in expected_record:
                exp_hash, exp_size = expected_record[rel]
                if exp_hash.startswith("sha256="):
                    actual_hash = "sha256=" + base64.urlsafe_b64encode(
                        hashlib.sha256(data).digest()
                    ).rstrip(b"=").decode("ascii")
                    if actual_hash != exp_hash:
                        fail(f"record-hash-mismatch:{dist.metadata['Name']}:{rel}")
                if exp_size and int(exp_size) != len(data):
                    fail(f"record-size-mismatch:{dist.metadata['Name']}:{rel}")
            archive.writestr(rel, data)
            seen_paths.add(rel)
            digest_b64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            rows.append([rel, f"sha256={digest_b64}", str(len(data))])
        rows.append([record_rel, "", ""])
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerows(rows)
        archive.writestr(record_rel, buf.getvalue().encode("utf-8"))
    with zipfile.ZipFile(out_whl) as verify_zip:
        if verify_zip.testzip() is not None:
            fail(f"bad-wheel:{out_whl.name}")


with zipfile.ZipFile(wheel_path) as whl:
    meta_name = next((n for n in whl.namelist() if n.endswith(".dist-info/METADATA")), None)
    if not meta_name:
        fail("wheel-metadata-missing")
    meta = email.parser.Parser().parsestr(whl.read(meta_name).decode("utf-8", "replace"))

root_name = canonicalize_name(meta.get("Name", "openthu"))
queue = [(Requirement(raw), "") for raw in (meta.get_all("Requires-Dist") or [])]
visited = set()
dists = {}

while queue:
    req, active_extra = queue.pop(0)
    if req.marker and not req.marker.evaluate({"extra": active_extra}):
        continue
    pkg = canonicalize_name(req.name)
    if pkg == root_name:
        continue
    try:
        dist = importlib.metadata.distribution(pkg)
    except importlib.metadata.PackageNotFoundError:
        fail(f"missing-dependency:{req}")
    if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
        fail(f"version-mismatch:{req}:{dist.version}")
    dists[pkg] = dist
    extras_to_visit = ("",) + tuple(sorted(req.extras))
    for extra in extras_to_visit:
        state = (pkg, extra)
        if state in visited:
            continue
        visited.add(state)
        for sub in (dist.requires or ()):
            queue.append((Requirement(sub), extra))

for pkg in sorted(dists):
    pack_dist(dists[pkg], dest_dir)

print(json.dumps({"ok": True, "packages": sorted(dists)}))
"""


def _install_environment() -> dict[str, str]:
    """Child environment without the ambient import path of the checkout."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"}
    }
    return env


def _validate_wheelhouse(path: Path) -> Path:
    resolved = Path(path)
    if resolved.is_symlink() or not resolved.is_dir():
        raise ReleaseError("INSTALL_WHEELHOUSE_INVALID", str(path))
    wheels = sorted(resolved.glob("*.whl"))
    if not wheels:
        raise ReleaseError("INSTALL_WHEELHOUSE_INVALID", "no wheel files found")
    for wheel_file in wheels:
        if wheel_file.is_symlink() or not wheel_file.is_file():
            raise ReleaseError("INSTALL_WHEELHOUSE_INVALID", wheel_file.name)
        try:
            with zipfile.ZipFile(wheel_file) as archive:
                if archive.testzip() is not None:
                    raise ReleaseError("INSTALL_WHEELHOUSE_INVALID", wheel_file.name)
        except (OSError, zipfile.BadZipFile):
            raise ReleaseError("INSTALL_WHEELHOUSE_INVALID", wheel_file.name) from None
    return resolved.resolve()


def _stage_local_wheelhouse(
    wheel: Path,
    destination: Path,
    *,
    python: str,
    env: dict[str, str],
    cwd: Path,
) -> list[str]:
    result = subprocess.run(
        [python, "-c", STAGE_WHEELHOUSE_SCRIPT, str(wheel.resolve()), str(destination)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode:
        detail = result.stdout.strip() or result.stderr.strip()
        raise ReleaseError("INSTALL_DEPENDENCY_UNAVAILABLE", detail[-300:])
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise ReleaseError("INSTALL_DEPENDENCY_UNAVAILABLE", result.stdout[-300:]) from None
    if not payload.get("ok"):
        raise ReleaseError("INSTALL_DEPENDENCY_UNAVAILABLE", str(payload.get("error", "")))
    return list(payload.get("packages", []))


def install_check(
    wheel: Path,
    *,
    python: str | None = None,
    wheelhouse: Path | None = None,
) -> dict:
    python = python or sys.executable
    required = ["source.windows_auth", "source.madmodel_proxy"]
    env = _install_environment()
    with tempfile.TemporaryDirectory(prefix="openthu-install-") as tmp:
        root = Path(tmp)
        workdir = root / "elsewhere"
        workdir.mkdir()
        environment = root / "venv"
        created = subprocess.run(
            [python, "-m", "venv", str(environment)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if created.returncode:
            raise ReleaseError("INSTALL_VENV_FAILED", created.stderr.strip()[:200])
        pyvenv_cfg = environment / "pyvenv.cfg"
        cfg_text = (
            pyvenv_cfg.read_text(encoding="utf-8", errors="replace")
            if pyvenv_cfg.is_file()
            else ""
        )
        if not any(
            line.strip().casefold() == "include-system-site-packages = false"
            for line in cfg_text.splitlines()
        ):
            raise ReleaseError("INSTALL_VENV_NOT_ISOLATED", str(pyvenv_cfg))
        if wheelhouse is not None:
            find_links = _validate_wheelhouse(Path(wheelhouse))
            dependency_source = "wheelhouse"
        else:
            staged = root / "wheelhouse"
            staged.mkdir()
            _stage_local_wheelhouse(
                Path(wheel),
                staged,
                python=python,
                env=env,
                cwd=workdir,
            )
            find_links = staged
            dependency_source = "local-cache"
        target_python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installed = subprocess.run(
            [
                str(target_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(find_links),
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
        if not report.get("isolated_venv"):
            raise ReleaseError("INSTALL_VENV_NOT_ISOLATED", str(environment))
        if report.get("outside_prefix"):
            raise ReleaseError("INSTALL_NOT_ISOLATED", ",".join(report["outside_prefix"]))
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

        # An installed wheel must be able to diagnose itself without the
        # source-only scripts/dev.py, and doing so must not touch user state.
        user_state = root / "user-state"
        (user_state / "downloads").mkdir(parents=True)
        (user_state / "config.json").write_text('{"ui": {"page_size": 20}}', encoding="utf-8")
        (user_state / "downloads" / "keep.bin").write_bytes(b"synthetic download\n")
        before = _snapshot_tree(user_state)
        doctor_env = dict(env)
        doctor_env.update(
            OPENTHU_STATE_DIR=str(user_state),
            OPENTHU_DOWNLOAD_DIR=str(user_state / "downloads"),
            OPENTHU_CONFIG_FILE=str(user_state / "config.json"),
        )
        doctor = subprocess.run(
            [str(target_python), "-I", "-m", "thu_assistant.doctor", "--json"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            env=doctor_env,
        )
        if doctor.returncode:
            raise ReleaseError("INSTALL_DOCTOR_FAILED", doctor.stdout[-300:] or doctor.stderr[-300:])
        diagnostics = json.loads(doctor.stdout)
        if not diagnostics.get("ok") or diagnostics.get("mode") != "installed":
            raise ReleaseError(
                "INSTALL_DOCTOR_REPORT", f"ok={diagnostics.get('ok')} mode={diagnostics.get('mode')}"
            )
        if _snapshot_tree(user_state) != before:
            raise ReleaseError("INSTALL_TOUCHED_USER_STATE", str(user_state.name))
        report["mcp_tools"] = len(tools)
        report["doctor_ok"] = True
        report["user_state_unchanged"] = True
        report["dependency_source"] = dependency_source
        report["venv"] = "temporary"
        return report


def _snapshot_tree(root: Path) -> dict[str, tuple[int, str]]:
    rows: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows[path.relative_to(root).as_posix()] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return rows


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
    parser.add_argument(
        "--wheelhouse",
        type=Path,
        default=None,
        help="verified local wheel directory for offline dependency resolution during install-check",
    )
    parser.add_argument(
        "--expected-commit",
        default=None,
        metavar="SHA",
        help="commit the candidate must have been built from (defaults to HEAD)",
    )
    args = parser.parse_args(argv)
    if args.command == "manifest":
        print(json.dumps(materialize.load_manifest(ROOT), ensure_ascii=False, indent=2))
        return 0
    if args.command == "build":
        release = build_candidate(args.dist)
        result = verify(args.dist, expected_commit=args.expected_commit)
        print(json.dumps({"build": release, "verify": result}, ensure_ascii=False))
        return 0
    if args.command == "verify":
        print(
            json.dumps(
                verify(args.dist, expected_commit=args.expected_commit), ensure_ascii=False
            )
        )
        return 0
    wheel = args.wheel or next(iter(sorted(args.dist.glob("*.whl"))), None)
    if wheel is None:
        raise ReleaseError("RELEASE_ARTIFACT_MISSING", "*.whl")
    print(json.dumps(install_check(wheel, wheelhouse=args.wheelhouse), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
