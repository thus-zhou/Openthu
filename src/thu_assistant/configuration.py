"""Layered, non-secret configuration and path selection for Openthu.

Precedence is explicit command-line values, then project-specific environment
variables, then a local JSON file, then safe defaults.  All layers are
validated before use; an invalid lower layer is not hidden by a higher one.

Secrets are deliberately outside this object.  They are read one at a time
from ``OPENTHU_SECRET_<NAME>`` by :meth:`Configuration.read_secret` and never
enter ``public_dict()``, the config file, or diagnostic output.  That avoids
silently combining a half-configured secret from two sources.

Error messages contain a stable code and a relative configuration item name;
they never echo a value or an absolute path.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import paths

CONFIG_VERSION = 1
ENV_PREFIX = "OPENTHU_"

_GROUP_NAMES = ("resources", "storage", "runtime", "network", "ui")
_FIELDS = {
    "resources.root": "path",
    "storage.state_dir": "path",
    "storage.download_dir": "path",
    "storage.temp_dir": "path",
    "storage.log_dir": "path",
    "runtime.python": "command",
    "runtime.node": "command",
    "runtime.command_timeout_seconds": "int",
    "runtime.max_output_bytes": "int",
    "network.npm_registry": "url",
    "network.listen_host": "loopback",
    "network.listen_port": "port",
    "network.request_timeout_seconds": "int",
    "ui.page_size": "int",
    "ui.refresh_seconds": "int",
}
_INT_RANGES = {
    "runtime.command_timeout_seconds": (1, 600),
    "runtime.max_output_bytes": (1024, 1024 * 1024),
    "network.listen_port": (1, 65535),
    "network.request_timeout_seconds": (1, 600),
    "ui.page_size": (1, 200),
    "ui.refresh_seconds": (10, 3600),
}
_SECRET_HINTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "private_key",
)
_SECRET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_STORAGE_FIELDS = (
    "storage.state_dir",
    "storage.download_dir",
    "storage.temp_dir",
    "storage.log_dir",
)
_REQUIRED_PACKAGE_FILES = tuple(
    spec.relative_path
    for spec in paths.RESOURCE_SPECS
    if spec.scope == "package" and spec.required and spec.kind == "file"
)

_ENV_FIELDS = {
    "OPENTHU_RESOURCE_ROOT": "resources.root",
    "OPENTHU_STATE_DIR": "storage.state_dir",
    "OPENTHU_DOWNLOAD_DIR": "storage.download_dir",
    "OPENTHU_TEMP_DIR": "storage.temp_dir",
    "OPENTHU_LOG_DIR": "storage.log_dir",
    "OPENTHU_PYTHON": "runtime.python",
    "OPENTHU_NODE": "runtime.node",
    "OPENTHU_COMMAND_TIMEOUT": "runtime.command_timeout_seconds",
    "OPENTHU_MAX_OUTPUT_BYTES": "runtime.max_output_bytes",
    "OPENTHU_NPM_REGISTRY": "network.npm_registry",
    "OPENTHU_LISTEN_HOST": "network.listen_host",
    "OPENTHU_LISTEN_PORT": "network.listen_port",
    "OPENTHU_REQUEST_TIMEOUT": "network.request_timeout_seconds",
    "OPENTHU_PAGE_SIZE": "ui.page_size",
    "OPENTHU_REFRESH_SECONDS": "ui.refresh_seconds",
}
# Old callers used these names.  They remain lower-priority aliases for the
# same field; a canonical OPENTHU_* value wins when both are present.
_LEGACY_ENV_FIELDS = {
    "THU_ASSISTANT_STATE_DIR": "storage.state_dir",
    "THU_PYTHON": "runtime.python",
    "THU_NODE": "runtime.node",
}


class ConfigurationError(ValueError):
    """A stable, public-safe configuration failure."""

    def __init__(self, code: str, item: str):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}")

    def public_dict(self) -> dict[str, str]:
        return {"code": self.code, "item": self.item}


@dataclass(frozen=True)
class Resources:
    root: Path
    source_root: Path | None


@dataclass(frozen=True)
class Storage:
    state_dir: Path
    download_dir: Path
    temp_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class Runtime:
    python: str
    node: str
    command_timeout_seconds: int
    max_output_bytes: int


@dataclass(frozen=True)
class Network:
    npm_registry: str
    listen_host: str
    listen_port: int
    request_timeout_seconds: int


@dataclass(frozen=True)
class Ui:
    page_size: int
    refresh_seconds: int


@dataclass(frozen=True)
class Configuration:
    resources: Resources
    storage: Storage
    runtime: Runtime
    network: Network
    ui: Ui
    sources: dict[str, str]
    config_file_used: bool

    def public_dict(self) -> dict[str, object]:
        """A serializable summary with no paths, commands or secrets."""
        return {
            "groups": list(_GROUP_NAMES),
            "config_file": "loaded" if self.config_file_used else "defaults",
            "sources": dict(self.sources),
            "resources": {
                "layout": "source" if self.resources.source_root else "package",
            },
            "storage": {
                "state_dir": self.sources["storage.state_dir"],
                "download_dir": self.sources["storage.download_dir"],
                "temp_dir": self.sources["storage.temp_dir"],
                "log_dir": self.sources["storage.log_dir"],
            },
            "runtime": {
                "command_timeout_seconds": self.runtime.command_timeout_seconds,
                "max_output_bytes": self.runtime.max_output_bytes,
            },
            "network": {
                "npm_registry": self.network.npm_registry,
                "listen_host": self.network.listen_host,
                "listen_port": self.network.listen_port,
                "request_timeout_seconds": self.network.request_timeout_seconds,
            },
            "ui": {
                "page_size": self.ui.page_size,
                "refresh_seconds": self.ui.refresh_seconds,
            },
            "secrets": "environment-only",
        }

    def read_secret(self, name: str, *, env: dict[str, str] | None = None) -> str | None:
        """Read one explicitly named secret from its single environment slot.

        This method is intentionally not part of ``public_dict``.  It returns
        ``None`` when unset and never falls back to a config file, another
        environment name or an old account directory.
        """
        if not _SECRET_NAME.fullmatch(name):
            raise ValueError("invalid secret name")
        values = os.environ if env is None else env
        return values.get(ENV_PREFIX + "SECRET_" + name.upper()) or None


class _Object(dict):
    """A JSON object that remembers duplicate keys for a precise error path."""

    def __init__(self, pairs):
        super().__init__()
        self.duplicates = []
        for key, value in pairs:
            if key in self:
                self.duplicates.append(key)
            else:
                self[key] = value


def _pairs(pairs):
    return _Object(pairs)


def _find_duplicate(value, path="config"):
    if isinstance(value, _Object):
        if value.duplicates:
            return path + "." + str(value.duplicates[0])
        for key, child in value.items():
            found = _find_duplicate(child, path + "." + str(key))
            if found:
                return found
    elif isinstance(value, dict):
        for key, child in value.items():
            found = _find_duplicate(child, path + "." + str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_duplicate(child, path + "[" + str(index) + "]")
            if found:
                return found
    return None


def _secret_like(key: str) -> bool:
    lowered = key.casefold()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _require_string(value, item: str, *, command: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("CONFIG_INVALID_TYPE", item)
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ConfigurationError("CONFIG_INVALID_TYPE", item)
    text = value.strip()
    if command and not text:
        raise ConfigurationError("CONFIG_INVALID_TYPE", item)
    return text


def _validate_value(item: str, value, *, home: Path | str | None = None):
    kind = _FIELDS[item]
    if kind == "path":
        if not isinstance(value, (str, Path)):
            raise ConfigurationError("CONFIG_INVALID_TYPE", item)
        try:
            return paths.normalize_path(value, item=item, home=home)
        except paths.PathError as error:
            raise ConfigurationError(error.code, item) from None
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError("CONFIG_INVALID_TYPE", item)
        low, high = _INT_RANGES[item]
        if not low <= value <= high:
            raise ConfigurationError("CONFIG_OUT_OF_RANGE", item)
        return value
    if kind == "command":
        return _require_string(value, item, command=True)
    if kind == "url":
        text = _require_string(value, item)
        try:
            parsed = urlsplit(text)
        except ValueError:
            raise ConfigurationError("CONFIG_INVALID_URL", item) from None
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigurationError("CONFIG_INVALID_URL", item)
        if parsed.username or parsed.password:
            raise ConfigurationError("CONFIG_SECRET_IN_URL", item)
        if any(character.isspace() for character in text):
            raise ConfigurationError("CONFIG_INVALID_URL", item)
        return text
    if kind == "loopback":
        text = _require_string(value, item)
        if text not in ("127.0.0.1", "::1", "localhost"):
            raise ConfigurationError("CONFIG_NOT_LOOPBACK", item)
        return text
    if kind == "port":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError("CONFIG_INVALID_TYPE", item)
        low, high = _INT_RANGES[item]
        if not low <= value <= high:
            raise ConfigurationError("CONFIG_OUT_OF_RANGE", item)
        return value
    raise ConfigurationError("CONFIG_INVALID_TYPE", item)


def _config_layer(data: dict, source: str, *, home=None) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ConfigurationError("CONFIG_INVALID_TYPE", "config")
    values: dict[str, object] = {}
    for group, mapping in data.items():
        group_item = "config." + str(group)
        if _secret_like(str(group)):
            raise ConfigurationError("CONFIG_SECRET_NOT_ALLOWED", group_item)
        if group not in _GROUP_NAMES:
            raise ConfigurationError("CONFIG_UNKNOWN_KEY", group_item)
        if not isinstance(mapping, dict):
            raise ConfigurationError("CONFIG_INVALID_TYPE", group)
        for key, value in mapping.items():
            item = f"{group}.{key}"
            if _secret_like(str(key)):
                raise ConfigurationError("CONFIG_SECRET_NOT_ALLOWED", item)
            if item not in _FIELDS:
                raise ConfigurationError("CONFIG_UNKNOWN_KEY", item)
            values[item] = _validate_value(item, value, home=home)
    return values


def _coerce_layer_value(item: str, value):
    """Coerce textual command-line/env integers without accepting garbage."""
    if _FIELDS[item] in ("int", "port") and isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d+", text):
            raise ConfigurationError("CONFIG_INVALID_TYPE", item)
        return int(text)
    return value


def _env_layer(env: dict[str, str], *, home=None) -> dict[str, object]:
    values: dict[str, object] = {}
    for env_name, item in _ENV_FIELDS.items():
        if env_name in env:
            values[item] = _validate_value(
                item, _coerce_layer_value(item, env[env_name]), home=home
            )
    for env_name, item in _LEGACY_ENV_FIELDS.items():
        if item not in values and env_name in env:
            values[item] = _validate_value(
                item, _coerce_layer_value(item, env[env_name]), home=home
            )
    return values


def _cli_layer(cli: dict[str, object] | None, *, home=None) -> dict[str, object]:
    values: dict[str, object] = {}
    for item, value in (cli or {}).items():
        if _secret_like(item):
            raise ConfigurationError("CONFIG_SECRET_NOT_ALLOWED", item)
        if item not in _FIELDS:
            raise ConfigurationError("CONFIG_UNKNOWN_KEY", item)
        values[item] = _validate_value(
            item, _coerce_layer_value(item, value), home=home
        )
    return values


def _read_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ConfigurationError("CONFIG_FILE_UNREADABLE", "config.file") from None
    try:
        data = json.loads(text, object_pairs_hook=_pairs)
    except (ValueError, TypeError):
        raise ConfigurationError("CONFIG_INVALID_JSON", "config") from None
    duplicate = _find_duplicate(data)
    if duplicate:
        raise ConfigurationError("CONFIG_DUPLICATE_KEY", duplicate)
    if not isinstance(data, dict):
        raise ConfigurationError("CONFIG_INVALID_TYPE", "config")
    return data


def _config_file(
    config_file: str | Path | None,
    env: dict[str, str],
    platform_name: str,
) -> tuple[Path, bool]:
    explicit = config_file is not None or "OPENTHU_CONFIG_FILE" in env
    raw = config_file if config_file is not None else env.get("OPENTHU_CONFIG_FILE")
    if raw is not None:
        try:
            return (
                paths.normalize_path(
                    raw,
                    item="config.file",
                    base=Path.cwd(),
                    must_be_absolute=False,
                    home=env.get("HOME") or env.get("USERPROFILE"),
                ),
                True,
            )
        except paths.PathError as error:
            raise ConfigurationError(error.code, "config.file") from None
    return paths.default_paths(env=env, platform_name=platform_name).config_file, False


def _prepare_directory(
    value,
    *,
    item: str,
    package_root: Path,
    source_root: Path | None,
    state_dir: Path | None = None,
) -> Path:
    try:
        candidate = (
            value
            if isinstance(value, Path)
            else paths.normalize_path(value, item=item)
        )
        return paths.ensure_private_directory(
            candidate,
            item=item,
            package_root_value=package_root,
            source_root_value=source_root,
            state_dir=state_dir,
            include_key_dirs=state_dir is not None,
        )
    except paths.PathError as error:
        raise ConfigurationError(error.code, item) from None


def _resource_root(value) -> Path:
    root = Path(value)
    if (
        not root.is_dir()
        or not os.access(root, os.R_OK)
        or any(not (root / name).is_file() for name in _REQUIRED_PACKAGE_FILES)
    ):
        raise ConfigurationError("CONFIG_RESOURCE_ROOT_INVALID", "resources.root")
    return root


def _validate_layer_values(values: dict[str, object], *, home=None) -> None:
    """Validate semantic path rules per layer, not only after merging.

    Syntax is already checked by ``_config_layer``/environment/CLI parsing.
    This catches an invalid lower-layer state path even when a higher layer
    overrides the final value, matching the documented layering contract.
    """
    actual_package = paths.package_root()
    actual_source = paths.source_root()
    if "resources.root" in values:
        _resource_root(values["resources.root"])
    state = values.get("storage.state_dir")
    for item in _STORAGE_FIELDS:
        if item not in values:
            continue
        state_for_item = None if item == "storage.state_dir" else state
        try:
            paths.ensure_private_directory(
                values[item],
                item=item,
                package_root_value=actual_package,
                source_root_value=actual_source,
                state_dir=state_for_item,
                include_key_dirs=state_for_item is not None,
            )
        except paths.PathError as error:
            raise ConfigurationError(error.code, item) from None


def load_config(
    *,
    cli: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    config_file: str | Path | None = None,
    platform_name: str | None = None,
) -> Configuration:
    """Load and validate the layered configuration.

    ``env`` is injectable for tests; production passes the process environment
    by omitting it.  An explicitly named config file must be readable even if
    it is empty of overrides; a missing default file simply yields defaults.
    """
    env = dict(os.environ if env is None else env)
    platform_name = platform_name or os.name
    defaults = paths.default_paths(env=env, platform_name=platform_name)
    selected_file, explicit_file = _config_file(config_file, env, platform_name)
    home = env.get("HOME") or env.get("USERPROFILE")
    file_values: dict[str, object] = {}
    file_used = False
    if selected_file.exists():
        file_values = _config_layer(_read_file(selected_file), "file", home=home)
        file_used = True
    elif explicit_file:
        raise ConfigurationError("CONFIG_FILE_UNREADABLE", "config.file")

    env_values = _env_layer(env, home=home)
    cli_values = _cli_layer(cli, home=home)
    layers = (("file", file_values), ("env", env_values), ("cli", cli_values))
    for _, layer in layers:
        _validate_layer_values(layer, home=home)
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for name, layer in layers:
        for item, value in layer.items():
            values[item] = value
            sources[item] = name

    resources_root = _resource_root(values.get("resources.root", paths.package_root()))
    detected_source = paths.detect_source_root(resources_root)

    state_dir = values.get("storage.state_dir", defaults.state_dir)
    state_dir = _prepare_directory(
        state_dir,
        item="storage.state_dir",
        package_root=resources_root,
        source_root=detected_source,
    )
    download_dir = values.get("storage.download_dir", state_dir / "downloads")
    download_dir = _prepare_directory(
        download_dir,
        item="storage.download_dir",
        package_root=resources_root,
        source_root=detected_source,
        state_dir=state_dir,
    )
    temp_dir = values.get("storage.temp_dir", defaults.temp_dir)
    temp_dir = _prepare_directory(
        temp_dir,
        item="storage.temp_dir",
        package_root=resources_root,
        source_root=detected_source,
        state_dir=state_dir,
    )
    log_dir = values.get("storage.log_dir", state_dir / "logs")
    log_dir = _prepare_directory(
        log_dir,
        item="storage.log_dir",
        package_root=resources_root,
        source_root=detected_source,
        state_dir=state_dir,
    )

    for item in _FIELDS:
        sources.setdefault(item, "default")

    runtime = Runtime(
        python=values.get("runtime.python", sys.executable),
        node=values.get("runtime.node", "node"),
        command_timeout_seconds=values.get("runtime.command_timeout_seconds", 300),
        max_output_bytes=values.get("runtime.max_output_bytes", 65536),
    )
    network = Network(
        npm_registry=values.get(
            "network.npm_registry", "https://registry.npmjs.org"
        ),
        listen_host=values.get("network.listen_host", "127.0.0.1"),
        listen_port=values.get("network.listen_port", 8765),
        request_timeout_seconds=values.get("network.request_timeout_seconds", 240),
    )
    ui = Ui(
        page_size=values.get("ui.page_size", 50),
        refresh_seconds=values.get("ui.refresh_seconds", 30),
    )
    return Configuration(
        resources=Resources(root=resources_root, source_root=detected_source),
        storage=Storage(
            state_dir=state_dir,
            download_dir=download_dir,
            temp_dir=temp_dir,
            log_dir=log_dir,
        ),
        runtime=runtime,
        network=network,
        ui=ui,
        sources=sources,
        config_file_used=file_used,
    )
