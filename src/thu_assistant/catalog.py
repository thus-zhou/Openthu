"""Local service catalog: bundled static directory plus a bounded live cache.

Module 3.2 makes the cache rules explicit.  The bundled ``services.json`` is a
static resource.  The dynamic directory is a cached live read: it is bound to
the source and policy version, carries its original check time, and never
proves login or business permission.  A stale or unversioned cache is still
shown (so the user keeps the last known rows) but is reported as incomplete.
"""
from __future__ import annotations
import json
import os
import uuid
from pathlib import Path
from .bridge import CampusError, gateway
from . import cache_policy, paths, source_state


def state_root():
    """The one state directory used by stateful modules.

    Resolution order is explicit environment overrides, then the layered
    configuration file, then the documented platform default.  The legacy
    ``THU_ASSISTANT_STATE_DIR`` name stays a lower-priority alias, so an old
    invocation keeps working while the runtime and ``doctor``/migration see the
    same directory.  This used to be a second, independent implementation that
    ignored ``OPENTHU_STATE_DIR`` and the configuration file on Linux.
    """
    for name in ('OPENTHU_STATE_DIR', 'THU_ASSISTANT_STATE_DIR'):
        value = (os.environ.get(name) or '').strip()
        if value:
            return Path(value).expanduser()
    try:
        from .configuration import ConfigurationError, load_config
        return load_config().storage.state_dir
    except (ConfigurationError, OSError, ValueError):
        # A malformed or unreadable configuration must not change where state
        # is written; the platform default is the same default the configuration
        # layer would have used.
        return paths.default_paths().state_dir


def directory_path():
    return state_root() / 'directory.json'


def load_services():
    static = json.loads(Path(__file__).with_name('services.json').read_text(encoding='utf-8'))
    try:
        cached = json.loads(directory_path().read_text(encoding='utf-8'))
        dynamic = cached.get('services', []) if isinstance(cached, dict) else []
    except (OSError, ValueError):
        dynamic = []
    if not isinstance(dynamic, list):
        dynamic = []
    by_roaming = {row.get('roaming_id'): row for row in static if row.get('roaming_id')}
    remaining = []
    for row in dynamic:
        alias = by_roaming.get(row.get('roaming_id'))
        if alias:
            alias['portal_id'] = row['id']
            alias['description'] = row.get('description', '')
            alias['department'] = row.get('department', '')
        else:
            remaining.append(row)
    return static + remaining


