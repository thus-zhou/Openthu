"""Course resource centre: encrypted index, cross-course search and safe download.

New in v0.2 module 4.2.  The package is intentionally not wired into
``queries.py`` or the MCP tool list; module 6.1 owns the public facade.  See
``README.md`` for the boundary and the frozen shapes.
"""
from .api import FACADE_VERSION, ResourceCenter
from .errors import ERROR_CLASSES, ResourceError
from .identity import SOURCE_KINDS, archive_relative_path, stable_uid
from .limits import DEFAULT_LIMITS, ResourceLimits
from .manifest import MANIFEST_VERSION, build_manifest
from .model import DownloadRecord, ResourceCandidate, ResourceEntry, empty_index

__all__ = [
    "DEFAULT_LIMITS",
    "DownloadRecord",
    "ERROR_CLASSES",
    "FACADE_VERSION",
    "MANIFEST_VERSION",
    "ResourceCandidate",
    "ResourceCenter",
    "ResourceEntry",
    "ResourceError",
    "ResourceLimits",
    "SOURCE_KINDS",
    "archive_relative_path",
    "build_manifest",
    "empty_index",
    "stable_uid",
]
