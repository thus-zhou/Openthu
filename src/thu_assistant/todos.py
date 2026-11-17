"""Deterministic, account-bound aggregation over registered read adapters only."""
from __future__ import annotations
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from learn_assistant.auth import _dpapi_protect, _dpapi_unprotect
from . import calendar_export
from . import events as agenda_events
from . import snapshot_diff
from .bridge import CampusError
from .catalog import state_root
from .gateway_client import _control
from .queries import Snapshots, binding

VERSION = 'todos-v1'
TZ = timezone(timedelta(hours=8))
SOURCES = {
    'learn.assignments': ('learn', 'assignments'),
    'library.loans': ('library', 'loans'),
    'library.seat': ('library', 'reservations'),
    'library.room': ('library', 'reservations'),
    'sports.orders': ('sports','orders'),
}
# Academic capabilities are opt-in: the default capability table blocks all
# three, so a plain get_todos call never reads or pretends to read them.
ACADEMIC_SOURCES = {
    'academics.exams': ('academics', 'exams'),
    'academics.timetable': ('academics', 'timetable'),
}
PROPERTIES = {
    'start_date': {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'},
    'end_date': {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'},
    'services': {'type': 'array', 'minItems': 1, 'maxItems': 11, 'uniqueItems': True,
                 'items': {'type': 'string', 'enum': ['learn', 'library', 'sports', 'academics',
                                                       *ACADEMIC_SOURCES, *SOURCES]}},
    'include_completed': {'type': 'boolean'},
    'include_overdue': {'type': 'boolean'},
    'allow_stale': {'type': 'boolean'},
    'max_source_pages': {'type': 'integer', 'minimum': 1, 'maximum': 50},
    'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 128},
    'semester': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.-]{1,80}$'},
}
TERMINAL = {'SUBMITTED', 'GRADED', 'COMPLETED', 'ATTENDED', 'CANCELLED', 'REJECTED', 'NO_SHOW'}
SEAT_STATES = {
    '预约成功': 'SCHEDULED', '已预约': 'SCHEDULED', '待签到': 'SCHEDULED',
    '使用中': 'IN_PROGRESS', '已签到': 'IN_PROGRESS',
    '已使用': 'ATTENDED',
    '已完成': 'COMPLETED', '已结束': 'COMPLETED', '已取消': 'CANCELLED', '用户取消':'CANCELLED', '系统取消':'CANCELLED',
    '已违约': 'NO_SHOW', '未签到': 'NO_SHOW', '待支付': 'PENDING_PAYMENT',
    '待审核': 'PENDING_APPROVAL', '审核中': 'PENDING_APPROVAL',
}
# Exact official values only; an unfamiliar combination must stay UNKNOWN.
ROOM_STATES = {2: 'SCHEDULED', 4: 'SCHEDULED', 16: 'NO_SHOW', 128: 'COMPLETED',
               256: 'PENDING_APPROVAL', 512: 'REJECTED', 1024: 'APPROVED', 8192: 'PENDING_APPROVAL'}


def clock():
    return datetime.now(TZ)


def check_cancel():
    event, _ = _control.get()
    if event and event.is_set():
        raise CampusError('CANCELLED')


def code(error):
    value = getattr(error, 'code', '')
    return value if isinstance(value, str) and re.fullmatch('[A-Z][A-Z0-9_]{0,100}', value) else 'SOURCE_READ_FAILED'


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise CampusError('INVALID_DATE_RANGE')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CampusError('INVALID_DATE_RANGE') from None


def timestamp(value):
    """Never turn a date-only deadline into an invented midnight."""
    if not value:
        return None, 'unknown'
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    value = value.strip()
    # Learn's jzsj is a 13-digit Unix millisecond string on the live adapter.
    # Do not guess that an arbitrary numeric date is seconds or milliseconds.
    if re.fullmatch(r'[1-9]\d{12}', value):
        return datetime.fromtimestamp(int(value) / 1000, TZ).isoformat(timespec='milliseconds'), 'millisecond'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value).isoformat(), 'date'
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', value):
        raise ValueError('invalid timestamp')
    precision = 'second' if re.match(r'.{10}[T ]\d{2}:\d{2}:\d{2}', value) else 'minute'
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec='seconds' if precision == 'second' else 'minutes'), precision


