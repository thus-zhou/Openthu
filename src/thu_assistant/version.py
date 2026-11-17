"""Single product version source.

The Python package metadata, the MCP ``serverInfo`` banner, the CLI, the
release manifest and the candidate notes all read this module, so a version
bump cannot leave one of them behind.  Storage and contract formats are
versioned separately: a product version bump never implies that stored bytes
or frozen contracts changed shape.
"""
from __future__ import annotations

#: The one product version.  Bump this and nothing else for a release.
__version__ = "0.2.0"

#: Version of the on-disk secure storage header (see contracts/v0.2).
STATE_FORMAT_VERSION = "secure-storage-header-v1"

#: Version of the frozen public contracts under ``contracts/v0.2``.
CONTRACT_VERSION = "v0.2"

#: Version of the installed resource layout descriptor written by the build.
LAYOUT_SCHEMA_VERSION = "openthu-bundled-layout-v1"


def product_version() -> str:
    """Return the product version string (``major.minor.patch``)."""
    return __version__
