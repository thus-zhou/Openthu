"""Registered read operations and bounded, process-local result snapshots.

v0.2 additions (module 3.2):
  * every strict envelope carries the completeness triad, a query time and a
    declared read scope; a failed page is never turned into an empty result;
  * cursors carry a format version and are bound to parameters, account scope
    and snapshot version; expiry, mismatch and stale versions stay distinct;
  * page snapshots keep the original check time/query window across midnight;
  * error statuses carry the frozen error-classification object.

Legacy fields and legacy status strings remain unchanged for old clients.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import os
import re
import secrets
import time
from .bridge import CampusError
from . import catalog, cache_policy, precheck, source_state, local_ops

VERSION = 'query-v1'
CURSOR_VERSION = 'c1'
SNAPSHOT_VERSION = 'snapshot-v2'
BINDING_VERSION = 'binding-v2'
TEXT = {'type': 'string', 'maxLength': 2000}


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


PAGE = schema({'json_pointer': TEXT, 'authenticate': {'type': 'boolean'}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100000}})
REGISTRY = {
    ('*', 'page'): (PAGE, 'snapshot_text_or_list', 'PAGE_READ'),
    ('*', 'status'): (schema(), 'none', 'PAGE_READ'),
    ('campus', 'page'): (schema({**PAGE['properties'], 'url': TEXT, 'via_webvpn': {'type': 'boolean'}}, ('url',)), 'snapshot_text_or_list', 'PAGE_READ'),
    ('info', 'directory'): (schema({'query': TEXT, 'category': TEXT}), 'upstream_pages_then_snapshot', 'DIRECTORY_READ'),
    ('learn', 'courses'): (schema({'semester': {'type': 'string', 'pattern': r'^[a-zA-Z0-9_-]{1,40}$'}, 'query': TEXT}), 'upstream_full_list_then_snapshot', 'BUSINESS_READ'),
}
ID = {'type': 'string', 'pattern': r'^[a-zA-Z0-9_-]{1,128}$'}
COURSE = {'course_id': ID, 'semester': ID, 'course_name': {'type': 'string', 'minLength': 1, 'maxLength': 240}}
for _operation, _schema in {
    'semesters': schema(), 'notices': schema(COURSE),
    'notice_detail': schema({**COURSE, 'notice_id': ID}, ('notice_id',)),
    'assignments': schema(COURSE),
    'assignment_detail': schema({**COURSE, 'assignment_id': ID}, ('assignment_id',)),
    'files': schema({**COURSE, 'query': TEXT}),
    'calendar': schema({'start_date': {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'},
                        'end_date': {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'}, 'graduate': {'type': 'boolean'}}, ('start_date', 'end_date')),
}.items():
    REGISTRY[('learn', _operation)] = (_schema, 'bounded_upstream_then_snapshot', 'BUSINESS_READ')

LIBRARY_RANGE = {'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
LIBRARY_TERM = {'type': 'string', 'minLength': 1, 'maxLength': 200}
LIBRARY_OPS = {
    'catalog_search': schema({'query': LIBRARY_TERM, 'field': {'type': 'string', 'pattern': 'keyword|title|author'},
                              'scope': {'type': 'string', 'pattern': 'all|print|online'}, **LIBRARY_RANGE}, ('query',)),
    'catalog_detail': schema({'record_id': {'type': 'string', 'pattern': r'[A-Za-z0-9_.:-]{1,250}'}}, ('record_id',)),
    'loans': schema(),
    'database_links': schema({'query': LIBRARY_TERM, 'database_id': {'type': 'string', 'pattern': r'[0-9]{1,30}'}, **LIBRARY_RANGE}),
}
for _operation, _schema in LIBRARY_OPS.items():
    REGISTRY[('library', _operation)] = (_schema, 'upstream_window_then_snapshot' if _operation in ('catalog_search', 'database_links') else 'snapshot_list', 'BUSINESS_READ')

SPACE_ID = {'type': 'string', 'pattern': r'[1-9][0-9]{0,15}'}
SPACE_DATE = {'type': 'string', 'pattern': r'\d{4}-\d{2}-\d{2}'}
SPACE_TIME = {'type': 'string', 'pattern': r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'}
SPACE_WINDOW = {'date': SPACE_DATE, 'start_time': SPACE_TIME, 'end_time': SPACE_TIME, 'name': LIBRARY_TERM}
SPACE_PAGE = {'page': {'type': 'integer', 'minimum': 1, 'maximum': 10000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}
SPACE_OPS = {
    'seat_areas': schema({'parent_id': SPACE_ID, 'query': LIBRARY_TERM}),
    'seat_availability': schema({'area_id': SPACE_ID, **SPACE_WINDOW}),
    'room_types': schema({'query': LIBRARY_TERM}),
    'room_availability': schema({'kind_id': SPACE_ID, **SPACE_WINDOW, **SPACE_PAGE, 'people': {'type': 'integer', 'minimum': 1, 'maximum': 500}}),
    'reservations': schema({'system': {'type': 'string', 'pattern': 'seat|room'}, **SPACE_PAGE, 'start_date': SPACE_DATE, 'end_date': SPACE_DATE,
                            'status_mask': {'type': 'integer', 'minimum': 1, 'maximum': 65535}}, ('system',)),
}
for _operation, _schema in SPACE_OPS.items():
    REGISTRY[('library', _operation)] = (_schema, 'upstream_window_then_snapshot' if _operation in ('room_availability', 'reservations') else 'snapshot_list', 'BUSINESS_READ')
from . import sports
SPORTS_OPS = {
    'scenes': schema({'query': LIBRARY_TERM}),
    'availability': schema({'scene_id': sports.ID, 'name': LIBRARY_TERM, 'room_id': sports.ID,
                            'date': sports.DAY, 'mode': {'type': 'string', 'pattern': 'SPORT_GROUP|SPORT_PERSON'}, **sports.WINDOW}),
    'orders': schema(sports.WINDOW),
    'order_detail': schema({'record_id': sports.ID}, ('record_id',)),
}
for _op, _schema in SPORTS_OPS.items():
    REGISTRY[('sports', _op)] = (_schema, 'upstream_window_then_snapshot', 'BUSINESS_READ')

WINDOW_OPS = {'catalog_search', 'database_links', 'seat_availability', 'room_availability', 'reservations', 'availability', 'orders'}
RECORD_OPS = {'catalog_detail', 'order_detail', 'notice_detail', 'assignment_detail'}

# Local aggregate reads (module 6.1).  They are not campus endpoints: agenda
# combines already-registered reads and resources reads a local encrypted
# index.  The schema table lives in ``local_ops`` so the facade description and
# this registry cannot drift apart.
LOCAL_OPS = local_ops.operations()
for _operation, _spec in LOCAL_OPS.items():
    REGISTRY[(local_ops.SERVICE_ID, _operation)] = (_spec['schema'], _spec['pagination'], _spec['level'])



def _provider_page_limit(provider, default=50, ceiling=200):
    """The provider's own search ceiling, clamped to a sane engineering bound."""
    limits = getattr(provider, 'limits', None)
    value = getattr(limits, 'max_search_limit', default)
    if type(value) is not int or value < 1:
        value = default
    return max(1, min(ceiling, value))


