"""Public configuration, privacy scanning and manual-checkpoint contracts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]



@pytest.mark.skipif(os.name=='nt',reason='Non-Windows production encryption failure contract')
def test_production_store_still_rejects_missing_dpapi(tmp_path):
    from thu_assistant.action_store import ActionStore
    from thu_assistant.bridge import CampusError
    with pytest.raises(CampusError,match='ACTION_LOG_ENCRYPT_FAILED'):
        ActionStore(tmp_path).write({'operation_id':'a'*32,'value':'synthetic'})
    assert not list(tmp_path.glob('*.dpapi'))


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_scanner_flags_synthetic_secret_without_echoing_its_value():
    scanner=load('privacy_scan')
    value='ghp_'+'a'*36
    rows=scanner.findings('example.txt',value.encode())
    assert rows==[{'file':'example.txt','line':1,'rule':'github-token'}]
    assert value not in json.dumps(rows)


def test_configuration_generated_for_current_checkout_without_credentials(tmp_path):
    output=tmp_path/'mcp.json'
    subprocess.run([sys.executable,str(ROOT/'scripts/configure_mcp.py'),'--output',str(output)],check=True,capture_output=True)
    server=json.loads(output.read_text(encoding='utf-8'))['mcpServers']['tsinghua-assistant']
    assert server['command']==sys.executable
    assert server['env']['PYTHONPATH']==str(ROOT/'src')
    assert set(server['env'])=={'PYTHONUTF8','PYTHONPATH','THU_PYTHON','THU_NODE','THU_MADMODEL_DIR','THU_ACCESS_HELPER','THU_LEARN_PROVIDER'}


def new_relay_repo(tmp_path):
    (tmp_path/'scripts').mkdir();(tmp_path/'handoffs').mkdir()
    shutil.copyfile(ROOT/'scripts/handoff.py',tmp_path/'scripts/handoff.py')
    (tmp_path/'handoffs/TEMPLATE.md').write_text('unfilled template\n')
    (tmp_path/'handoffs/T1.md').write_text('Goal: synthetic task; reproduce using fixtures; actual: failing fixture; next: Codex\n')
    def git(*args):
        return subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True,text=True).stdout.strip()
    git('init','-b','hermes/1-example');git('config','user.name','Synthetic Test')
    git('config','user.email','tests@example.invalid');git('config','commit.gpgsign','false')
    git('add','.');git('commit','-m','Synthetic task state')
    return git


def test_manual_handoff_pins_committed_code_and_has_no_dispatch(tmp_path):
    git=new_relay_repo(tmp_path)
    commit=git('rev-parse','HEAD')
    result=subprocess.run([sys.executable,str(tmp_path/'scripts/handoff.py'),'T1','--to','codex'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0
    checkpoint=json.loads((tmp_path/'handoffs/T1.json').read_text())
    assert checkpoint['code_commit']==commit
    assert checkpoint['next_owner']=='codex' and checkpoint['dispatch']=='manual'
    assert git('rev-parse','HEAD')==commit


def test_manual_handoff_refuses_to_lose_dirty_work(tmp_path):
    new_relay_repo(tmp_path)
    (tmp_path/'unfinished.txt').write_text('not committed\n')
    result=subprocess.run([sys.executable,str(tmp_path/'scripts/handoff.py'),'T1','--to','codex'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode!=0
    assert not (tmp_path/'handoffs/T1.json').exists()


# ---------------------------------------------------------------------------
# 6.3 complete distribution layout: version source, resource manifest, build,
# verification, install check and diagnostics capability reporting.
# ---------------------------------------------------------------------------

import tomllib


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_materialize():
    spec = importlib.util.spec_from_file_location(
        "distribution_materialize", ROOT / "build_support" / "materialize.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_metadata():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def declared_version():
    reference = project_metadata()["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    module_name, _, attribute = reference.rpartition(".")
    namespace = {}
    path = ROOT / "src" / Path(*module_name.split(".")).with_suffix(".py")
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace[attribute]


def manifest():
    return load_materialize().load_manifest(ROOT)


def test_one_version_source_feeds_package_metadata_and_changelog():
    version = declared_version()
    assert project_metadata()["project"]["dynamic"] == ["version"]
    assert version == "0.2.0"
    sys.path.insert(0, str(ROOT / "src"))
    try:
        import thu_assistant
        import learn_assistant

        assert thu_assistant.__version__ == version
        assert learn_assistant.__version__ == version
    finally:
        sys.path.pop(0)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert version in changelog.split("\n", 3)[1] or version in changelog[:400]


def test_python_and_node_floor_are_declared_once_in_the_manifest():
    spec = project_metadata()
    data = manifest()
    assert spec["project"]["requires-python"] == data["product"]["python_requires"] == ">=3.12"
    assert data["product"]["node_requires"] == ">=24"
    assert data["product"]["node_lockfile"] == "src/thu_assistant/package-lock.json"


def test_pinned_dependencies_match_the_metadata_and_requirement_files():
    spec = project_metadata()
    data = manifest()
    runtime = {row.split("==")[0]: row.split("==")[1] for row in spec["project"]["dependencies"]}
    assert runtime == data["dependencies"]["runtime"]
    browser = {
        row.split("==")[0]: row.split("==")[1]
        for row in spec["project"]["optional-dependencies"]["browser"]
    }
    assert browser == data["dependencies"]["optional"]["browser"]
    pinned = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("==")
            pinned[name] = value
    assert pinned == data["dependencies"]["runtime"]
    development = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert development.splitlines()[0].strip() == "-r requirements.txt"


def test_no_development_tool_is_a_runtime_dependency():
    runtime = project_metadata()["project"]["dependencies"]
    for tool in ("pytest", "build", "jsonschema", "playwright"):
        assert all(tool not in row for row in runtime), tool


def test_development_pins_match_the_manifest():
    pins = {}
    for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        name, _, value = line.partition("==")
        if value:
            pins[name] = value
    assert pins == manifest()["dependencies"]["development"]


def test_glob_translation_matches_double_star_without_matching_everything():
    module = load_materialize()
    assert module.matches_any("core/tokenizer.js", ["**/*.js"])
    assert module.matches_any("proxy.js", ["**/*.js"])
    assert not module.matches_any("docs/README.md", ["**/*.js"])
    assert module.matches_any("vendor/licenses/x", ["vendor/**"])
    assert module.matches_any("test/a.test.js", ["test/**"])
    assert not module.matches_any("platform/a.js", ["test/**"])


def test_materialize_copies_bundles_and_writes_the_layout(tmp_path):
    module = load_materialize()
    source = tmp_path / "repo"
    (source / "runtime" / "windows").mkdir(parents=True)
    (source / "runtime" / "windows" / "access.js").write_text("// helper\n", encoding="utf-8")
    (source / "runtime" / "windows" / "import-session.py").write_text("# helper\n", encoding="utf-8")
    data = {
        "schema_version": "openthu-distribution-manifest-v1",
        "bundles": [
            {
                "id": "runtime.windows",
                "source": "runtime/windows",
                "install": "runtime/windows",
                "include": ["*.js", "*.py"],
                "must_include": ["access.js", "import-session.py"],
            }
        ],
        "resource_map": {"source.windows_auth": "runtime/windows/access.js"},
        "forbidden": {"names": [".env"], "suffixes": [".key"], "directories": ["logs"]},
        "licenses": {"openthu": "MIT"},
    }
    package = tmp_path / "pkg"
    package.mkdir()
    summary = module.materialize(source, package, data, version="9.9.9", commit="deadbeef")
    assert summary["files"] == 2
    assert (package / "_bundled" / "runtime/windows/access.js").is_file()
    layout = json.loads((package / "_bundled" / "layout.json").read_text(encoding="utf-8"))
    assert layout["product_version"] == "9.9.9"
    assert layout["commit"] == "deadbeef"
    assert layout["resources"] == {"source.windows_auth": "runtime/windows/access.js"}
    if os.name != "nt":
        # POSIX keeps the documented read-only resource mode; Windows only
        # exposes the read-only attribute through chmod.
        assert oct((package / "_bundled/runtime/windows/access.js").stat().st_mode & 0o777) == "0o644"
    assert os.access(package / "_bundled/runtime/windows/access.js", os.R_OK)


def test_materialize_refuses_an_incomplete_bundle(tmp_path):
    module = load_materialize()
    source = tmp_path / "repo"
    (source / "runtime").mkdir(parents=True)
    (source / "runtime" / "access.js").write_text("", encoding="utf-8")
    data = {
        "schema_version": "openthu-distribution-manifest-v1",
        "bundles": [
            {
                "id": "runtime",
                "source": "runtime",
                "install": "runtime",
                "include": ["*.js"],
                "must_include": ["missing.js"],
            }
        ],
        "resource_map": {},
        "forbidden": {},
        "licenses": {},
    }
    package = tmp_path / "pkg"
    package.mkdir()
    with pytest.raises(module.DistributionError) as caught:
        module.materialize(source, package, data, version="0")
    assert caught.value.code == "DISTRIBUTION_BUNDLE_INCOMPLETE"
    assert not (package / "_bundled").exists()


def test_forbidden_scan_flags_keys_caches_logs_and_user_files():
    module = load_materialize()
    rows = module.scan_forbidden(
        [
            "src/thu_assistant/gateway.js",
            ".env",
            ".env.example",
            "keys/master.key",
            "session.dpapi",
            "logs/app.log",
            "node_modules/tough-cookie/index.js",
            "cache/session.json",
            "docs/README.md",
        ],
        manifest(),
    )
    flagged = {row["file"]: row["rule"] for row in rows}
    assert ".env.example" not in flagged
    assert "docs/README.md" not in flagged
    assert "src/thu_assistant/gateway.js" not in flagged
    for name in ("keys/master.key", "session.dpapi", "logs/app.log", "node_modules/tough-cookie/index.js"):
        assert name in flagged


# --- resource resolution ---------------------------------------------------


def _package_with_bundle(root, *, resources, version="0.2.0", commit="abc"):
    package = root / "site-packages" / "thu_assistant"
    package.mkdir(parents=True)
    bundled = package / "_bundled"
    (bundled / "runtime" / "windows").mkdir(parents=True)
    (bundled / "runtime" / "windows" / "access.js").write_text("// bundled\n", encoding="utf-8")
    (bundled / "vendor" / "madmodel-proxy").mkdir(parents=True)
    (bundled / "vendor" / "madmodel-proxy" / "package.json").write_text("{}\n", encoding="utf-8")
    (bundled / "layout.json").write_text(
        json.dumps(
            {
                "schema_version": "openthu-bundled-layout-v1",
                "product_version": version,
                "commit": commit,
                "resources": resources,
            }
        ),
        encoding="utf-8",
    )
    for name in ("gateway.js", "services.json", "security.js", "cookies.js"):
        (package / name).write_text("", encoding="utf-8")
    return package


def test_a_wheel_install_resolves_bundled_resources_without_a_checkout(tmp_path):
    from thu_assistant import paths

    package = _package_with_bundle(
        tmp_path,
        resources={
            "source.windows_auth": "runtime/windows/access.js",
            "source.madmodel_proxy": "vendor/madmodel-proxy/package.json",
        },
    )
    helper = paths.resolve_resource(
        "source.windows_auth", package_root_value=package, source_root_value=None
    )
    assert helper.available
    assert helper.location == "bundled"
    assert helper.path.parent.name == "windows"
    vendor = paths.resolve_resource(
        "source.madmodel_proxy", package_root_value=package, source_root_value=None
    )
    assert vendor.location == "bundled"
    assert vendor.path.parent.name == "madmodel-proxy"
    rows = paths.resource_manifest(package_root_value=package, source_root_value=None)
    assert all("path" not in row for row in rows)
    locations = {row["id"]: row["location"] for row in rows if row["scope"] == "source"}
    assert locations["source.windows_auth"] == "bundled"
    assert locations["source.madmodel_proxy"] == "bundled"
    assert locations["source.tests"] is None


def test_a_live_checkout_wins_over_a_stale_bundled_copy(tmp_path):
    from thu_assistant import paths

    package = _package_with_bundle(
        tmp_path, resources={"source.windows_auth": "runtime/windows/access.js"}
    )
    checkout = tmp_path / "checkout"
    (checkout / "runtime" / "windows").mkdir(parents=True)
    (checkout / "runtime" / "windows" / "access.js").write_text("// live\n", encoding="utf-8")
    resolved = paths.resolve_resource(
        "source.windows_auth", package_root_value=package, source_root_value=checkout
    )
    assert resolved.location == "source"
    assert resolved.path == (checkout / "runtime" / "windows" / "access.js").resolve()


def test_a_layout_path_escape_is_refused(tmp_path):
    from thu_assistant import paths

    package = _package_with_bundle(
        tmp_path, resources={"source.windows_auth": "../../../etc/passwd"}
    )
    resolved = paths.resolve_resource(
        "source.windows_auth", package_root_value=package, source_root_value=None
    )
    assert resolved.available is False
    broken = package / "_bundled" / "layout.json"
    broken.write_text("{not json", encoding="utf-8")
    assert paths.bundled_layout(package_root_value=package) is None
    assert not paths.resolve_resource(
        "source.windows_auth", package_root_value=package, source_root_value=None
    ).available


# --- candidate build -------------------------------------------------------

DECOYS = {
    ".local/synthetic-master.key": b"SYNTHETIC-NOT-A-KEY\n",
    "cache/session.json": b'{"token": "synthetic"}',
    "logs/synthetic.log": b"synthetic log line\n",
    "downloads/synthetic.pdf": b"%PDF-1.4 synthetic\n",
    ".env": b"OPENTHU_SECRET_SYNTHETIC=dummy\n",
}


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    module = load_script("package_release")
    created = []
    for name, data in DECOYS.items():
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        created.append(path)
    dist = tmp_path_factory.mktemp("candidate-dist")
    try:
        try:
            release = module.build_candidate(dist)
        except module.ReleaseError as error:
            pytest.fail(
                "the candidate build only runs on a clean tracked tree; "
                f"commit or stash the pending change first ({error.code})"
            )
        report = module.verify(dist)
    finally:
        for path in created:
            path.unlink(missing_ok=True)
        for directory in (".local", "cache", "logs", "downloads"):
            leftover = ROOT / directory
            if leftover.is_dir() and not any(leftover.iterdir()):
                leftover.rmdir()
    return {"module": module, "dist": dist, "release": release, "report": report}


def test_candidate_build_produces_the_declared_artifact_set(candidate):
    names = sorted(path.name for path in candidate["dist"].iterdir())
    assert names == [
        "Openthu-0.2.0-source.tar.gz",
        "Openthu-0.2.0-source.zip",
        "SHA256SUMS",
        "openthu-0.2.0-py3-none-any.whl",
        "openthu-0.2.0.tar.gz",
        "release-manifest.json",
    ]
    release = candidate["release"]
    assert release["version"] == declared_version()
    assert release["release_status"] == "candidate-not-released"
    assert release["published"] is False
    assert release["commit"] == candidate["module"].git_commit()
    assert candidate["report"]["ok"] is True


def test_release_manifest_checksums_match_the_files(candidate):
    dist = candidate["dist"]
    release = json.loads((dist / "release-manifest.json").read_text(encoding="utf-8"))
    sums = {}
    for line in (dist / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, _, name = line.partition("  ")
        sums[name] = digest
    import hashlib

    for row in release["artifact_files"]:
        assert sums[row["name"]] == row["sha256"]
        assert hashlib.sha256((dist / row["name"]).read_bytes()).hexdigest() == row["sha256"]


def test_wheel_is_the_complete_layout_with_licenses_and_entry_points(candidate):
    import zipfile

    wheel = next(candidate["dist"].glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read("openthu-0.2.0.dist-info/METADATA").decode("utf-8")
        assert "Version: 0.2.0" in metadata
        assert "Requires-Python: >=3.12" in metadata
        for dependency in ("cryptography==50.0.2", "Pillow==13.0.0", "requests==2.33.0"):
            assert "Requires-Dist: " + dependency in metadata
        for license_name in ("LICENSE", "THIRD_PARTY.md"):
            assert any(name.endswith("/licenses/" + license_name) for name in names)
        assert "thu_assistant/_bundled/layout.json" in names
        for required in (
            "thu_assistant/_bundled/runtime/windows/access.js",
            "thu_assistant/_bundled/runtime/windows/import-session.py",
            "thu_assistant/_bundled/vendor/madmodel-proxy/package.json",
            "thu_assistant/_bundled/vendor/madmodel-proxy/madmodel-auth.js",
            "thu_assistant/_bundled/vendor/madmodel-proxy/vendor/deepseek-tokenizer.json",
        ):
            assert required in names
        entry_points = archive.read("openthu-0.2.0.dist-info/entry_points.txt").decode("utf-8")
        for entry in ("openthu", "openthu-mcp", "learn-assistant", "tsinghua-assistant-mcp"):
            assert entry + " =" in entry_points
        assert not any(name.startswith(("tests/", "plans/", "vendor/")) for name in names)
        assert not any("node_modules" in name for name in names)


def test_source_archives_exclude_synthetic_keys_caches_and_logs(candidate):
    import tarfile
    import zipfile

    dist = candidate["dist"]
    with zipfile.ZipFile(dist / "Openthu-0.2.0-source.zip") as archive:
        zip_names = set(archive.namelist())
    with tarfile.open(dist / "Openthu-0.2.0-source.tar.gz") as archive:
        tar_names = {member.name for member in archive.getmembers()}
    for name in DECOYS:
        assert "Openthu-0.2.0/" + name not in zip_names
        assert "Openthu-0.2.0/" + name not in tar_names
    assert "Openthu-0.2.0/pyproject.toml" in zip_names
    assert "Openthu-0.2.0/src/thu_assistant/gateway.js" in zip_names


def test_sdist_contains_the_sources_needed_to_rebuild_the_bundles(candidate):
    import tarfile

    with tarfile.open(candidate["dist"] / "openthu-0.2.0.tar.gz") as archive:
        names = {member.name for member in archive.getmembers()}
    for required in (
        "openthu-0.2.0/setup.py",
        "openthu-0.2.0/build_support/distribution.json",
        "openthu-0.2.0/build_support/materialize.py",
        "openthu-0.2.0/vendor/madmodel-proxy/package.json",
        "openthu-0.2.0/vendor/madmodel-proxy/vendor/deepseek-tokenizer.json",
        "openthu-0.2.0/runtime/windows/access.js",
        "openthu-0.2.0/vendor/madmodel-proxy/LICENSE",
    ):
        assert required in names


def test_a_wheel_rebuilt_from_the_sdist_keeps_the_bundled_resources(candidate, tmp_path):
    import tarfile
    import zipfile

    staging = tmp_path / "sdist"
    staging.mkdir()
    with tarfile.open(candidate["dist"] / "openthu-0.2.0.tar.gz") as archive:
        archive.extractall(staging, filter="data")
    source = staging / "openthu-0.2.0"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(tmp_path / "out")],
        cwd=source,
        capture_output=True,
        text=True,
        env={**os.environ, "SOURCE_DATE_EPOCH": "1", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    wheel = next((tmp_path / "out").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "thu_assistant/_bundled/layout.json" in names
    assert "thu_assistant/_bundled/vendor/madmodel-proxy/vendor/deepseek-tokenizer.json" in names
    assert "thu_assistant/package-lock.json" in names


def test_archive_bytes_come_from_commits_not_from_the_worktree(candidate, tmp_path):
    module = candidate["module"]
    target = ROOT / "README.md"
    original = target.read_bytes()
    before = module.committed_sources()
    try:
        target.write_bytes(original.replace(b"\n", b"\r\n"))
        os.utime(target, (1, 1))
        after = module.committed_sources()
    finally:
        target.write_bytes(original)
    assert before == after
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    module.write_source_zip(first, "prefix", before)
    module.write_source_zip(second, "prefix", after)
    assert first.read_bytes() == second.read_bytes()
    import zipfile

    with zipfile.ZipFile(first) as archive:
        assert archive.read("prefix/README.md") == original


def test_build_refuses_a_dirty_tracked_tree(candidate, tmp_path):
    module = candidate["module"]
    target = ROOT / "README.md"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\nuncommitted line\n")
        with pytest.raises(module.ReleaseError) as caught:
            module.build_candidate(tmp_path / "out")
        assert caught.value.code == "RELEASE_TREE_DIRTY"
    finally:
        target.write_bytes(original)


def test_verify_refuses_a_tampered_artifact(candidate, tmp_path):
    module = candidate["module"]
    dist = tmp_path / "dist"
    shutil.copytree(candidate["dist"], dist)
    wheel = next(dist.glob("*.whl"))
    data = bytearray(wheel.read_bytes())
    data[-1] ^= 0xFF
    wheel.write_bytes(bytes(data))
    with pytest.raises(module.ReleaseError) as caught:
        module.verify(dist)
    assert caught.value.code == "RELEASE_CHECKSUM_MISMATCH"


def test_release_manifest_and_package_metadata_agree_after_reinstall(candidate):
    wheel = next(candidate["dist"].glob("*.whl"))
    import zipfile

    with zipfile.ZipFile(wheel) as archive:
        layout = json.loads(archive.read("thu_assistant/_bundled/layout.json"))
    release = candidate["release"]
    assert layout["product_version"] == release["version"]
    assert layout["commit"] == release["commit"]
    for relative in manifest()["resource_map"].values():
        assert "thu_assistant/_bundled/" + relative


@pytest.fixture(scope="module")
def installed_wheel(candidate):
    wheel = next(candidate["dist"].glob("*.whl"))
    return candidate["module"].install_check(wheel)


def test_release_manifest_lists_each_runtime_resource_with_its_hash(candidate):
    release = candidate["release"]
    ids = {row["id"] for row in release["resources"]}
    assert ids == set(manifest()["resource_map"])
    assert release["bundled_files"] > len(ids)
    import zipfile

    wheel = next(candidate["dist"].glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for row in release["resources"]:
            import hashlib

            assert hashlib.sha256(archive.read(row["install"])).hexdigest() == row["sha256"]
            assert row["bytes"] > 0


def test_wheel_installs_outside_the_repository_without_source_fallback(installed_wheel):
    report = installed_wheel
    assert report["version"] == declared_version()
    assert report["source_root"] is None
    assert report["missing"] == []
    assert report["locations"]["source.windows_auth"] == "bundled"
    assert report["locations"]["source.madmodel_proxy"] == "bundled"
    assert report["locations"]["package.gateway"] == "package"
    assert report["mcp_tools"] == 29
    assert "site-packages" in report["file"] or "dist-packages" in report["file"]


# --- diagnostics name the missing capability -------------------------------


def diagnostics_module():
    return load_script("dev_diagnostics")


def diagnostics_case(tmp_path, **overrides):
    import test_dev_diagnostics as helpers

    module = diagnostics_module()
    root = helpers.complete_checkout(tmp_path)
    kwargs = helpers.healthy_kwargs(root)
    kwargs.update(overrides)
    return module, root, kwargs


def test_missing_node_names_the_capabilities_it_blocks(tmp_path):
    module, root, kwargs = diagnostics_case(tmp_path, platform_name="nt")
    kwargs["which"] = lambda name: None if name in ("node", "npm", "npm.cmd") else "/synthetic/" + name
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["node"]["status"] == "fail"
    assert "校园网关" in rows["node"]["reason"]
    assert rows["windows-auth"]["status"] == "fail"
    assert "Node.js 不可用" in rows["windows-auth"]["reason"]
    assert report["capabilities"]["development"] == "unavailable"
    assert report["capabilities"]["production-auth"] == "unavailable"
    assert report["capabilities"]["readonly"] == "available"
    text = module.format_report(report)
    assert "能力状态" in text


def test_missing_chromium_is_reported_as_a_browser_gap_only(tmp_path):
    module, root, kwargs = diagnostics_case(
        tmp_path, browser_available=lambda: {"available": False, "reason": "binary-missing"}
    )
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["playwright-browser"]["status"] == "warning"
    assert "Chromium 可执行文件" in rows["playwright-browser"]["reason"]
    assert report["ok"] is True
    assert report["capabilities"]["browser"] == "degraded"
    assert report["capabilities"]["readonly"] == "available"


def test_missing_playwright_client_is_distinguished_from_missing_browser(tmp_path):
    module, root, kwargs = diagnostics_case(
        tmp_path, browser_available=lambda: {"available": False, "reason": "client-missing"}
    )
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert "Playwright 客户端" in rows["playwright-browser"]["reason"]


def test_release_layout_mismatch_blocks_start(tmp_path):
    module, root, kwargs = diagnostics_case(tmp_path)
    package = root / "src" / "thu_assistant"
    (package / "_bundled").mkdir()
    (package / "_bundled" / "layout.json").write_text(
        json.dumps({"product_version": "0.1.9", "resources": {}}), encoding="utf-8"
    )
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["release-layout"]["status"] == "fail"
    assert rows["release-layout"]["blocking"] is True
    assert "0.1.9" in rows["release-layout"]["reason"]
    assert report["ok"] is False


def test_a_consistent_installed_layout_is_reported_as_complete(tmp_path):
    import test_dev_diagnostics as helpers

    module = diagnostics_module()
    package = tmp_path / "site-packages" / "thu_assistant"
    package.mkdir(parents=True)
    for name in ("gateway.js", "services.json", "security.js", "cookies.js"):
        (package / name).write_text("", encoding="utf-8")
    (package / "package-lock.json").write_text(
        json.dumps({"packages": {"node_modules/tough-cookie": {"version": "6.0.0"}}}),
        encoding="utf-8",
    )
    installed = package / "node_modules" / "tough-cookie"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version": "6.0.0"}', encoding="utf-8")
    bundled = package / "_bundled"
    (bundled / "runtime" / "windows").mkdir(parents=True)
    (bundled / "runtime" / "windows" / "access.js").write_text("", encoding="utf-8")
    (bundled / "vendor" / "madmodel-proxy").mkdir(parents=True)
    (bundled / "vendor" / "madmodel-proxy" / "package.json").write_text("{}\n", encoding="utf-8")
    (bundled / "layout.json").write_text(
        json.dumps(
            {
                "product_version": declared_version(),
                "resources": {
                    "source.windows_auth": "runtime/windows/access.js",
                    "source.madmodel_proxy": "vendor/madmodel-proxy/package.json",
                },
            }
        ),
        encoding="utf-8",
    )
    kwargs = helpers.healthy_kwargs(package)
    kwargs["root"] = package
    kwargs["config"] = helpers.synthetic_config(package)
    report = module.collect(**kwargs)
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["release-layout"]["status"] == "pass"
    assert rows["source-package"]["status"] == "pass"
    assert "不依赖源码目录" in rows["source-package"]["reason"]
    assert rows["windows-auth"]["status"] == "not_applicable"