def _cache_record(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _cache_verdict(state, reason):
    return {'allowed': False, 'cache_state': state, 'freshness': 'REJECTED',
            'warnings': [reason], 'proves_authentication': False,
            'reusable_for_booking': False, 'capability_evidence': False,
            'complete': False, 'source_complete': False, 'checked_at': None,
            'age_seconds': None, 'value': None}


def cache_status(record=None, now=None):
    """Evaluate the directory cache against the v0.2 policy."""
    invalid = False
    if record is None:
        cached = _cache_record(directory_path())
        if cached is None:
            record = {}
        elif isinstance(cached, dict):
            record = cached
        else:
            record = {}
            invalid = True
    elif not isinstance(record, dict):
        record = {}
        invalid = True
    if invalid:
        verdict = _cache_verdict('INVALID_FORMAT', 'CACHE_FORMAT_INVALID')
    elif not record:
        verdict = _cache_verdict('MISSING', 'CACHE_MISSING')
    else:
        verdict = cache_policy.evaluate(record, resource_kind='info.directory',
                                        account='PUBLIC', parameters={}, source='info.directory',
                                        now=now)
    state = verdict['cache_state']
    warnings = []
    if invalid:
        warnings.append('DIRECTORY_CACHE_FORMAT_INVALID')
    elif not record:
        warnings.append('LIVE_DIRECTORY_NOT_CHECKED')
    elif state == 'POLICY_VERSION_MISMATCH':
        warnings.append('CACHE_COMPLETENESS_NOT_REVALIDATED')
    elif state == 'STALE':
        warnings.append('DIRECTORY_CACHE_STALE')
    elif state == 'BOUND_MISMATCH':
        warnings.append('DIRECTORY_CACHE_BINDING_MISMATCH')
    elif state in ('INVALID_CHECKED_AT', 'CLOCK_SKEW'):
        warnings.append('DIRECTORY_CACHE_TIME_UNVERIFIED')
    if record and record.get('query_schema') != 1:
        warnings.append('CACHE_COMPLETENESS_NOT_REVALIDATED')
    return {'record': record, 'verdict': verdict, 'state': state,
            'warnings': sorted(set(warnings))}


def list_services(query: str | None = None, category: str | None = None):
    rows = load_services()
    if query:
        q = query.casefold()
        rows = [r for r in rows if q in ' '.join(str(r.get(k, '')) for k in ('id', 'name', 'description', 'category')).casefold()]
    if category:
        rows = [r for r in rows if category.casefold() in r.get('category', '').casefold()]
    status = cache_status()
    record, verdict = status['record'], status['verdict']
    checked_at = source_state.now_iso()
    complete = bool(verdict.get('allowed') and record.get('query_schema') == 1 and record.get('complete') is True)
    pages = record.get('pages') if isinstance(record.get('pages'), list) else []
    missing = record.get('missing_pages') if isinstance(record.get('missing_pages'), list) else []
    scope = source_state.read_scope('FULL_LIST', offset=0, limit=max(1, len(rows)),
                                    pages_read=len(pages),
                                    expected_pages=(len(pages) + len(missing)) if (pages or missing) else None,
                                    query_time=checked_at)
    warnings = status['warnings'] + list(verdict.get('warnings') or [])
    if state := status['state']:
        if complete:
            partial_reasons = []
        elif state == 'FRESH':
            partial_reasons = ['DIRECTORY_CACHE_INCOMPLETE']
        else:
            partial_reasons = ['DIRECTORY_CACHE_' + state]
    else:
        partial_reasons = ['DIRECTORY_CACHE_UNKNOWN']
    return {
        'services': rows, 'count': len(rows), 'status': 'LOCAL_CATALOG_READ',
        'source': {'kind': 'static_and_cached_directory',
                   'cached_at': record.get('checkedAt'),
                   'checked_at': verdict.get('checked_at'),
                   'cache_state': status['state'],
                   'freshness': verdict.get('freshness'),
                   'proves_authentication': False},
        'checked_at': checked_at, 'query_time': checked_at,
        'complete': complete,
        'source_complete': complete,
        'snapshot_complete': True,
        'next_cursor': None,
        'warnings': sorted(set(warnings)),
        'partial_reasons': partial_reasons,
        'verification_level': 'DIRECTORY_CACHE' if verdict.get('allowed') else 'NONE',
        'read_scope': scope,
        'upstream_total': record.get('expected') if type(record.get('expected')) is int else None,
        'account_scope': 'PUBLIC',
        'scope': 'any HTTPS tsinghua.edu.cn subdomain is also accepted by read_page',
        'coverage': 'Directory membership does not prove login, permission or booking capability.',
    }


def refresh_directory():
    result = gateway('discover')
    checked = source_state.normalize_timestamp(result.get('checkedAt'), source_state.now_iso())
    record = {**result,
              'query_schema': 1,
              'cache_version': 1,
              'policy_version': cache_policy.VERSION,
              'account_scope': 'PUBLIC',
              'parameters_sha256': cache_policy.binding(account='PUBLIC', parameters={},
                                                         source='info.directory', checked_at=checked),
              'source_id': 'info.directory',
              'checked_at': checked,
              'checkedAt': result.get('checkedAt') or checked}
    target = directory_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return {'status': result['status'], 'count': result['count'], 'expected': result['expected'],
            'rowCount': result.get('rowCount'), 'duplicateCount': result.get('duplicateCount'),
            'complete': result['complete'], 'source_complete': result['complete'],
            'snapshot_complete': True, 'checkedAt': record['checkedAt'], 'checked_at': checked,
            'query_time': checked, 'freshness': 'LIVE', 'cache_state': 'FRESH',
            'source': {'url': 'https://info.tsinghua.edu.cn/f/info/portal_fg/student/yyfwxxindex',
                       'checked_at': checked},
            'next_cursor': None, 'warnings': result.get('warnings', []),
            'missing_pages': result.get('missing_pages', []), 'page_errors': result.get('page_errors', []),
            'verification_level': 'DIRECTORY_READ'}


def resolve(service_id: str):
    found = next((row for row in load_services() if row['id'] == service_id or row.get('portal_id') == service_id), None)
    if not found:
        raise CampusError('SERVICE_NOT_FOUND_REFRESH_DIRECTORY_OR_USE_URL')
    return dict(found)


def request_arguments(service_id=None, url=None, via_webvpn=False, authenticate=True):
    if bool(service_id) == bool(url):
        raise CampusError('PROVIDE_ONE_SERVICE_ID_OR_URL')
    if url:
        return {'url': url, 'via_webvpn': via_webvpn, 'authenticate': authenticate}
    service = resolve(service_id)
    result = {'via_webvpn': via_webvpn or service.get('via_webvpn', False), 'authenticate': authenticate}
    if service.get('roaming_id'):
        result['roaming_id'] = service['roaming_id']
        if service.get('entry_url'):
            result['landing_url'] = service['entry_url']
    else:
        result['url'] = service['entry_url']
    return result
