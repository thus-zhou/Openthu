"""Unified configuration and path rules for the v0.2.0 base layer."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from thu_assistant import configuration, paths

ROOT = Path(__file__).resolve().parents[1]


def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "TMPDIR": str(tmp_path / "tmp"),
    }


def write_config(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_precedence_cli_env_file_default_and_derived_directories(tmp_path):
    env = isolated_env(tmp_path)
    config_path = write_config(
        tmp_path / "config.json",
        {
            "storage": {"state_dir": str(tmp_path / "file-state")},
            "ui": {"page_size": 20},
        },
    )
    env.update(
        {
            "OPENTHU_STATE_DIR": str(tmp_path / "env-state"),
            "OPENTHU_PAGE_SIZE": "30",
        }
    )
    config = configuration.load_config(
        cli={"storage.state_dir": str(tmp_path / "cli-state"), "ui.page_size": 40},
        env=env,
        config_file=config_path,
        platform_name="posix",
    )
    assert config.storage.state_dir == (tmp_path / "cli-state").resolve()
    assert config.ui.page_size == 40
    assert config.sources["storage.state_dir"] == "cli"
    assert config.sources["ui.page_size"] == "cli"
    assert config.storage.download_dir == (tmp_path / "cli-state" / "downloads").resolve()
    assert config.sources["storage.download_dir"] == "default"
    assert config.config_file_used is True


def test_defaults_are_used_without_a_config_file(tmp_path):
    env = isolated_env(tmp_path)
    config = configuration.load_config(env=env, platform_name="posix")
    assert config.config_file_used is False
    assert config.storage.state_dir == (tmp_path / "home" / ".state" / "openthu").resolve()
    assert config.network.listen_host == "127.0.0.1"
    assert config.network.listen_port == 8765
    assert config.ui.page_size == 50


def test_unknown_duplicate_invalid_and_unreadable_config_are_stable_errors(tmp_path):
    env = isolated_env(tmp_path)
    cases = [
        ({"storage": {"extra": 1}}, "CONFIG_UNKNOWN_KEY", "storage.extra"),
        ({"ui": {"page_size": "50"}}, "CONFIG_INVALID_TYPE", "ui.page_size"),
        ({"network": {"listen_port": 70000}}, "CONFIG_OUT_OF_RANGE", "network.listen_port"),
        ({"storage": {"state_dir": "relative/state"}}, "PATH_NOT_ABSOLUTE", "storage.state_dir"),
        ({"secrets": {"token": "do-not-echo"}}, "CONFIG_SECRET_NOT_ALLOWED", "config.secrets"),
    ]
    for index, (body, code, item) in enumerate(cases):
        path = write_config(tmp_path / f"case-{index}.json", body)
        with pytest.raises(configuration.ConfigurationError) as caught:
            configuration.load_config(env=env, config_file=path, platform_name="posix")
        assert caught.value.code == code
        assert caught.value.item == item
        assert "do-not-echo" not in str(caught.value)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"ui": {"page_size": 1, "page_size": 2}}', encoding="utf-8")
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(env=env, config_file=duplicate, platform_name="posix")
    assert caught.value.code == "CONFIG_DUPLICATE_KEY"
    assert caught.value.item == "config.ui.page_size"

    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(env=env, config_file=tmp_path, platform_name="posix")
    assert caught.value.code == "CONFIG_FILE_UNREADABLE"

    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            env=env, config_file=tmp_path / "missing.json", platform_name="posix"
        )
    assert caught.value.code == "CONFIG_FILE_UNREADABLE"


def test_state_and_download_cannot_overwrite_program_or_key_directories(tmp_path):
    env = isolated_env(tmp_path)
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={"storage.state_dir": str(ROOT / "synthetic-state")},
            env=env,
            platform_name="posix",
        )
    assert caught.value.code == "PATH_PROTECTED"
    assert caught.value.item == "storage.state_dir"

    state = tmp_path / "state"
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={
                "storage.state_dir": str(state),
                "storage.download_dir": str(state / "keys"),
            },
            env=env,
            platform_name="posix",
        )
    assert caught.value.code == "PATH_PROTECTED"
    assert caught.value.item == "storage.download_dir"


def test_path_expansion_and_legacy_environment_compatibility(tmp_path):
    env = isolated_env(tmp_path)
    config = configuration.load_config(
        cli={"storage.state_dir": "~/private-state"},
        env=env,
        platform_name="posix",
    )
    assert config.storage.state_dir == (tmp_path / "home" / "private-state").resolve()
    assert config.sources["storage.state_dir"] == "cli"

    env["THU_ASSISTANT_STATE_DIR"] = str(tmp_path / "legacy-state")
    legacy = configuration.load_config(env=env, platform_name="posix")
    assert legacy.storage.state_dir == (tmp_path / "legacy-state").resolve()
    assert legacy.sources["storage.state_dir"] == "env"

    env["OPENTHU_STATE_DIR"] = str(tmp_path / "canonical-state")
    canonical = configuration.load_config(env=env, platform_name="posix")
    assert canonical.storage.state_dir == (tmp_path / "canonical-state").resolve()
    assert canonical.sources["storage.state_dir"] == "env"


def test_public_summary_contains_no_absolute_path_command_or_secret(tmp_path):
    env = isolated_env(tmp_path)
    config = configuration.load_config(
        cli={"storage.state_dir": str(tmp_path / "private-state")},
        env={**env, "OPENTHU_SECRET_MODEL_TOKEN": "synthetic-secret"},
        platform_name="posix",
    )
    text = json.dumps(config.public_dict(), ensure_ascii=False)
    assert str(tmp_path) not in text
    assert str(Path.home()) not in text
    assert "synthetic-secret" not in text
    assert config.read_secret("model_token", env={"OPENTHU_SECRET_MODEL_TOKEN": "x"}) == "x"
    assert config.read_secret("model_token", env={}) is None
    assert config.public_dict()["secrets"] == "environment-only"


def test_network_values_reject_credentials_and_non_loopback_binding(tmp_path):
    env = isolated_env(tmp_path)
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={"network.npm_registry": "https://user:pass@example.invalid/simple"},
            env=env,
            platform_name="posix",
        )
    assert caught.value.code == "CONFIG_SECRET_IN_URL"
    assert "pass" not in str(caught.value)

    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={"network.npm_registry": "http://[::1"},
            env=env,
            platform_name="posix",
        )
    assert caught.value.code == "CONFIG_INVALID_URL"

    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={"network.listen_host": "0.0.0.0"},
            env=env,
            platform_name="posix",
        )
    assert caught.value.code == "CONFIG_NOT_LOOPBACK"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits; Windows ACLs are covered by secure_store"
)
def test_parent_directory_permission_is_checked(tmp_path):
    env = isolated_env(tmp_path)
    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        with pytest.raises(configuration.ConfigurationError) as caught:
            configuration.load_config(
                cli={"storage.state_dir": str(parent / "state")},
                env=env,
                platform_name="posix",
            )
        assert caught.value.code == "PATH_NOT_ACCESSIBLE"
    finally:
        parent.chmod(0o700)


def test_lower_layer_repository_path_is_not_hidden_by_cli(tmp_path):
    env = isolated_env(tmp_path)
    config_file = write_config(
        tmp_path / "lower.json",
        {"storage": {"state_dir": str(ROOT / "lower-state")}},
    )
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(
            cli={"storage.state_dir": str(tmp_path / "cli-state")},
            env=env,
            config_file=config_file,
            platform_name="posix",
        )
    assert caught.value.code == "PATH_PROTECTED"


def test_resource_manifest_requires_the_declared_file_type(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "gateway.js").mkdir()  # a directory must not satisfy a file resource
    (package / "services.json").write_text("[]", encoding="utf-8")
    resolved = paths.resolve_resource("package.gateway", package_root_value=package)
    assert resolved.available is False


def test_resource_root_requires_all_required_package_files(tmp_path):
    env = isolated_env(tmp_path)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "gateway.js").write_text("", encoding="utf-8")
    (package / "services.json").write_text("[]", encoding="utf-8")
    config_file = write_config(
        tmp_path / "root.json", {"resources": {"root": str(package)}}
    )
    with pytest.raises(configuration.ConfigurationError) as caught:
        configuration.load_config(env=env, config_file=config_file, platform_name="posix")
    assert caught.value.code == "CONFIG_RESOURCE_ROOT_INVALID"


def test_resource_manifest_has_identical_ids_for_source_and_package(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    for name in ("gateway.js", "services.json", "security.js", "cookies.js", "models.js"):
        (package / name).write_text("", encoding="utf-8")
    (package / "package-lock.json").write_text("{}", encoding="utf-8")

    source = tmp_path / "checkout"
    (source / "runtime" / "windows").mkdir(parents=True)
    (source / "runtime" / "windows" / "access.js").write_text("", encoding="utf-8")
    (source / "vendor" / "madmodel-proxy").mkdir(parents=True)
    (source / "vendor" / "madmodel-proxy" / "package.json").write_text(
        "{}", encoding="utf-8"
    )
    (source / "scripts").mkdir()
    (source / "scripts" / "dev.py").write_text("", encoding="utf-8")
    (source / "tests").mkdir()

    source_rows = paths.resource_manifest(
        package_root_value=package, source_root_value=source
    )
    package_rows = paths.resource_manifest(
        package_root_value=package, source_root_value=None
    )
    assert {row["id"] for row in source_rows} == {row["id"] for row in package_rows}
    assert all(row["available"] for row in source_rows)
    source_only = {
        row["id"]: row for row in package_rows if row["scope"] == "source"
    }
    assert source_only
    assert all(not row["available"] for row in source_only.values())
    assert all("path" not in row for row in source_rows)

    resolved = paths.resolve_resource(
        "package.gateway", package_root_value=package, source_root_value=source
    )
    assert resolved.path == (package / "gateway.js").resolve()
    with pytest.raises(paths.PathError) as caught:
        paths.resolve_resource("package.missing", package_root_value=package)
    assert caught.value.code == "RESOURCE_UNKNOWN"


def test_path_normalization_rejects_relative_and_root_targets(tmp_path):
    with pytest.raises(paths.PathError) as caught:
        paths.normalize_path("relative/path", item="storage.state_dir")
    assert caught.value.code == "PATH_NOT_ABSOLUTE"
    assert paths.normalize_path(str(tmp_path / "a" / ".." / "b"), item="x") == (
        tmp_path / "b"
    ).resolve()
    root_target = Path(tmp_path.anchor)
    with pytest.raises(paths.PathError) as caught:
        paths.normalize_path(str(root_target), item="storage.state_dir")
    assert caught.value.code == "PATH_ROOT"


def test_sample_config_loads_with_isolated_home(tmp_path):
    env = isolated_env(tmp_path)
    config = configuration.load_config(
        config_file=ROOT / "configs" / "openthu.example.json",
        env=env,
        platform_name="posix",
    )
    assert config.storage.state_dir == (tmp_path / "home" / "openthu-state").resolve()
    assert config.storage.download_dir == (
        tmp_path / "home" / "openthu-downloads"
    ).resolve()
    assert config.config_file_used is True
    assert config.public_dict()["config_file"] == "loaded"


def test_migration_preview_does_not_move_or_read_state(tmp_path):
    env = isolated_env(tmp_path)
    preview = paths.migration_preview(env=env, platform_name="posix")
    assert preview["action"] == "preview-only"
    assert preview["moved"] is False
    assert preview["from"] == str(tmp_path / "home" / "TsinghuaAssistant")
    assert preview["to"] == str(tmp_path / "home" / ".state" / "openthu")


def test_platform_defaults_keep_windows_state_name_and_use_no_fixed_home():
    env = {
        "USERPROFILE": r"D:\synthetic-profile",
        "LOCALAPPDATA": r"D:\synthetic-profile\AppData\Local",
        "APPDATA": r"D:\synthetic-profile\AppData\Roaming",
        "TEMP": r"D:\synthetic-profile\AppData\Local\Temp",
    }
    defaults = paths.default_paths(env=env, platform_name="nt")
    assert str(defaults.state_dir).endswith("TsinghuaAssistant")
    assert defaults.download_dir == defaults.state_dir / "downloads"
    assert defaults.config_file.name == "config.json"
    assert "synthetic" in str(defaults.state_dir)
