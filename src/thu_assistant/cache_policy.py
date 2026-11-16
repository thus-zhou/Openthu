"""Per-resource cache policy for v0.2 read results.

The policy is intentionally small and explicit:

* real-time capacity (seat/room/sports availability) is never served from a
  cache and a cached row can never prove booking eligibility;
* static directory content may be served from a short-lived cache, with the
  original check time and cache state visible in the result;
* business history may fall back only when the caller explicitly allows stale
  data, and the fallback result is marked HISTORICAL/PARTIAL.

Every cache record is bound to the account scope, parameters, source and
policy version.  A record from another binding is rejected with a stable
reason instead of being silently reused.  The module performs no I/O.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

VERSION = 'cache-v1'
TZ = timezone(timedelta(hours=8))

POLICIES = {
    'availability': {
        'label': 'LIVE_ONLY',
        'ttl_seconds': 0,
        'history_fallback': False,
        'verification_level': 'NONE',
        'description': 'Real-time capacity must be read on every query.',
    },
    'directory': {
        'label': 'SHORT_CACHE',
        'ttl_seconds': 3600,
        'history_fallback': False,
        'verification_level': 'DIRECTORY_CACHE',
        'description': 'Static service directory may be served for up to one hour.',
    },
    'history': {
        'label': 'HISTORY_FALLBACK_ALLOWED',
        'ttl_seconds': 7 * 86400,
        'history_fallback': True,
        'verification_level': 'PAGE_READ',
        'description': 'Business history may be shown as historical only when explicitly allowed.',
    },
}

# Resource kinds are stable names, not operation names, so adapters can share
# one policy row without registering a new cache format.
RESOURCE_KINDS = {
    'library.seat_availability': 'availability',
    'library.room_availability': 'availability',
    'sports.availability': 'availability',
    'info.directory': 'directory',
    'learn.courses': 'directory',
    'library.loans': 'history',
    'library.reservations': 'history',
    'sports.orders': 'history',
    'learn.assignments': 'history',
}


def policy(resource_kind):
    """Return a policy dict for a resource kind or registered resource id."""
    kind = RESOURCE_KINDS.get(resource_kind, resource_kind)
    row = POLICIES.get(kind)
    return {'kind': kind, **row} if row else None


def binding(*, account, parameters, source, policy_version=VERSION):
    """A deterministic binding digest for one cache record."""
    payload = {'policy_version': policy_version, 'account_scope': str(account or ''),
               'parameters': parameters or {}, 'source': str(source or '')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def record(*, policy_version=VERSION, account, parameters, source, checked_at, value):
    """Build the metadata wrapper a cache backend stores next to its payload."""
    return {'policy_version': policy_version, 'account_scope': str(account or ''),
            'parameters_sha256': binding(account=account, parameters=parameters,
                                         source=source, policy_version=policy_version),
            'source': str(source or ''), 'checked_at': checked_at, 'value': value}


def _reject(kind, state, reason):
    return {'allowed': False, 'policy': kind, 'cache_state': state,
            'freshness': 'LIVE_REQUIRED' if state == 'LIVE_ONLY_RESOURCE' else 'REJECTED',
            'warnings': [reason], 'proves_authentication': False,
            'reusable_for_booking': False, 'capability_evidence': False,
            'complete': False, 'source_complete': False, 'checked_at': None,
            'age_seconds': None, 'value': None}


def evaluate(entry, *, resource_kind, account, parameters, source, now=None,
             allow_stale=False, current_policy_version=VERSION):
    """Decide whether a cache entry may be used for this request.

    The returned dict always states what the cache cannot prove.  Callers must
    keep ``freshness``/``cache_state`` in the result and must not upgrade
    ``verification_level`` to a live level based on cached content.
    """
    rule = policy(resource_kind)
    if rule is None:
        return _reject(str(resource_kind), 'NO_POLICY', 'CACHE_POLICY_UNREGISTERED')
    kind = rule['kind']
    if rule['label'] == 'LIVE_ONLY':
        return _reject(kind, 'LIVE_ONLY_RESOURCE', 'CACHE_NOT_ALLOWED_FOR_LIVE_RESOURCE')
    if not isinstance(entry, dict):
        return _reject(kind, 'MISSING', 'CACHE_MISSING')
    if entry.get('policy_version') != current_policy_version:
        return _reject(kind, 'POLICY_VERSION_MISMATCH', 'CACHE_POLICY_VERSION_MISMATCH')
    expected = binding(account=account, parameters=parameters, source=source,
                       policy_version=current_policy_version)
    if entry.get('parameters_sha256') != expected:
        return _reject(kind, 'BOUND_MISMATCH', 'CACHE_BINDING_MISMATCH')
    checked = _parse_time(entry.get('checked_at'))
    if checked is None:
        return _reject(kind, 'INVALID_CHECKED_AT', 'CACHE_CHECKED_AT_UNVERIFIED')
    current = now or datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    age = (current.astimezone(TZ) - checked).total_seconds()
    if age < 0:
        return _reject(kind, 'CLOCK_SKEW', 'CACHE_CHECKED_AT_IN_FUTURE')
    stale = age > rule['ttl_seconds']
    if stale and not (rule['history_fallback'] and allow_stale):
        return {**_reject(kind, 'STALE', 'CACHE_STALE'), 'age_seconds': int(age)}
    if rule['history_fallback'] and allow_stale:
        freshness = 'HISTORICAL'
        warnings = ['HISTORICAL_FALLBACK_EXPLICIT', 'CACHE_NOT_LIVE_EVIDENCE']
        complete = False
        source_complete = False
    else:
        freshness = 'CACHED'
        warnings = ['CACHE_NOT_LIVE_EVIDENCE']
        complete = bool(entry.get('source_complete', False))
        source_complete = complete
    return {'allowed': True, 'policy': kind, 'cache_state': 'FRESH' if not stale else 'STALE',
            'freshness': freshness, 'warnings': warnings,
            'proves_authentication': False, 'reusable_for_booking': False,
            'capability_evidence': False, 'complete': complete,
            'source_complete': source_complete, 'checked_at': checked.isoformat(timespec='seconds'),
            'age_seconds': int(age), 'verification_level': rule['verification_level'],
            'value': entry.get('value')}


def describe():
    return {'version': VERSION,
            'policies': {name: {key: value for key, value in rule.items() if key != 'description'}
                         for name, rule in POLICIES.items()},
            'rules': ['availability is never cached',
                      'directory cache is bound to source, parameters and policy version',
                      'history fallback requires allow_stale=true',
                      'cached content never proves authentication or booking eligibility']}