def now():
    return source_state.now_iso()


def validate(value, spec):
    kind = spec.get('type')
    valid = {'array': isinstance(value, list), 'object': isinstance(value, dict), 'string': isinstance(value, str), 'integer': type(value) is int, 'boolean': type(value) is bool}
    if kind not in valid or not valid[kind]:
        raise CampusError('INVALID_ARGUMENT_TYPE')
    if kind == 'array':
        if not spec.get('minItems', 0) <= len(value) <= spec.get('maxItems', 1000):
            raise CampusError('INVALID_ARGUMENT_LENGTH')
        for item in value:
            validate(item, spec['items'])
    if kind == 'object':
        if set(value) - set(spec.get('properties', {})):
            raise CampusError('INVALID_ARGUMENTS')
        if any(k not in value for k in spec.get('required', [])):
            raise CampusError('MISSING_ARGUMENT')
        for k, v in value.items():
            validate(v, spec['properties'][k])
    if kind == 'string':
        if not spec.get('minLength', 0) <= len(value) <= spec.get('maxLength', 1000000):
            raise CampusError('INVALID_ARGUMENT_LENGTH')
        if spec.get('pattern') and not re.fullmatch(spec['pattern'], value):
            raise CampusError('INVALID_ARGUMENT_VALUE')
    if kind == 'integer' and not spec.get('minimum', value) <= value <= spec.get('maximum', value):
        raise CampusError('INVALID_ARGUMENT_RANGE')


def _switch(name, env=None):
    if env is not None and name in env:
        return str(env[name])
    return os.environ.get(name, '1')


