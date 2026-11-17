"""Local read-only dashboard API (module 6.2).

Public surface for 6.1 (facade wiring), 7.1 (dashboard) and the delivery
record:

* :class:`LocalApiServer` - loopback HTTP server, one socket per instance;
* :class:`LocalApiClient` - protected-file bootstrap plus GET;
* :class:`BusinessFacade` - the frozen consumer protocol 6.1 implements;
* :func:`describe_api` - routes, limits, display states and synthetic samples;
* :data:`LIMITS` - the engineering bounds mirrored from ``local-read-api-v1``.

Nothing here reads a campus source, starts a browser or writes business state.
"""
from __future__ import annotations

from .client import LocalApiClient
from .contract import (
    CONTRACT_VERSION,
    FACADE_CONTRACT_VERSION,
    LIMITS,
    LocalApiError,
    LocalApiLimits,
    route_table,
)
from .facade import BusinessFacade, CancelToken, UnavailableFacade
from .paging import PageStore
from .projection import project
from .samples import describe_api
from .security import BOOTSTRAP_SCHEMA, COOKIE_NAME, read_bootstrap
from .server import LocalApiServer

__all__ = [
    'LocalApiServer',
    'LocalApiClient',
    'BusinessFacade',
    'CancelToken',
    'UnavailableFacade',
    'LocalApiError',
    'LocalApiLimits',
    'PageStore',
    'CONTRACT_VERSION',
    'FACADE_CONTRACT_VERSION',
    'LIMITS',
    'BOOTSTRAP_SCHEMA',
    'COOKIE_NAME',
    'read_bootstrap',
    'describe_api',
    'project',
    'route_table',
]