def canonical(source, row):
    if source == 'learn.assignments':
        parts = [row.get('course_id'), row.get('base_id') or row.get('id')]
    elif source == 'library.loans':
        parts = [row.get('loan_id')]
    else:
        parts = [row.get('reservation_id')]
    if any(not isinstance(x, (str, int)) or isinstance(x, bool) or not str(x) for x in parts):
        raise ValueError('missing stable identifier')
    # JSON avoids ambiguities if an upstream ID contains a separator.
    return json.dumps([str(x) for x in parts], ensure_ascii=False, separators=(',', ':'))


def normalize(source, row, checked_at):
    sid = canonical(source, row)
    event = {
        'event_id': 'todo_' + hashlib.sha256((source + '\0' + sid).encode()).hexdigest()[:32],
        'service': SOURCES[source][0], 'source': source, 'source_id': sid,
        'title': '', 'start_at': None, 'due_at': None, 'end_at': None,
        'date_precision': 'unknown', 'timezone': 'Asia/Shanghai',
        'status': 'UNKNOWN', 'source_url': '', 'checked_at': checked_at,
        'freshness': 'LIVE_AT_CHECKED_AT', 'warnings': [],
    }
    if source == 'learn.assignments':
        event.update(title=str(row.get('title') or '未命名作业'), course_id=row['course_id'],
                     course_name=str(row.get('course_name') or ''), kind='assignment',
                     source_url='https://learn.tsinghua.edu.cn/')
        event['due_at'], event['date_precision'] = timestamp(row.get('deadline'))
        if row.get('submitted') is True:
            event['status'] = 'GRADED' if row.get('graded') is True else 'SUBMITTED'
        elif row.get('submitted') is False:
            event['status'] = 'TODO'
        event['source_status'] = {'submitted': row.get('submitted'), 'graded': row.get('graded')}
    elif source == 'library.loans':
        event.update(title=str(row.get('title') or '未命名借阅'), kind='loan',
                     source_url='https://discover.lib.tsinghua.edu.cn/entrance/myLibEntrance/myLibIndex/MyLibBorrowNow')
        due = row.get('due_date')
        if due and row.get('due_time'):
            due += 'T' + row['due_time']
        event['due_at'], event['date_precision'] = timestamp(due)
        event['status'] = 'BORROWED'  # the registered loans operation returns current loans
        event['source_status'] = {'overdue': row.get('overdue')}
    elif source == 'sports.orders':
        event.update(kind='reservation', title=row.get('scene_name') or '体育预约', status=row['status'],
                     source_status=row.get('platform_status'), source_url=row['official_url'],
                     payment_status=row.get('payment_status'), refund_status=row.get('refund_status'))
        event['start_at'], event['date_precision'] = timestamp(row.get('start_at'))
        event['end_at'], _ = timestamp(row.get('end_at'))
    else:
        event['kind'] = 'reservation'
        event['start_at'], event['date_precision'] = timestamp(row.get('start_time'))
        event['end_at'], _ = timestamp(row.get('end_time'))
        if source == 'library.seat':
            event.update(title=str(row.get('location') or '图书馆座位预约'),
                         status=SEAT_STATES.get(row.get('status_label'), 'UNKNOWN'),
                         source_status=row.get('status_label'),
                         source_url='https://seat.lib.tsinghua.edu.cn/user/index/book')
        else:
            event.update(title='、'.join(str(x.get('name') or '') for x in row.get('rooms', [])) or '研讨间预约',
                         status=ROOM_STATES.get(row.get('platform_status'), 'UNKNOWN'),
                         source_status=row.get('platform_status'),
                         source_url='https://cab.lib.tsinghua.edu.cn/')
    if event['status'] == 'UNKNOWN':
        event['warnings'].append('STATUS_UNKNOWN')
    return event


def deduplicate(events):
    unique = {}
    conflicts = set()
    for event in events:
        key = (event['source'], event['source_id'])
        if key in unique:
            old = unique[key]
            fields = ('title', 'start_at', 'due_at', 'end_at', 'status', 'course_id')
            if any(old.get(k) != event.get(k) for k in fields):
                old['status'] = 'UNKNOWN'
                old['warnings'] = sorted(set(old['warnings'] + ['CONFLICTING_DUPLICATE']))
                conflicts.add(event['source'])
            continue
        unique[key] = deepcopy(event)
    possible = defaultdict(list)
    for event in unique.values():
        if event['kind'] == 'assignment':
            possible[(event.get('course_id'), event['title'], event['due_at'])].append(event)
    for group in possible.values():
        if len(group) > 1:
            for event in group:
                event['possible_duplicate_of'] = [x['event_id'] for x in group if x is not event]
    return list(unique.values()), conflicts


