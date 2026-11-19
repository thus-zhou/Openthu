"""Local media assets for Openthu v0.2 (module 3.3).

The package owns one boundary for local images and attachments:

* ``MediaAssets.register_*`` validates a file, decides its type from content,
  sanitises images and stores only derived bytes in a controlled temporary area;
* ``MediaAssets.resolve`` is scoped to one account, one task authorization and
  one expiry instant;
* ``MediaAssets.prepare_model_bundle`` produces an inspectable preview and a
  single-use grant before any bytes may leave the process.

No network client is imported anywhere in this package. The pipeline is
deliberately separate from transport; 4.5 and 6.1 connect the model and resource
features to this boundary instead of opening another local read path.
"""
from __future__ import annotations

from .api import MediaAssets, MediaAudit, build
from .boundary import ensure_within, open_verified, sanitize_display_name
from .errors import ERROR_CLASSES, MediaError
from .images import ImageResult, sanitize as sanitize_image
from .limits import DEFAULT_LIMITS, MediaLimits
from .model_input import (
    BundleItem,
    ModelAssetBundle,
    ModelInputBroker,
    OutboundImage,
    TransferGrant,
    capability_report,
)
from .registry import (
    PURPOSES,
    RETENTIONS,
    SOURCE_KINDS,
    MediaReference,
    MediaStateStore,
    ReferenceRegistry,
)
from .tempstore import CleanupReport, TempArea

__all__ = [
    "BundleItem",
    "CleanupReport",
    "DEFAULT_LIMITS",
    "ERROR_CLASSES",
    "ImageResult",
    "ModelAssetBundle",
    "MediaAssets",
    "MediaAudit",
    "MediaError",
    "MediaLimits",
    "MediaReference",
    "MediaStateStore",
    "ModelInputBroker",
    "OutboundImage",
    "PURPOSES",
    "RETENTIONS",
    "ReferenceRegistry",
    "SOURCE_KINDS",
    "TempArea",
    "TransferGrant",
    "build",
    "capability_report",
    "ensure_within",
    "open_verified",
    "sanitize_display_name",
    "sanitize_image",
]