def describe(service_id, env=None):
    if service_id not in ('campus', local_ops.SERVICE_ID):
        catalog.resolve(service_id)
    operations = []
    local_names = tuple(LOCAL_OPS) if service_id == local_ops.SERVICE_ID else ()
    for operation in (*local_names, 'page', 'status', 'directory', 'courses', 'semesters', 'notices', 'notice_detail', 'assignments', 'assignment_detail', 'files', 'calendar', *LIBRARY_OPS, *SPACE_OPS, *SPORTS_OPS):
        if operation in SPORTS_OPS and _switch('THU_SPORTS_READ', env) == '0':
            continue
        if operation in (*LIBRARY_OPS, *SPACE_OPS) and _switch('THU_LIBRARY_READ', env) == '0':
            continue
        if operation in SPACE_OPS and _switch('THU_SPACE_READ', env) == '0':
            continue
        entry = REGISTRY.get((service_id, operation)) or (REGISTRY.get(('*', operation)) if service_id not in ('campus', local_ops.SERVICE_ID) else None)
        if entry:
            parameters, pagination, level = entry
            expanded = service_id == 'learn' and operation not in ('page', 'status', 'courses')
            operations.append({'operation': operation, 'parameters_schema': deepcopy(parameters), 'category': 'read', 'pagination': pagination,
                               'adapter_version': 'learn-read-v1' if expanded else VERSION, 'verification_level_ceiling': level,
                               'last_acceptance': {'level': 'LIVE_BUSINESS_READ' if expanded else {('info', 'directory'): 'LIVE_PARTIAL_DIRECTORY', ('learn', 'courses'): 'LIVE_BUSINESS_READ'}.get((service_id, operation), 'OFFLINE_CONTRACT'),
                                                   'date': '2026-10-31' if expanded else '2026-10-23', 'report': '06-验收.md' if expanded else '04-验收.md'},
                               'note': '页面或目录命中不证明业务权限；本次证据在查询结果中返回。'})
            if service_id == 'sports' and operation in SPORTS_OPS:
                operations[-1].update(adapter_version=sports.VERSION, last_acceptance={'report': '11-验收.md', 'level': 'SEE_OPERATION_EVIDENCE'}, note='余量按选定日期、模式和房间查询；下一来源页看 next_page。')
            if service_id == 'library' and operation in LIBRARY_OPS:
                operations[-1].update(adapter_version='library-read-v1', last_acceptance={'date': '2026-11-03', 'report': '07-验收.md', 'level': 'SEE_OPERATION_EVIDENCE'},
                                      note='搜索返回所选 offset/limit 区间，next_offset 用于后续来源区间。数据库导航链接仅证明入口已登记。')
            if service_id == 'library' and operation in SPACE_OPS:
                operations[-1].update(adapter_version='space-read-v1', last_acceptance={'date': '2026-11-05', 'report': '08-验收.md', 'level': 'SEE_OPERATION_EVIDENCE'},
                                      note='每次新查询实时读取；游标仅为同一检查时间的快照。next_page 表示下一来源页，complete 只覆盖当前来源页。空闲不代表具备预约资格。')
            if service_id == local_ops.SERVICE_ID and operation in LOCAL_OPS:
                operations[-1].update(adapter_version=LOCAL_OPS[operation]['adapter_version'],
                                      last_acceptance={'level': 'OFFLINE_CONTRACT', 'date': '2026-11-17',
                                                       'report': '模块结果/6.1.md'},
                                      note=LOCAL_OPS[operation]['note'], local_read=True)
    from .actions import describe_actions
    return {'service_id': service_id, 'operations': operations, 'actions': describe_actions(service_id, env=env),
            'registry_version': VERSION, 'cursor_version': CURSOR_VERSION,
            'snapshot_version': SNAPSHOT_VERSION,
            'completeness': {'fields': ['source_complete', 'snapshot_complete', 'complete'],
                             'rule': 'complete requires source_complete and snapshot_complete; a source failure is never EMPTY'},
            'cache_policy': cache_policy.describe(),
            'error_contract': 'error-classification-v1'}


def safe_url(url):
    return source_state.sanitize_url(url)


def pointer(data, path):
    if path == '':
        return data
    if not path.startswith('/') or re.search(r'~(?![01])', path):
        raise CampusError('INVALID_JSON_POINTER')
    node = data
    for part in path[1:].split('/'):
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) < len(node):
            node = node[int(key)]
        else:
            raise CampusError('JSON_POINTER_NOT_FOUND')
    return node


class Snapshots:
    def __init__(self, clock=time.monotonic, ttl=600, max_bytes=32 * 1024 * 1024, max_entries=64):
        self.clock = clock
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = {}
        self.cursors = {}

    def purge(self):
        expired = [k for k, v in self.entries.items() if self.clock() >= v['expires']]
        for k in expired:
            self.drop(k)

    def drop(self, key):
        self.entries.pop(key, None)
        self.cursors = {k: v for k, v in self.cursors.items() if v[0] != key}

    def start(self, binding, result, kind, value, size, start=0):
        self.purge()
        payload = deepcopy(value)
        metadata = deepcopy(result)
        cost = len(json.dumps([payload, metadata], ensure_ascii=False).encode())
        if cost > self.max_bytes:
            raise CampusError('RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE')
        while self.entries and (len(self.entries) >= self.max_entries or sum(v['bytes'] for v in self.entries.values()) + cost > self.max_bytes):
            self.drop(next(iter(self.entries)))
        sid = secrets.token_urlsafe(24)
        self.entries[sid] = {'binding': binding, 'result': metadata, 'kind': kind, 'value': payload,
                             'size': size, 'expires': self.clock() + self.ttl, 'bytes': cost,
                             'tokens': {}, 'version': SNAPSHOT_VERSION}
        return self.page(sid, start)

    def resume(self, cursor, binding):
        self.purge()
        if not isinstance(cursor, str) or not cursor or len(cursor) > 128:
            raise CampusError('CURSOR_EXPIRED')
        version, _, _rest = cursor.partition('.')
        if version != CURSOR_VERSION:
            # An old/external cursor format cannot be interpreted safely.
            raise CampusError('STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT')
        if cursor not in self.cursors:
            raise CampusError('CURSOR_EXPIRED')
        sid, index = self.cursors[cursor]
        entry = self.entries.get(sid)
        if entry is None:
            raise CampusError('CURSOR_EXPIRED')
        if entry.get('version') != SNAPSHOT_VERSION:
            raise CampusError('STALE_SNAPSHOT_TAKE_NEW_SNAPSHOT')
        if entry['binding'] != binding:
            raise CampusError('CURSOR_QUERY_MISMATCH')
        return self.page(sid, index)

    def page(self, sid, index):
        e = self.entries[sid]
        out = deepcopy(e['result'])
        if index > 0:
            if isinstance(out.get('retrieval'), dict):
                out['retrieval'] = {**out['retrieval'], 'freshness': 'SNAPSHOT_ONLY_NOT_LIVE',
                                    'current_source_read': False, 'reused_snapshot': True}
            if 'freshness' in out:
                out['freshness'] = 'SNAPSHOT_ONLY_NOT_LIVE'
        kind = e['kind']
        value = e['value']
        if kind in ('list', 'text'):
            end = min(index + e['size'], len(value))
            chunk = deepcopy(value[index:end])
            more = end < len(value)
            if more:
                if end not in e['tokens']:
                    token = CURSOR_VERSION + '.' + secrets.token_urlsafe(24)
                    e['tokens'][end] = token
                    self.cursors[token] = (sid, end)
                out['next_cursor'] = e['tokens'][end]
            else:
                out['next_cursor'] = None
            snapshot_complete = not more
            if 'partial_reasons' in out or 'query_time' in out:
                flags = source_state.completeness(out.get('status', 'UNKNOWN'), out.get('source_complete', False),
                                                  snapshot_complete, out.get('missing_pages', ()),
                                                  out.get('source_errors', ()), out.get('warnings', ()))
                out.update(source_complete=flags['source_complete'], snapshot_complete=flags['snapshot_complete'],
                           complete=flags['complete'], warnings=flags['warnings'],
                           partial_reasons=sorted(set(out.get('partial_reasons', [])) | set(flags['partial_reasons'])))
            else:
                out.update(snapshot_complete=snapshot_complete,
                           complete=snapshot_complete and out.get('source_complete') is True)
            if kind == 'list':
                out.update(items=chunk, data=chunk, total_items=len(value), returned_items=len(chunk), item_offset=index)
            else:
                out.update(text=chunk, data=chunk, totalCharacters=len(value), offset=index, nextOffset=end if more else None)
        else:
            if 'partial_reasons' in out or 'query_time' in out:
                flags = source_state.completeness(out.get('status', 'UNKNOWN'), out.get('source_complete', False),
                                                  True, out.get('missing_pages', ()),
                                                  out.get('source_errors', ()), out.get('warnings', ()))
                out.update(source_complete=flags['source_complete'], complete=flags['complete'],
                           warnings=flags['warnings'],
                           partial_reasons=sorted(set(out.get('partial_reasons', [])) | set(flags['partial_reasons'])))
            out.update(data=deepcopy(value), next_cursor=None, snapshot_complete=True)
        return out