def select(events, start, end, now, include_completed=False, include_overdue=True):
    out = []
    for original in events:
        e = deepcopy(original)
        terminal = e['status'] in TERMINAL
        if terminal and not include_completed:
            continue
        value = e['due_at'] or e['start_at']
        day = date.fromisoformat(value[:10]) if value else None
        overdue = None
        if e['status'] in ('TODO', 'BORROWED') and value:
            overdue = day < now.date() if e['date_precision'] == 'date' else datetime.fromisoformat(value) < now
        e['overdue'] = overdue
        if overdue and not include_overdue:
            continue
        # Reservations are attendance entries, never invented "unfinished" past visits.
        intersects = e['kind'] == 'reservation' and e['end_at'] and day is not None and day <= end and date.fromisoformat(e['end_at'][:10]) >= start
        if day is not None and not (start <= day <= end) and not intersects and not (
                include_overdue and overdue and not terminal):
            continue
        e['group'] = 'completed' if terminal else 'overdue' if overdue else 'unscheduled' if day is None else 'today' if day == now.date() else 'upcoming'
        if not value:
            e['warnings'].append('DATE_UNKNOWN')
        out.append(e)
    # Same-day date-only items precede timed items, without assigning an actual time.
    out.sort(key=lambda e: (e['due_at'] or e['start_at'] or '9999-12-31T99', e['event_id']))
    return out


class TodoCache:
    """Only the latest read per source/account; ciphertext, no plaintext fallback."""
    MAGIC = b'THU-TODOS-1\0'
    def __init__(self, root=None, protect=_dpapi_protect, unprotect=_dpapi_unprotect):
        self.root = Path(root) if root else state_root() / 'todos-v1'
        self.protect, self.unprotect = protect, unprotect

    def path(self, account, source):
        return self.root / (hashlib.sha256(json.dumps([account, source]).encode()).hexdigest() + '.dpapi')

    def save(self, account, source, query_key, data):
        record = {'version': VERSION, 'account': account, 'source': source, 'query_key': query_key, 'data': data}
        blob = self.protect(json.dumps(record, ensure_ascii=False).encode())
        if not blob:
            raise CampusError('TODO_CACHE_ENCRYPT_FAILED')
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(account, source)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with open(temp, 'xb') as f:
                f.write(self.MAGIC + blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self, account, source, query_key, now):
        path = self.path(account, source)
        if not path.exists():
            return None
        if path.stat().st_size > 32 * 1024 * 1024:
            raise CampusError('TODO_CACHE_CORRUPT')
        blob = path.read_bytes()
        if not blob.startswith(self.MAGIC):
            raise CampusError('TODO_CACHE_CORRUPT')
        try:
            record = json.loads(self.unprotect(blob[len(self.MAGIC):]))
            if (record['version'], record['account'], record['source'], record['query_key']) != (VERSION, account, source, query_key):
                return None
            data = record['data']
            age = (now - datetime.fromisoformat(data['checked_at'])).total_seconds()
            return data if 0 <= age <= 7 * 86400 else None
        except Exception:
            raise CampusError('TODO_CACHE_UNREADABLE') from None


