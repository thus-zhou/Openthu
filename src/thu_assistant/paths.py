"""Unified path and resource resolution for Openthu.

The resolver never searches the machine for an old installation.  A logical
resource is looked up at one documented location; if it is absent the caller
gets an unavailable resource and can report a precise, public-safe reason.

Nothing in this module creates directories, reads credentials or writes state.
Callers decide when a directory is needed.  Public manifests contain relative
paths only so they can be attached to an issue without leaking a home
directory or host name.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "TsinghuaAssistant"
PACKAGE_ROOT = Path(__file__).resolve().parent
_PROJECT_CANDIDATE = PACKAGE_ROOT.parents[1]

_AUTO = object()


class PathError(ValueError):
    """A path or resource selection that cannot be used safely."""

    def __init__(self, code: str, item: str):
        self.code = code
        self.item = item
        super().__init__(f"{code}: {item}")


@dataclass(frozen=True)
class DefaultPaths:
    config_file: Path
    state_dir: Path
    download_dir: Path
    temp_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class ResourceSpec:
    id: str
    relative_path: str
    scope: str  # "package" or "source"
    required: bool
    capability: str
    kind: str = "file"


@dataclass(frozen=True)
class ResolvedResource:
    id: str
    scope: str
    relative_path: str
    capability: str
    required: bool
    path: Path | None

    @property
    def available(self) -> bool:
        return self.path is not None


# A complete logical inventory.  The same id has the same meaning whether the
# bytes come from a source checkout or an installed wheel; only availability
# changes.
RESOURCE_SPECS = (
    ResourceSpec("package.gateway", "gateway.js", "package", True, "readonly"),
    ResourceSpec("package.services", "services.json", "package", True, "readonly"),
    ResourceSpec("package.security", "security.js", "package", True, "readonly"),
    ResourceSpec("package.cookies", "cookies.js", "package", True, "readonly"),
    ResourceSpec("package.models", "models.js", "package", False, "model"),
    ResourceSpec("package.package_lock", "package-lock.json", "package", True, "development"),
    ResourceSpec("source.windows_auth", "runtime/windows/access.js", "source", True, "production-auth"),
    ResourceSpec("source.madmodel_proxy", "vendor/madmodel-proxy/package.json", "source", True, "production-auth"),
    ResourceSpec("source.dev_command", "scripts/dev.py", "source", False, "development"),
    ResourceSpec("source.tests", "tests", "source", False, "development", "dir"),
)


def package_root() -> Path:
    """The installed/resource package directory (never a guessed path)."""
    return PACKAGE_ROOT


def source_root() -> Path | None:
    """The checkout root when this package is running from ``src/``."""
    candidate = _PROJECT_CANDIDATE
    if (
        (candidate / "pyproject.toml").is_file()
        and (candidate / "src" / "thu_assistant").resolve() == PACKAGE_ROOT
    ):
        return candidate
    return None


def detect_source_root(package_root_value: Path | str | None = None) -> Path | None:
    """Return a checkout root for a package directory, or ``None``.

    This is a layout check, not a filesystem search: it only inspects the
    parent chain of the supplied package directory.
    """
    package = Path(package_root_value).resolve() if package_root_value else PACKAGE_ROOT
    if package.name != "thu_assistant":
        return None
    candidate = package.parents[1]
    if (candidate / "pyproject.toml").is_file() and (
        candidate / "src" / "thu_assistant"
    ).resolve() == package:
        return candidate
    return None


def default_paths(
    *, env: dict[str, str] | None = None, platform_name: str | None = None
) -> DefaultPaths:
    """Platform defaults, preserving the old Windows state directory."""
    env = os.environ if env is None else env
    platform_name = platform_name or os.name
    if platform_name == "nt":
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        local = Path(env.get("LOCALAPPDATA") or env.get("APPDATA") or home / "AppData" / "Local")
        config_base = Path(env.get("APPDATA") or local)
        state = local / APP_NAME
        temp = Path(env.get("TEMP") or tempfile.gettempdir()) / "Openthu"
        return DefaultPaths(
            config_file=config_base / "Openthu" / "config.json",
            state_dir=state,
            download_dir=state / "downloads",
            temp_dir=temp,
            log_dir=state / "logs",
        )

    home = Path(env.get("HOME") or Path.home())
    if platform_name == "darwin":
        config_base = home / "Library" / "Application Support"
        state_base = config_base
        app_name = "openthu"
    else:
        config_base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        state_base = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
        app_name = "openthu"
    state = state_base / app_name
    return DefaultPaths(
        config_file=config_base / "openthu" / "config.json",
        state_dir=state,
        download_dir=state / "downloads",
        temp_dir=Path(env.get("TMPDIR") or tempfile.gettempdir()) / "openthu",
        log_dir=state / "logs",
    )


def normalize_path(
    value: str | Path,
    *,
    item: str,
    base: Path | str | None = None,
    must_be_absolute: bool = True,
    home: Path | str | None = None,
) -> Path:
    """Expand, make absolute and resolve a user-selected directory.

    Relative storage paths are rejected by callers that require absolute
    locations; an explicit relative config-file path can pass a ``base``.
    ``home`` lets a caller validate against an injected environment instead
    of the process account.  Error messages contain only the logical item.
    """
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise PathError("PATH_INVALID", item)
    text = str(value).strip()
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise PathError("PATH_INVALID", item)
    try:
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            home_value = Path(home) if home is not None else Path.home()
            expanded = str(home_value if text == "~" else home_value / text[2:])
        else:
            expanded = os.path.expanduser(text)
    except (RuntimeError, KeyError):
        raise PathError("PATH_HOME_UNRESOLVED", item) from None
    expanded = os.path.expandvars(expanded)
    if "$" in expanded:
        raise PathError("PATH_ENV_UNRESOLVED", item)
    path = Path(expanded)
    if base is not None and not path.is_absolute():
        path = Path(base) / path
    if must_be_absolute and not path.is_absolute():
        raise PathError("PATH_NOT_ABSOLUTE", item)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise PathError("PATH_UNRESOLVED", item) from None
    if resolved == Path(resolved.anchor):
        raise PathError("PATH_ROOT", item)
    return resolved


def _norm(value: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def is_within(child: Path | str, parent: Path | str) -> bool:
    child_text = _norm(child)
    parent_text = _norm(parent).rstrip(os.sep)
    return child_text == parent_text or child_text.startswith(parent_text + os.sep)


def program_roots(
    *, package_root_value: Path | str | None = None, source_root_value: Path | str | None = None
) -> list[Path]:
    roots: list[Path] = []
    package = Path(package_root_value).resolve() if package_root_value else PACKAGE_ROOT
    roots.append(package)
    source = (
        Path(source_root_value).resolve()
        if source_root_value is not None
        else source_root()
    )
    if source is not None:
        roots.append(source)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def key_roots(state_dir: Path | str) -> list[Path]:
    state = Path(state_dir)
    return [state / name for name in ("keys", "keyrings", "secrets", "credentials")]


def legacy_state_dir(
    *, env: dict[str, str] | None = None, platform_name: str | None = None
) -> Path:
    """The pre-v0.2 state location, returned without reading or moving it."""
    env = os.environ if env is None else env
    platform_name = platform_name or os.name
    if platform_name == "nt":
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        local = Path(env.get("LOCALAPPDATA") or env.get("APPDATA") or home / "AppData" / "Local")
        return local / APP_NAME
    return Path(env.get("HOME") or Path.home()) / APP_NAME


def migration_preview(
    *, env: dict[str, str] | None = None, platform_name: str | None = None
) -> dict[str, object]:
    """Describe the legacy-to-default move without touching either tree."""
    defaults = default_paths(env=env, platform_name=platform_name)
    source = legacy_state_dir(env=env, platform_name=platform_name)
    return {
        "from": str(source),
        "to": str(defaults.state_dir),
        "action": "preview-only",
        "moved": False,
    }


def ensure_not_protected(
    path: Path | str, *, item: str, roots: list[Path]
) -> None:
    for root in roots:
        if is_within(path, root):
            raise PathError("PATH_PROTECTED", item)


def ensure_private_directory(
    path: Path | str,
    *,
    item: str,
    package_root_value: Path | str | None = None,
    source_root_value: Path | str | None = None,
    state_dir: Path | str | None = None,
    include_key_dirs: bool = False,
) -> Path:
    """Validate a private directory target without creating it."""
    candidate = Path(path)
    roots = program_roots(
        package_root_value=package_root_value,
        source_root_value=source_root_value,
    )
    if include_key_dirs and state_dir is not None:
        roots.extend(key_roots(state_dir))
    ensure_not_protected(candidate, item=item, roots=roots)
    if candidate.exists():
        if not candidate.is_dir():
            raise PathError("PATH_NOT_DIRECTORY", item)
        if not os.access(candidate, os.R_OK | os.W_OK):
            raise PathError("PATH_NOT_ACCESSIBLE", item)
    else:
        existing_parent = candidate.parent
        while not existing_parent.exists():
            if existing_parent.parent == existing_parent:
                existing_parent = None
                break
            existing_parent = existing_parent.parent
        if existing_parent is None or not os.access(
            existing_parent, os.W_OK | os.X_OK
        ):
            raise PathError("PATH_NOT_ACCESSIBLE", item)
    return candidate


def resolve_resource(
    resource_id: str,
    *,
    package_root_value: Path | str | None = None,
    source_root_value: Path | str | None = _AUTO,
) -> ResolvedResource:
    """Resolve one logical resource at its documented location.

    ``source_root_value=None`` explicitly means "no source checkout"; omitting
    it asks the package layout to detect one.  No directory outside the
    documented root is inspected.
    """
    spec = next((row for row in RESOURCE_SPECS if row.id == resource_id), None)
    if spec is None:
        raise PathError("RESOURCE_UNKNOWN", resource_id)
    if spec.scope == "package":
        root = Path(package_root_value).resolve() if package_root_value else PACKAGE_ROOT
    else:
        if source_root_value is _AUTO:
            root = source_root()
        else:
            root = Path(source_root_value).resolve() if source_root_value else None
    path = root / spec.relative_path if root is not None else None
    if path is None:
        available = False
    elif spec.kind == "dir":
        available = path.is_dir()
    else:
        available = path.is_file()
    return ResolvedResource(
        id=spec.id,
        scope=spec.scope,
        relative_path=spec.relative_path,
        capability=spec.capability,
        required=spec.required,
        path=path if available else None,
    )


def resource_manifest(
    *,
    package_root_value: Path | str | None = None,
    source_root_value: Path | str | None = _AUTO,
) -> list[dict[str, object]]:
    """Public manifest: logical ids, relative paths and availability only."""
    if source_root_value is _AUTO:
        detected = source_root()
    else:
        detected = Path(source_root_value).resolve() if source_root_value else None
    rows: list[dict[str, object]] = []
    for spec in RESOURCE_SPECS:
        resolved = resolve_resource(
            spec.id,
            package_root_value=package_root_value,
            source_root_value=detected,
        )
        rows.append(
            {
                "id": spec.id,
                "scope": spec.scope,
                "relative_path": spec.relative_path,
                "required": spec.required,
                "capability": spec.capability,
                "kind": spec.kind,
                "available": resolved.available,
            }
        )
    return rows
