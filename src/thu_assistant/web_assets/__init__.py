"""Static local dashboard assets (module 7.1).

The package contains no Python behaviour beyond locating its own directory:
``static_dir()`` is the one path the local API server needs in order to serve
``index.html`` and ``assets/*``.  Keeping the path here, instead of inventing a
second lookup rule in the CLI, means an installed wheel and a source checkout
resolve the same folder.

The assets are read-only: HTML, CSS and JavaScript with no build step, no
network fetch to any third party and no generated binary.  ``index.html`` is
served only from ``/`` and every other file from ``/assets/<name>`` by the
6.2 server; see ``tests/test_web_assets.py`` for the static and browser checks.
"""
from __future__ import annotations

from pathlib import Path

VERSION = 'dashboard-v1'
ASSET_DIR_NAME = 'assets'


def static_dir() -> Path:
    """Return the dashboard's static root (the folder holding ``index.html``)."""
    return Path(__file__).resolve().parent


def index_file() -> Path:
    """Return the dashboard entry file without opening it."""
    return static_dir() / 'index.html'


__all__ = ['VERSION', 'ASSET_DIR_NAME', 'static_dir', 'index_file']