class TodoEngine:
    def __init__(self, owner, cache=None, now=clock, academic_bridge=None, incremental=None):
        self.owner = owner
        self.cache = cache if cache is not None else TodoCache()
        self.now = now
        self.snapshots = Snapshots()
        self.academic_bridge = academic_bridge if academic_bridge is not None else agenda_events.AcademicBridge()
        self.incremental = incremental if incremental is not None else snapshot_diff.IncrementalStore()

    def _academic_sources(self, services):
        if services is None:
            return []
        return [source for source in ACADEMIC_SOURCES
                if source in services or 'academics' in services]

    def _academic_statuses(self, selected, now, semester=None, expansions=None):
        """Read only explicitly selected, currently available capabilities.

        The default academics table blocks exams, timetable and grades.  A
        blocked capability produces an explicit ``UNKNOWN`` status with
        ``ADAPTER_REQUIRED``; it never becomes an empty successful source.
        """
        if not selected:
            return [], []
        try:
            capabilities = self.academic_bridge.capabilities()
        except Exception:
            capabilities = {}
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        statuses, events = [], []
        for source in selected:
            capability = ACADEMIC_SOURCES[source][1]
            spec = capabilities.get(capability)
            spec = spec if isinstance(spec, dict) else {}
            status = {
                'source': source, 'service': 'academics', 'operation': capability,
                'status': 'UNKNOWN', 'complete': False, 'snapshot_complete': True,
                'freshness': 'NOT_READ', 'checked_at': now.isoformat(), 'warnings': [],
                'errors': [], 'pages_read': 0, 'records_read': 0,
                'next_upstream_page': None, 'scope': 'REQUESTED_SEMESTER',
                'parameters': {'semester': semester, 'expansions': bool(expansions)},
                'capability_state': spec.get('start_conclusion'),
                'implementation_state': spec.get('implementation_state'),
                'runtime_availability': spec.get('runtime_availability'),
                'evidence_level': spec.get('evidence_level') or 'NONE',
                'source_version': spec.get('version') or 'academics-enablement-v1',
            }
            available = (spec.get('implementation_state') not in (None, 'BLOCKED')
                         and spec.get('runtime_availability') == 'AVAILABLE'
                         and not str(spec.get('start_conclusion') or '').startswith('BLOCKED'))
            if not available:
                status['warnings'] = ['SOURCE_CONTRACT_BLOCKED']
                if spec.get('start_conclusion'):
                    status['warnings'].append('CAPABILITY_' + str(spec['start_conclusion']))
                status['errors'] = [{'code': 'ADAPTER_REQUIRED'}]
                statuses.append(status)
                continue
            try:
                data = self.academic_bridge.read(capability, semester=semester,
                                                 expansions=expansions)
            except TypeError:
                # A caller-supplied bridge predating the semester/expansion
                # forwarding still works; the default bridge accepts both.
                try:
                    data = self.academic_bridge.read(capability)
                except Exception as error:
                    if code(error) == 'CANCELLED':
                        raise
                    status.update(status='FAILED', errors=[{'code': code(error)}],
                                  warnings=['ACADEMICS_READ_FAILED'])
                    statuses.append(status)
                    continue
            except Exception as error:
                if code(error) == 'CANCELLED':
                    raise
                status.update(status='FAILED', errors=[{'code': code(error)}],
                              warnings=['ACADEMICS_READ_FAILED'])
                statuses.append(status)
                continue
            data = data if isinstance(data, dict) else {}
            rows = [row for row in data.get('events') or () if isinstance(row, dict)]
            excluded = list(data.get('excluded') or ())
            complete = bool(data.get('complete')) and not excluded
            pages_read = data.get('pages_read')
            if type(pages_read) is not int or pages_read < 0:
                pages_read = 1
            status.update(status='OK' if rows else ('EMPTY' if complete else 'UNKNOWN'),
                          complete=complete,
                          freshness='LIVE_AT_CHECKED_AT',
                          pages_read=pages_read,
                          records_read=data.get('records_read') if type(data.get('records_read')) is int
                          else len(rows) + len(excluded),
                          warnings=sorted(set(data.get('warnings') or ())
                                          | ({'EVENT_CONVERSION_EXCLUDED'} if excluded else set())),
                          source_version=data.get('source_version') or status['source_version'])
            if not complete:
                status['status'] = 'PARTIAL'
            if data.get('checked_at'):
                status['checked_at'] = data['checked_at']
            for event in rows:
                events.append({'source_id': source, 'event': event})
            statuses.append(status)
        return statuses, events

    def read_source(self, source, parameters, cap):
        service, operation = SOURCES[source]
        events = []
        status = {'source': source, 'service': service, 'operation': operation,
                  'status': 'SUCCESS', 'complete': True, 'freshness': 'LIVE_AT_CHECKED_AT',
                  'checked_at': self.now().isoformat(), 'warnings': [], 'errors': [],
                  'pages_read': 0, 'records_read': 0, 'next_upstream_page': None,
                  'scope': 'CURRENT_SEMESTER' if service == 'learn' else
                           'CURRENT_LOANS' if operation == 'loans' else 'OWN_SPORTS_RESERVATION_HISTORY' if service == 'sports' else
                           'ALL_SEAT_HISTORY' if source == 'library.seat' else 'REQUESTED_DATE_RANGE_ALL_STATUS_BITS',
                  'parameters': deepcopy(parameters)}
        cursor = None
        tokens = set()
        totals = set()
        seen_previous_pages = set()
        for _ in range(cap):
            check_cancel()
            try:
                result = self.owner.queries.query(service, operation, parameters, cursor=cursor, page_size=200)
            except Exception as error:
                if code(error) == 'CANCELLED':
                    raise
                status['errors'].append({'code': code(error)})
                status['complete'] = False
                break
            status['pages_read'] += 1
            status['checked_at'] = result.get('checked_at', status['checked_at'])
            status['warnings'].extend(result.get('warnings', []))
            status['errors'].extend({'code': x.get('code', 'SOURCE_READ_FAILED')} for x in result.get('source_errors', []))
            if result.get('status') not in ('OK', 'EMPTY', 'PARTIAL') or not result.get('source_complete'):
                status['complete'] = False
                status['warnings'].append('UPSTREAM_' + str(result.get('status', 'UNKNOWN')))
            if result.get('upstream_total') is not None:
                totals.add(result['upstream_total'])
            rows = result.get('items', [])
            status['records_read'] += len(rows)
            page_ids = set()
            for row in rows:
                try:
                    event = normalize(source, row, result.get('checked_at', status['checked_at']))
                    events.append(event)
                    page_ids.add(event['source_id'])
                except (ValueError, TypeError, KeyError):
                    status['complete'] = False
                    status['warnings'].append('INVALID_SOURCE_RECORD')
            if page_ids & seen_previous_pages:
                status['complete'] = False
                status['warnings'].append('DUPLICATE_ACROSS_SOURCE_PAGES')
            seen_previous_pages.update(page_ids)
            cursor = result.get('next_cursor')
            next_page = result.get('next_page')
            status['next_upstream_page'] = next_page
            if cursor:
                if cursor in tokens:
                    status['warnings'].append('REPEATED_SOURCE_CURSOR')
                    status['complete'] = False
                    break
                tokens.add(cursor)
            elif next_page:
                if type(next_page) is not int or next_page <= parameters.get('page', 1):
                    status['warnings'].append('INVALID_SOURCE_NEXT_PAGE')
                    status['complete'] = False
                    break
                parameters = {**parameters, 'page': next_page}
            else:
                break
        else:
            if cursor or status['next_upstream_page']:
                status['complete'] = False
                status['warnings'].append('SOURCE_PAGE_LIMIT')
        if len(totals) > 1:
            status['complete'] = False
            status['warnings'].append('SOURCE_CHANGED_DURING_PAGING')
        status['upstream_total'] = next(iter(totals)) if len(totals) == 1 else None
        if status['errors'] or not status['complete']:
            status['status'] = 'PARTIAL' if status['pages_read'] else 'FAILED'
        status['warnings'] = sorted(set(status['warnings']))
        return {'items': events, **status}

    def get(self, start_date=None, end_date=None, services=None, include_completed=False,
            include_overdue=True, cursor=None, allow_stale=False, max_source_pages=3,
            semester=None, academic_expansions=None):
        if os.environ.get('THU_TODOS', '1') == '0':
            raise CampusError('TODOS_DISABLED')
        now = self.now()
        start = parse_date(start_date) if start_date is not None else now.date()
        end = parse_date(end_date) if end_date is not None else start + timedelta(days=6)
        if start > end or (end - start).days > 366:
            raise CampusError('INVALID_DATE_RANGE')
        if any(type(x) is not bool for x in (include_completed, include_overdue, allow_stale)):
            raise CampusError('INVALID_ARGUMENT_TYPE')
        if type(max_source_pages) is not int or not 1 <= max_source_pages <= 50:
            raise CampusError('INVALID_ARGUMENT_RANGE')
        if semester is not None and (not isinstance(semester, str)
                                     or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', semester)):
            raise CampusError('INVALID_ACADEMIC_SEMESTER')
        allowed = ['learn', 'library', 'sports', 'academics', *ACADEMIC_SOURCES, *SOURCES]
        if services is not None and (not isinstance(services, list) or not 1 <= len(services) <= 11 or
                                    any(not isinstance(x, str) or x not in allowed for x in services) or len(set(services)) != len(services)):
            raise CampusError('INVALID_TODO_SERVICES')
        selected = [s for s in SOURCES if services is None or s in services or SOURCES[s][0] in services]
        selected_academic = self._academic_sources(services)
        account = self.owner.bridge('action_identity')['account_id']
        # Bind raw optional dates, so continuation remains valid across midnight.
        key = binding('todos', VERSION, {'account': account, 'start': start_date, 'end': end_date,
                      'sources': selected, 'academic': selected_academic,
                      'semester': semester, 'expansions': bool(academic_expansions),
                      'completed': include_completed,
                      'overdue': include_overdue, 'stale': allow_stale,
                      'max_pages': max_source_pages}, 200)
        if cursor:
            return self.snapshots.resume(cursor, key)
        all_events, statuses, pending_cache = [], [], []
        for source in selected:
            check_cancel()
            _, progress = _control.get()
            if progress:
                progress('READING_TODOS_SOURCE ' + source)
            parameters = {}
            if source == 'library.seat':
                parameters = {'system': 'seat', 'page': 1}
            elif source == 'sports.orders':
                parameters = {'page': 1, 'limit': 50}
            elif source == 'library.room':
                parameters = {'system': 'room', 'start_date': start.isoformat(), 'end_date': end.isoformat(),
                              'status_mask': 65535, 'page': 1, 'limit': 50}
            cache_key = binding(source, VERSION, parameters, max_source_pages)
            data = self.read_source(source, parameters, max_source_pages)
            if data['status'] == 'FAILED' and allow_stale:
                try:
                    old = self.cache.load(account, source, cache_key, now)
                    if old:
                        old['errors'] = data['errors']
                        old.update(status='PARTIAL', complete=False, freshness='HISTORICAL',
                                   refresh_attempted_at=data['checked_at'])
                        old['warnings'] = sorted(set(old['warnings'] + ['HISTORICAL_DATA_SOURCE_REFRESH_FAILED']))
                        for e in old['items']:
                            e['freshness'] = 'HISTORICAL'
                        data = old
                except Exception:
                    data['warnings'].append('TODO_CACHE_UNAVAILABLE')
            elif data['pages_read']:
                pending_cache.append((source, cache_key, data))
            all_events.extend(data['items'])
            statuses.append({k: v for k, v in data.items() if k != 'items'})
        academic_statuses, academic_events = self._academic_statuses(
            selected_academic, now, semester, academic_expansions)
        statuses.extend(academic_statuses)

        def _academic_selected(entry):
            try:
                item = agenda_events.from_calendar_event(
                    entry['event'], source_id=entry['source_id'])
            except CampusError:
                # Keep malformed records for build_agenda's explicit excluded
                # report instead of silently dropping them here.
                return True
            return agenda_events.in_window(
                item, start.isoformat(), end.isoformat(),
                include_completed=include_completed,
                include_overdue=include_overdue)

        academic_events = [entry for entry in academic_events
                           if _academic_selected(entry)]
        # Never publish or retain data if account selection changed mid-query.
        if self.owner.bridge('action_identity')['account_id'] != account:
            raise CampusError('STATE_ACCOUNT_MISMATCH')
        check_cancel()
        for source, cache_key, data in pending_cache:
            try:
                self.cache.save(account, source, cache_key, data)
            except Exception:
                next(s for s in statuses if s['source'] == source)['warnings'].append('TODO_CACHE_SAVE_FAILED')
        events, conflicts = deduplicate(all_events)
        for s in statuses:
            if s['source'] in conflicts:
                s.update(status='PARTIAL', complete=False)
                s['warnings'].append('CONFLICTING_DUPLICATE')
        events = select(events, start, end, now, include_completed, include_overdue)
        complete = all(x['complete'] for x in statuses)
        total_events = len(events) + len(academic_events)
        result = {
            'status': 'OK' if total_events and complete else 'EMPTY' if complete else 'PARTIAL',
            'adapter_version': VERSION, 'checked_at': now.isoformat(), 'timezone': 'Asia/Shanghai',
            'window': {'start_date': start.isoformat(), 'end_date': end.isoformat(), 'inclusive': True},
            'sources_status': statuses, 'source_complete': complete,
            'groups': dict(Counter(x['group'] for x in events)),
            'scope_notes': ['CURRENT_SEMESTER_ASSIGNMENTS_ONLY', 'PAST_RESERVATIONS_NOT_INFERRED_OVERDUE',
                            'REGISTRAR_CALENDAR_NOT_AN_ASSIGNMENT_SOURCE', 'VISITORS_NOT_REGISTERED',
                            'GRADES_NEVER_BECOME_EVENTS'],
            'include_completed': include_completed, 'include_overdue': include_overdue,
            'selection': {'sources': selected, 'academic': selected_academic,
                          'semester': semester},
        }
        if selected_academic:
            result['academic_events'] = academic_events
        return self.snapshots.start(key, result, 'list', events, 200)

    def agenda(self, start_date=None, end_date=None, services=None, include_completed=False,
               include_overdue=True, allow_stale=False, max_source_pages=3, semester=None,
               academic_expansions=None):
        """Return the unified schedule, views, conflicts and source status.

        This is a live, on-demand read: it starts no poller, sends no message
        and does not mark anything as delivered.  When the underlying todo
        snapshot has more than one page, every page is collected before views
        and conflicts are built; a page cap fails closed instead of returning a
        truncated ``OK`` result.
        """
        first = True
        page = None
        items = []
        for _ in range(50):
            page = self.get(
                start_date=start_date, end_date=end_date, services=services,
                include_completed=include_completed, include_overdue=include_overdue,
                allow_stale=allow_stale, max_source_pages=max_source_pages,
                semester=semester, academic_expansions=academic_expansions,
                cursor=None if first else page.get('next_cursor'))
            items.extend(page['items'])
            first = False
            if not page.get('next_cursor'):
                break
        else:
            raise CampusError('AGENDA_RESULT_TRUNCATED')
        checked_at = page['checked_at']
        now = datetime.fromisoformat(checked_at)
        pairs = [(entry['source_id'], entry['event'])
                 for entry in page.get('academic_events', ())
                 if isinstance(entry, dict) and isinstance(entry.get('event'), dict)]
        built = agenda_events.build_agenda(
            items, pairs, statuses=page['sources_status'],
            now=now, window=page['window'], checked_at=checked_at)
        account = self.owner.bridge('action_identity')['account_id']
        sequences = self.incremental.observe(built['calendar_events'], account=account)
        for item in built['calendar_events']:
            uid = item.get('event_uid')
            if uid in sequences:
                item['sequence'] = max(int(item.get('sequence') or 0), sequences[uid])
        return {
            'status': page['status'],
            'source': {'service_id': 'agenda', 'operation': 'agenda',
                       'adapter_version': agenda_events.VERSION},
            'adapter_version': agenda_events.VERSION,
            'checked_at': checked_at,
            'verification_level': 'NONE',
            'complete': page['source_complete'],
            'next_cursor': None,
            'timezone': page['timezone'],
            'window': built['window'],
            'as_of': built['as_of'],
            'sources_status': page['sources_status'],
            'source_complete': page['source_complete'],
            'scope_notes': page['scope_notes'],
            'selection': page['selection'],
            'items': built['items'],
            'calendar_events': built['calendar_events'],
            'undated': built['undated'],
            'excluded': built['excluded'],
            'warnings': sorted(set(page.get('warnings') or ()) | set(built['warnings'])),
            'views': built['views'],
            'conflicts': built['conflicts'],
            'pagination': {
                'total_items': page.get('total_items'),
                'returned_items': len(items),
                'next_cursor': None,
            },
            'background_polling': False,
            'message_delivery': False,
            'delivery_state': 'NOT_DELIVERED',
        }

    def changes(self, cursor=None, start_date=None, end_date=None, services=None,
                include_completed=False, include_overdue=True, allow_stale=False,
                max_source_pages=3, semester=None, academic_expansions=None):
        """Return one on-demand incremental change set.

        The baseline is the persisted/in-memory snapshot or the snapshot behind
        the supplied opaque cursor.  Producing a change set never marks an event
        as delivered.
        """
        agenda = self.agenda(start_date=start_date, end_date=end_date, services=services,
                             include_completed=include_completed,
                             include_overdue=include_overdue, allow_stale=allow_stale,
                             max_source_pages=max_source_pages, semester=semester,
                             academic_expansions=academic_expansions)
        sources = [row['source'] for row in agenda['sources_status']]
        scope = {
            'start_date': agenda['window']['start_date'],
            'end_date': agenda['window']['end_date'],
            'sources': sources,
            'include_completed': include_completed,
            'include_overdue': include_overdue,
            'semester': semester,
            'allow_stale': allow_stale,
            'max_source_pages': max_source_pages,
        }
        account = self.owner.bridge('action_identity')['account_id']
        current = {'events': agenda['calendar_events'],
                   'sources': agenda['sources_status'],
                   'checked_at': agenda['checked_at']}
        result = self.incremental.query(current, account=account, scope=scope, cursor=cursor)
        result['source'] = {'service_id': 'agenda', 'operation': 'changes',
                            'adapter_version': snapshot_diff.VERSION}
        result['verification_level'] = 'NONE'
        result['agenda'] = {
            'status': agenda['status'],
            'as_of': agenda['as_of'],
            'window': agenda['window'],
            'views': agenda['views'],
            'conflicts': agenda['conflicts'],
            'undated': [item['event_id'] for item in agenda['undated']],
            'background_polling': False,
            'message_delivery': False,
        }
        return result

    def export_calendar(self, path, events, *, scope, source_complete=True, warnings=(),
                        allow_partial=False, format='ics', overwrite=False, now=None):
        """Write already-built agenda events to a local file.

        This is a pure local export: it performs no campus read and advances no
        incremental baseline.  Call ``agenda()`` first if a live read is
        intended, or use ``export_agenda()`` as the explicit convenience path.
        """
        if scope is None:
            raise CampusError('EXPORT_SCOPE_REQUIRED')
        if events is None:
            raise CampusError('EXPORT_EVENTS_REQUIRED')
        if not source_complete and not allow_partial:
            raise CampusError('EXPORT_SOURCE_INCOMPLETE')
        return calendar_export.write_export(
            path, events, scope=scope, source_complete=source_complete,
            warnings=warnings, format=format, overwrite=overwrite, now=now)

    def export_agenda(self, path, *, start_date=None, end_date=None, services=None,
                      include_completed=False, include_overdue=True, allow_stale=False,
                      max_source_pages=3, semester=None, academic_expansions=None,
                      format='ics', overwrite=False, allow_partial=False, now=None):
        """Explicit read-then-export convenience; the read is visible here."""
        if start_date is None or end_date is None:
            raise CampusError('EXPORT_SCOPE_REQUIRED')
        agenda = self.agenda(start_date=start_date, end_date=end_date, services=services,
                             include_completed=include_completed,
                             include_overdue=include_overdue, allow_stale=allow_stale,
                             max_source_pages=max_source_pages, semester=semester,
                             academic_expansions=academic_expansions)
        sources = [row['source'] for row in agenda['sources_status']]
        written = self.export_calendar(
            path, agenda['calendar_events'],
            scope={'start_date': agenda['window']['start_date'],
                   'end_date': agenda['window']['end_date'],
                   'sources': sources},
            source_complete=agenda['source_complete'], warnings=agenda['warnings'],
            allow_partial=allow_partial, format=format, overwrite=overwrite, now=now)
        written['source_complete'] = agenda['source_complete']
        written['source_status'] = agenda['status']
        written['delivery_state'] = 'NOT_DELIVERED'
        return written

    def describe_agenda(self):
        """Capability and rule metadata for the future public facade (6.1)."""
        try:
            capabilities = self.academic_bridge.capabilities()
        except Exception:
            capabilities = {}
        return {
            'service_id': 'agenda',
            'event_version': agenda_events.VERSION,
            'incremental_version': snapshot_diff.VERSION,
            'cursor_version': snapshot_diff.CURSOR_VERSION,
            'sources': list(SOURCES),
            'academic_sources': list(ACADEMIC_SOURCES),
            'capabilities': capabilities if isinstance(capabilities, dict) else {},
            'views': ['upcoming', 'overdue', 'undated', 'completed', 'by_source'],
            'change_types': list(snapshot_diff.CHANGE_TYPES),
            'conflict_rule': 'TRUSTED_INTERVAL_ONLY',
            'export_formats': ['ics', 'markdown', 'json'],
            'background_polling': False,
            'message_delivery': False,
            'delivery_state': 'NOT_DELIVERED',
            'registration': '6.1 registers get_agenda/export_calendar/get_event_changes; '
                            'this module changes no MCP tool list.',
        }