def binding(service, operation, parameters, page_size, account_scope=None):
    payload = {'binding_version': BINDING_VERSION, 'service': service, 'operation': operation,
               'parameters': parameters, 'page_size': page_size}
    if account_scope is not None:
        payload['account_scope'] = str(account_scope)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def query_window(parameters):
    if not isinstance(parameters, dict):
        return None
    start = parameters.get('start_date') or parameters.get('date') or parameters.get('semester')
    end = parameters.get('end_date') or parameters.get('date') or parameters.get('semester')
    if not start and not end:
        return None
    return {'start': str(start) if start is not None else None,
            'end': str(end) if end is not None else None}


def _scope(kind, *, offset=0, limit=1, pages_read=1, expected_pages=None, query_time=None, parameters=None):
    return source_state.read_scope(kind, offset=max(0, int(offset or 0)), limit=max(1, int(limit or 1)),
                                   pages_read=max(0, int(pages_read or 0)),
                                   expected_pages=expected_pages,
                                   query_time=query_time, window=query_window(parameters))


def read_scope_for(service, operation, parameters, items, raw, query_time):
    """Declare what the adapter actually read for this operation."""
    count = len(items)
    if service == 'campus' or operation in ('page', 'status'):
        return _scope('PAGE', offset=int(parameters.get('offset', 0) or 0),
                      limit=int(parameters.get('limit', 24000) or 24000),
                      pages_read=1, expected_pages=None, query_time=query_time, parameters=parameters)
    if operation == 'directory':
        pages = raw.get('pages') or []
        missing = raw.get('missing_pages') or []
        expected = len(pages) + len(missing) if pages or missing else None
        return _scope('FULL_LIST', offset=0, limit=max(1, count),
                      pages_read=len(pages), expected_pages=expected, query_time=query_time, parameters=parameters)
    if operation in RECORD_OPS:
        if operation == 'order_detail' and isinstance(raw.get('collection'), dict):
            collection = raw['collection']
            total = collection.get('upstream_total')
            expected = ((total + 49) // 50) if type(total) is int and total >= 0 else None
            return _scope('FULL_LIST', offset=0, limit=max(1, total or count),
                          pages_read=collection.get('pages_read') or 1, expected_pages=expected,
                          query_time=query_time, parameters=parameters)
        return _scope('RECORD', offset=0, limit=1, pages_read=1, expected_pages=1,
                      query_time=query_time, parameters=parameters)
    pagination = raw.get('pagination') if isinstance(raw.get('pagination'), dict) else {}
    if operation in ('loans', 'reservations', 'orders') and pagination:
        page = pagination.get('page') or pagination.get('upstream_page') or parameters.get('page', 1)
        size = (pagination.get('page_size') or pagination.get('upstream_page_size')
                or parameters.get('limit') or max(1, count))
        total = pagination.get('upstream_total')
        pages_read = pagination.get('pages_read') or 1
        offset = max(0, (page - 1) * size) if type(page) is int and page >= 1 else 0
        expected = ((total + size - 1) // size) if type(total) is int and type(size) is int and size > 0 else None
        kind = 'FULL_LIST' if operation == 'loans' else 'WINDOW'
        return _scope(kind, offset=offset, limit=max(1, size),
                      pages_read=pages_read, expected_pages=expected,
                      query_time=query_time, parameters=parameters)
    if operation in WINDOW_OPS:
        page = parameters.get('page')
        page_size = raw.get('limit') if type(raw.get('limit')) is int else parameters.get('limit')
        offset = raw.get('offset') if type(raw.get('offset')) is int else parameters.get('offset')
        if offset is None and page_size is not None and type(page) is int and page >= 1:
            offset = (page - 1) * page_size
        if type(page_size) is not int or page_size < 1:
            page_size = max(1, count)
        if type(offset) is not int or offset < 0:
            offset = 0
        if operation == 'seat_availability':
            offset, page_size = 0, max(1, count)
        expected = 1 if raw.get('upstream_total') is not None or operation in ('seat_availability', 'room_availability', 'availability') else None
        return _scope('WINDOW', offset=offset, limit=page_size,
                      pages_read=1, expected_pages=expected, query_time=query_time, parameters=parameters)
    if service == 'learn':
        pages = raw.get('pages') or []
        return _scope('FULL_LIST', offset=0, limit=max(1, count),
                      pages_read=len(pages) if isinstance(pages, list) and pages else 1,
                      expected_pages=None, query_time=query_time, parameters=parameters)
    return _scope('FULL_LIST', offset=0, limit=max(1, count), pages_read=1,
                  expected_pages=1, query_time=query_time, parameters=parameters)


def metadata(service, operation, url='', level='NONE', status='UNKNOWN', complete=False,
             warnings=None, checked_at=None, *, source_complete=None, snapshot_complete=None,
             read_scope=None, query_time=None, missing_pages=None, source_errors=None,
             upstream_total=None, strict=False):
    """Build the legacy envelope, or the strict v0.2 envelope when requested."""
    checked = checked_at or now()
    if not strict:
        return {'status': status, 'source': {'service_id': service, 'operation': operation, 'url': safe_url(url), 'adapter_version': VERSION},
                'checked_at': checked, 'verification_level': level, 'source_complete': complete, 'complete': complete,
                'next_cursor': None, 'warnings': warnings or [], 'missing_pages': []}
    query_time = source_state.normalize_timestamp(query_time or checked, source_state.now_iso())
    checked = source_state.normalize_timestamp(checked, query_time)
    source_flag = complete if source_complete is None else source_complete
    flags = source_state.completeness(status, source_flag,
                                      True if snapshot_complete is None else snapshot_complete,
                                      missing_pages or [], source_errors or [], warnings or [])
    result = {'status': status, 'source': {'service_id': service, 'operation': operation,
                                           'url': safe_url(url), 'adapter_version': VERSION},
              'checked_at': checked, 'verification_level': level,
              'source_complete': flags['source_complete'], 'complete': flags['complete'],
              'next_cursor': None, 'warnings': flags['warnings'],
              'missing_pages': flags['missing_pages'], 'query_time': query_time,
              'partial_reasons': flags['partial_reasons'],
              'upstream_total': upstream_total if type(upstream_total) is int and upstream_total >= 0 else None,
              'source_state': source_state.evidence_state(status)}
    if snapshot_complete is not None:
        result['snapshot_complete'] = flags['snapshot_complete']
    if read_scope is not None:
        result['read_scope'] = read_scope
    if flags['source_errors']:
        result['source_errors'] = flags['source_errors']
    if status in source_state.ERROR_OBJECT_STATUSES:
        result['error'] = source_state.public_error(status)
    return result


def page_metadata(raw, service, operation):
    classified = source_state.classify_http(raw.get('httpStatus'), raw.get('authentication_state'),
                                            raw.get('status'), raw.get('maintenance'), raw.get('public_read'))
    status = classified['status']
    is_json = 'data' in raw
    warnings = list(classified['warnings'])
    if is_json:
        warnings.extend(['GENERIC_JSON_BUSINESS_STATUS_UNVERIFIED', 'UPSTREAM_COMPLETENESS_UNKNOWN'])
    source_complete = status in ('AUTHENTICATED', 'PUBLIC_READABLE', 'READABLE_AUTH_UNKNOWN') and not is_json
    scope = _scope('PAGE', offset=int(raw.get('offset', 0) or 0), limit=int(raw.get('limit', 24000) or 24000),
                   pages_read=1, expected_pages=None, query_time=raw.get('checked_at'))
    result = metadata(service, operation, raw.get('url'), classified['verification_level'], status,
                      complete=source_complete, warnings=warnings, checked_at=raw.get('checked_at'),
                      source_complete=source_complete, snapshot_complete=True,
                      read_scope=scope, query_time=raw.get('checked_at'), strict=True)
    result['source']['http_status'] = classified['http_status']
    result['source']['auth_state'] = classified['auth_state']
    if is_json:
        result['upstream_total'] = None
    return result


class QueryEngine:
    def __init__(self, owner):
        self.owner = owner
        self.snapshots = Snapshots()

    def _account_scope(self):
        """Resolve the account identity used to bind every account-scoped read.

        Fail closed: an unavailable gateway identity is not a shared
        ``UNAVAILABLE`` scope.  Returning that constant used to let two
        accounts share snapshots and cursors whenever the identity probe
        failed.  Public (non-account) reads never call this helper.
        """
        try:
            identity = self.owner.bridge('action_identity')
        except Exception:
            raise CampusError('AUTH_REQUIRED') from None
        if isinstance(identity, dict):
            account = identity.get('account_id')
            if type(account) is int:
                return str(account)
            if isinstance(account, str) and account:
                return account
        raise CampusError('AUTH_REQUIRED')

    def _finish(self, result, items, source_complete, scope, query_time,
                missing_pages=(), source_errors=()):
        status = result.get('status', 'UNKNOWN')
        flags = source_state.completeness(status, source_complete, True, missing_pages, source_errors)
        result.update(source_complete=flags['source_complete'], complete=flags['complete'],
                      warnings=sorted(set(result.get('warnings') or ()) | set(flags['warnings'])),
                      partial_reasons=flags['partial_reasons'], missing_pages=flags['missing_pages'],
                      query_time=query_time, read_scope=scope,
                      source_state=source_state.evidence_state(status))
        if flags['source_errors']:
            result['source_errors'] = flags['source_errors']
        if status in source_state.ERROR_OBJECT_STATUSES:
            result['error'] = source_state.public_error(status)
        if not items and result['source_complete'] and status == 'OK':
            if result.get('total_scope') == 'filtered_result':
                result['status'] = 'OK'
                result['matched_items'] = 0
            else:
                result['status'] = 'EMPTY'
                result['source_state'] = 'EMPTY'
        return result

    def query(self, service_id, operation, parameters, cursor=None, page_size=50):
        ops = {o['operation']: o for o in describe(service_id)['operations']}
        if operation not in ops:
            raise CampusError('UNREGISTERED_QUERY_OPERATION')
        validate(parameters, ops[operation]['parameters_schema'])
        validate(page_size, {'type': 'integer', 'minimum': 1, 'maximum': 200})
        if service_id == local_ops.SERVICE_ID:
            return self.local_query(operation, parameters, cursor=cursor, page_size=page_size)
        query_time = source_state.now_iso()
        account_bound = (service_id == 'library' and operation in ('loans', *SPACE_OPS)) or \
                        (service_id == 'sports' and operation in SPORTS_OPS)
        account_required = account_bound or service_id == 'learn' or operation in ('page', 'status')
        if operation in ('page', 'status') and parameters.get('authenticate') is False:
            account_required = False
        account_scope = self._account_scope() if account_required else None
        key = binding(service_id, operation, parameters, page_size, account_scope=account_scope)
        if cursor:
            return self.snapshots.resume(cursor, key)
        if operation in ('page', 'status'):
            args = {**parameters, **({'service_id': service_id} if service_id != 'campus' else {})}
            return self.read_page(args, page_size=page_size, bound=key, normalized=True, operation=operation)
        if (service_id == 'library' and operation in (*LIBRARY_OPS, *SPACE_OPS)) or (service_id == 'sports' and operation in SPORTS_OPS):
            if service_id == 'sports':
                library = sports
            elif operation in SPACE_OPS:
                from . import spaces as library
            else:
                from . import library
            if operation == 'database_links' and (('database_id' in parameters and len(parameters) > 1) or (set(parameters) & {'offset', 'limit'} and 'query' not in parameters)):
                raise CampusError('INVALID_LIBRARY_SELECTOR')
            raw = library.query(self.owner.browsers, operation, parameters)
            items = raw.pop('items')
            result = metadata(service_id, operation, raw.pop('url'), raw.pop('level', 'BUSINESS_READ'),
                              'OK' if raw['complete'] else 'PARTIAL', raw['complete'], raw.pop('warnings'),
                              strict=True, source_complete=raw['complete'], snapshot_complete=True,
                              query_time=query_time)
            result['source']['adapter_version'] = library.VERSION
            result.update(raw)
            result['retrieval'] = precheck.retrieval(service_id, operation,
                                                      checked_at=result['checked_at'],
                                                      parameters=parameters)
            built = precheck.build(service_id, operation,
                                   {**result, 'url': result['source'].get('url')},
                                   items, parameters, result['checked_at'])
            result['precheck'] = built['summary']
            scope = read_scope_for(service_id, operation, parameters, items, raw, query_time)
            result['upstream_total'] = raw.get('upstream_total') if type(raw.get('upstream_total')) is int else None
            if service_id == 'sports' and operation == 'scenes' and parameters.get('query'):
                result['total_scope'] = 'filtered_result'
            result = self._finish(result, items, raw.get('complete', False), scope, query_time,
                                  raw.get('missing_pages', ()), raw.get('source_errors', ()))
        elif operation == 'directory':
            raw = self.owner.bridge('discover')
            items = raw['services']
            for field in ('query', 'category'):
                if parameters.get(field):
                    items = [r for r in items if parameters[field].casefold() in (json.dumps(r, ensure_ascii=False) if field == 'query' else r.get('category', '')).casefold()]
            result = metadata(service_id, operation, 'https://info.tsinghua.edu.cn/f/info/portal_fg/student/yyfwxxindex', 'DIRECTORY_READ',
                              'OK' if raw['complete'] else 'PARTIAL', raw['complete'], raw.get('warnings', []), raw.get('checkedAt'),
                              strict=True, source_complete=raw['complete'], snapshot_complete=True,
                              query_time=query_time)
            result.update(upstream_total=raw['expected'], upstream_received=raw['count'], upstream_row_count=raw.get('rowCount'),
                          deduplicated_entries=raw.get('duplicateCount', 0), upstream_total_unit='directory_rows_before_deduplication',
                          upstream_pages=raw.get('pages'), missing_pages=raw.get('missing_pages', []), page_errors=raw.get('page_errors', []))
            result['total_scope'] = ('filtered_result' if parameters.get('query') or parameters.get('category')
                                     else 'directory_all_matches')
            scope = read_scope_for(service_id, operation, parameters, items, raw, query_time)
            result = self._finish(result, items, raw['complete'], scope, query_time,
                                  raw.get('missing_pages', ()), (raw.get('page_errors') or ()))
        elif service_id == 'learn' and operation != 'courses':
            raw = self.owner.learning_call('query_learning', {'operation': operation, 'parameters': parameters})
            items = raw['items']
            result = metadata('learn', operation, 'https://zhjw.cic.tsinghua.edu.cn/jxmh_out.do' if operation == 'calendar' else 'https://learn.tsinghua.edu.cn/',
                              'BUSINESS_READ', raw.get('status', 'OK' if raw['complete'] else 'PARTIAL'), raw['complete'],
                              raw.get('warnings', []), raw.get('checked_at'), strict=True,
                              source_complete=raw['complete'], snapshot_complete=True, query_time=query_time)
            result['source']['adapter_version'] = 'learn-read-v1'
            result.update(upstream_pages=raw.get('pages', []), source_errors=source_state.safe_errors(raw.get('errors', [])),
                          scope=raw.get('scope', 'selected Learn courses and operation'))
            for field in ('candidates', 'reason'):
                if field in raw:
                    result[field] = raw[field]
            scope = read_scope_for('learn', operation, parameters, items, raw, query_time)
            result = self._finish(result, items, raw['complete'], scope, query_time,
                                  (), result.get('source_errors', ()))
        else:
            raw = self.owner.learning_call('list_courses', parameters)
            items = raw['courses']
            complete = raw.get('complete', True) is not False
            warnings = raw.get('warnings') if isinstance(raw.get('warnings'), list) else []
            status = raw.get('status') if isinstance(raw.get('status'), str) and raw.get('status') else (
                'OK' if complete else 'PARTIAL')
            if not complete and status in ('OK', 'EMPTY'):
                status = 'PARTIAL'
            result = metadata('learn', 'courses', 'https://learn.tsinghua.edu.cn/b/wlxt/kc/v_wlkc_xs_xkb_kcb_extend/student/loadCourseBySemesterId/',
                              'BUSINESS_READ', status, complete, warnings, raw.get('checked_at'),
                              strict=True, source_complete=complete,
                              snapshot_complete=True, query_time=query_time)
            result['upstream_total'] = None if parameters.get('query') else len(items)
            result['total_scope'] = 'filtered_result' if parameters.get('query') else 'semester_courses'
            scope = read_scope_for('learn', 'courses', parameters, items, raw, query_time)
            result = self._finish(result, items, complete, scope, query_time)
        return self.snapshots.start(key, result, 'list', items, page_size)

    def local_query(self, operation, parameters, cursor=None, page_size=50):
        """Dispatch one local aggregate read without touching a campus adapter.

        ``agenda``/``agenda_changes`` read through the 5.4 todo/event engine;
        ``resources`` reads the 4.2 local encrypted index through an injected
        provider.  A paginated snapshot is built once per read so a cursor never
        mixes two different reads.
        """
        query_time = source_state.now_iso()
        local = LOCAL_OPS[operation]
        account_scope = self._account_scope()
        key = binding(local_ops.SERVICE_ID, operation, parameters, page_size,
                      account_scope=account_scope)
        if cursor:
            return self._local_page(self.snapshots.resume(cursor, key), operation)
        source = {'service_id': local_ops.SERVICE_ID, 'operation': operation,
                  'url': None, 'adapter_version': local['adapter_version']}
        if operation in ('agenda', 'agenda_changes'):
            todos = getattr(self.owner, 'todos', None)
            if todos is None:
                raise CampusError('AGENDA_PROVIDER_UNAVAILABLE')
            if operation == 'agenda':
                built = todos.agenda(**parameters)
                rows = list(built.pop('calendar_events', None) or [])
                built.pop('items', None)
            else:
                args = {name: value for name, value in parameters.items()
                        if name != 'change_cursor'}
                built = todos.changes(cursor=parameters.get('change_cursor'), **args)
                rows = list(built.pop('changes', None) or [])
                built['change_cursor'] = built.pop('next_cursor', None)
            result = {**built, 'source': source, 'query_time': query_time,
                      'verification_level': built.get('verification_level') or 'NONE'}
            return self._local_page(self.snapshots.start(key, result, 'list', rows, page_size),
                                    operation)
        provider = getattr(self.owner, 'resources', None)
        if provider is None:
            raise CampusError('RESOURCE_PROVIDER_UNAVAILABLE')
        # The provider owns its own ceiling (4.2 default max_search_limit is 50);
        # asking for more is rejected before matching, so clamp to it and report
        # the truncation instead of failing.
        limit = _provider_page_limit(provider)
        search = provider.search(semester=parameters['semester'],
                                 query=parameters.get('query'),
                                 course_id=parameters.get('course_id'),
                                 offset=0, limit=limit)
        rows = list(search.get('items') or [])
        total = search.get('total')
        capped = type(total) is int and total > len(rows)
        warnings = set(search.get('warnings') or ())
        if capped:
            warnings.add('RESOURCE_SEARCH_LIMIT_APPLIED')
        result = {name: value for name, value in search.items() if name != 'items'}
        checked = source_state.normalize_timestamp(search.get('checked_at'), query_time) or query_time
        result.update(source=source, query_time=query_time, checked_at=checked,
                      # The frozen envelope has no LOCAL_INDEX_READ value; a
                      # local index read is local execution state, not a live
                      # business read.
                      verification_level='LOCAL_EXECUTION_STATE',
                      status=search.get('status') or 'UNKNOWN',
                      # A capped search did not read every source row, so it is
                      # not complete even when the local index itself is.
                      source_complete=bool(search.get('source_complete')) and not capped,
                      complete=bool(search.get('complete')) and not capped,
                      warnings=sorted(warnings), next_cursor=None)
        return self._local_page(self.snapshots.start(key, result, 'list', rows, page_size),
                                operation)

    @staticmethod
    def _local_page(page, operation):
        """Expose the page under its natural name, without the legacy ``data`` alias."""
        page['calendar_events' if operation == 'agenda' else
             'changes' if operation == 'agenda_changes' else 'items'] = page['items']
        page.pop('data', None)
        if operation in ('agenda', 'agenda_changes'):
            page['pagination'] = {
                'total_items': page.get('total_items'),
                'returned_items': len(page['items']),
                'next_cursor': page.get('next_cursor'),
                'scope': 'FULL_WINDOW' if operation == 'agenda' else 'FULL_CHANGE_SET',
            }
        return page

    def read_page(self, args, page_size=50, bound=None, normalized=False, operation='page'):
        args = dict(args)
        cursor = args.pop('cursor', None)
        page_size = args.pop('page_size', page_size)
        validate(page_size, {'type': 'integer', 'minimum': 1, 'maximum': 200})
        service = args.get('service_id', 'campus')
        if bound is not None:
            key = bound
        else:
            authenticated = args.get('authenticate', True) is not False
            key = binding(service, 'read_page', args, page_size,
                          account_scope=self._account_scope() if authenticated else None)
        if cursor:
            return self.snapshots.resume(cursor, key)
        offset = args.pop('offset', 0)
        limit = args.pop('limit', 24000)
        selected = args.pop('json_pointer', None)
        query_time = source_state.now_iso()
        raw = self.owner.bridge('read', **catalog.request_arguments(**args), snapshot=True)
        result = page_metadata(raw, service, operation)
        result['query_time'] = result.get('query_time') or query_time
        result['read_scope'] = _scope('PAGE', offset=offset, limit=limit, pages_read=1,
                                      expected_pages=None, query_time=result['query_time'],
                                      parameters={**args, 'offset': offset, 'limit': limit})
        result = {**raw, **result} if normalized else {**result, **raw, 'service_state': result['status']}
        result['checkedAt'] = result['checked_at']
        if 'data' in raw:
            value = pointer(raw['data'], selected or '')
            result.pop('data', None)
            result['json_pointer'] = selected or ''
            result['upstream_total'] = None
            return self.snapshots.start(key, result, 'list' if isinstance(value, list) else 'data', value, page_size)
        if selected is not None:
            raise CampusError('JSON_POINTER_REQUIRES_JSON')
        value = raw.get('text', '')
        result.pop('text', None)
        return self.snapshots.start(key, result, 'text', value, limit, offset)


def legacy_metadata(name, result):
    """Add evidence to old fields without promising unimplemented pagination."""
    if name == 'list_courses':
        complete = result.get('complete', True) is not False
        status = 'PARTIAL' if not complete else ('EMPTY' if not result.get('count') else 'OK')
        warnings = result.get('warnings') if isinstance(result.get('warnings'), list) else []
        return {**metadata('learn', 'courses', 'https://learn.tsinghua.edu.cn/', 'BUSINESS_READ',
                           status, complete, warnings,
                           result.get('checked_at') or result.get('checkedAt')), **result}
    if name in ('list_assignments', 'list_course_files', 'get_assignment'):
        evidence = result.get('read_evidence')
        if evidence:
            out = metadata('learn', name, 'https://learn.tsinghua.edu.cn/', 'BUSINESS_READ',
                           ('EMPTY' if result.get('count') == 0 else 'OK') if evidence['complete'] else 'PARTIAL',
                           evidence['complete'], evidence.get('warnings', []), evidence.get('checked_at'))
            out['source']['adapter_version'] = evidence['adapter_version']
            return {**out, **result, 'source_errors': evidence.get('errors', []), 'upstream_pages': evidence.get('pages', [])}
        return {**metadata('learn', name, 'https://learn.tsinghua.edu.cn/', 'BUSINESS_READ', 'PARTIAL', False, ['UPSTREAM_COMPLETENESS_UNVERIFIED']), **result}
    return result
