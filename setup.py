"""Build entry point: attach the declared runtime resources to the wheel.

``pyproject.toml`` keeps the declarative metadata.  This file exists only for
one reason: the complete layout needs files that live outside the Python
package (the Windows authentication helper and the pinned upstream proxy), and
copying them by hand into the package tree would create a second, drifting
copy.  The build therefore materialises them from
``build_support/distribution.json`` at build time, for a wheel built either
from a checkout or from the generated sdist.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = Path(__file__).resolve().parent
_SUPPORT = ROOT / "build_support"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))

import materialize  # noqa: E402  (local build helper, not a distribution)


def _declared_version() -> str:
    """Read the single version source without importing the whole package."""
    module_path = ROOT / "src" / "thu_assistant" / "version.py"
    spec = importlib.util.spec_from_file_location("_openthu_version", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.__version__


def _build_commit() -> str | None:
    """The commit being built, or ``None`` outside a Git checkout.

    An explicit ``OPENTHU_BUILD_COMMIT`` wins so a release build can pin the
    commit even when the archive is produced from an exported tree.
    """
    pinned = os.environ.get("OPENTHU_BUILD_COMMIT", "").strip()
    if pinned:
        return pinned
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


class BuildPyWithResources(_build_py):
    """Copy the declared bundles next to the Python package."""

    def run(self):  # noqa: D102 - setuptools command entry point
        super().run()
        manifest = materialize.load_manifest(ROOT)
        summary = materialize.materialize(
            ROOT,
            Path(self.build_lib) / "thu_assistant",
            manifest,
            version=_declared_version(),
            commit=_build_commit(),
        )
        self.announce(
            f"openthu: bundled {summary['files']} runtime resource files",
            level=2,
        )


setup(cmdclass={"build_py": BuildPyWithResources})
