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

from typing import Any

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


def serve(*, host: str = contract.DEFAULT_BIND, port: int = 0, facade: Any = None,
          static_dir: Any = None, state_dir: Any = None) -> dict[str, Any]:
    """Start the local dashboard API and block until it is stopped.

    ``openthu dashboard`` builds :class:`LocalApiServer` itself so it can print
    the listening URL before blocking; this helper stays the one-call form for
    embedders that do not need that line.  ``localhost`` is mapped to the only
    loopback form the bind allowlist accepts; no other alias is invented.
    """
    bind = '127.0.0.1' if host == 'localhost' else host
    server = LocalApiServer(facade, bind=bind, port=port, static_dir=static_dir,
                            state_dir=state_dir).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return {'status': 'STOPPED', 'url': server.base_url, 'port': server.port,
            'instance_id': server.instance_id}

__all__ = [
    'LocalApiServer',
    'LocalApiClient',
    'serve',
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
