"""Module 7.1 tests: static dashboard assets, browser flows and the read-only contract.

Everything here is synthetic and loopback-only.  A ``SyntheticFacade`` behind
the real 6.2 ``LocalApiServer`` serves fixed envelopes; Chromium loads the real
``src/thu_assistant/web_assets`` files.  No campus service, model endpoint,
account, credential or non-loopback address is contacted.

The tests cover the module acceptance cases: empty vs unauthenticated vs source
failure, script/HTML/link injection, paging plus filter invalidation, one source
failing while others render and cancel, keyboard and narrow-screen use, no CDN
or analytics requests, no write side effects, and session expiry clearing the
previous session's details.
"""
from __future__ import annotations

import http.client
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from thu_assistant import local_api, source_state
from thu_assistant.bridge import CampusError
from thu_assistant.local_api import contract
from thu_assistant.local_api import samples as sample_module
from thu_assistant.local_api import server as server_module
from thu_assistant import web_assets

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'src' / 'thu_assistant' / 'web_assets'
ASSETS = WEB / 'assets'
INDEX = WEB / 'index.html'
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)
TODAY = NOW.date()


# ---------------------------------------------------------------------------
# Synthetic data and facade
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def iso(value: datetime) -> str:
    return value.isoformat(timespec='seconds')


def default_courses() -> list[dict]:
    return [
        {'course_id': 'SYN-COURSE-A', 'course_name': '合成课程甲', 'semester': '2026-2027-1',
         'role': 'student', 'teacher_names': ['合成教师甲']},
        {'course_id': 'SYN-COURSE-B', 'course_name': '合成课程乙', 'semester': '2026-2027-1',
         'role': 'teacher', 'teacher_names': ['合成教师乙']},
        {'course_id': 'SYN-COURSE-C', 'course_name': '合成课程丙', 'semester': '2026-2027-2',
         'role': 'student', 'teacher_names': ['合成教师丙']},
    ]


def default_semesters() -> list[dict]:
    return [
        {'semester_id': '2026-2027-1', 'title': '2026-2027 秋季学期', 'current': True},
        {'semester_id': '2026-2027-2', 'title': '2026-2027 春季学期', 'current': False},
    ]


def default_resources() -> list[list[dict]]:
    return [
        [
            {'stable_resource_uid': 'learn:FILE:REC-001', 'title': '合成讲义一',
             'course_id': 'SYN-COURSE-A', 'course_name': '合成课程甲',
             'semester': '2026-2027-1', 'mime_type': 'application/pdf',
             'size_bytes': 12345, 'state': 'ACTIVE',
             'source_link': 'https://learn.tsinghua.edu.cn/course/1'},
            {'stable_resource_uid': 'learn:FILE:REC-002', 'title': '合成讲义二',
             'course_id': 'SYN-COURSE-B', 'course_name': '合成课程乙',
             'semester': '2026-2027-1', 'mime_type': 'text/markdown',
             'size_bytes': 321, 'state': 'ACTIVE'},
        ],
        [
            {'stable_resource_uid': 'learn:FILE:REC-003', 'title': '合成讲义三',
             'course_id': 'SYN-COURSE-A', 'course_name': '合成课程甲',
             'semester': '2026-2027-1', 'mime_type': 'application/vnd.ms-excel',
             'size_bytes': 2048, 'state': 'ACTIVE'},
        ],
    ]


def default_calendar_items() -> list[dict]:
    tomorrow = TODAY + timedelta(days=1)
    yesterday = TODAY - timedelta(days=1)
    return [
        {'event_uid': 'SYN-UID-EXACT', 'title': '合成精确日程',
         'time': {'kind': 'DATE_TIME', 'start': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=9, tzinfo=TZ)),
                  'end': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=10, tzinfo=TZ))},
         'time_semantics': 'INTERVAL', 'calendar_valid': True, 'status': 'ACTIVE',
         'source_id': 'learn.calendar'},
        {'event_uid': 'SYN-UID-DATE', 'title': '合成日期级事项',
         'time': {'kind': 'DATE', 'date': tomorrow.isoformat()},
         'time_semantics': 'OCCURRENCE', 'calendar_valid': True, 'status': 'ACTIVE',
         'source_id': 'learn.assignments'},
        {'event_uid': 'SYN-UID-OVERDUE', 'title': '合成逾期日程',
         'time': {'kind': 'DATE_TIME',
                  'start': iso(datetime.combine(yesterday, datetime.min.time()).replace(hour=8, tzinfo=TZ)),
                  'end': iso(datetime.combine(yesterday, datetime.min.time()).replace(hour=9, tzinfo=TZ))},
         'time_semantics': 'INTERVAL', 'calendar_valid': True, 'status': 'ACTIVE',
         'source_id': 'learn.calendar'},
        {'event_id': 'SYN-UID-UNDATED', 'title': '合成无日期事项',
         'time_semantics': 'OCCURRENCE', 'calendar_valid': False, 'status': 'OPEN',
         'warnings': ['TIME_NOT_FROM_PERIOD_TABLE']},
    ]


def valid_envelope(service: str, operation: str, *, status: str = 'OK',
                   items: list | None = None, complete: bool | None = None,
                   source_complete: bool | None = None, next_cursor: str | None = None,
                   warnings: list | None = None, error: dict | None = None,
                   extra: dict | None = None, url: str | None = None) -> dict:
    if status == 'EMPTY':
        complete = True if complete is None else complete
        source_complete = True if source_complete is None else source_complete
    if complete is None:
        complete = True
    if source_complete is None:
        source_complete = complete
    value = {
        'status': status,
        'source': {'service_id': service, 'operation': operation,
                   'adapter_version': 'fixture-v1', 'url': url},
        'checked_at': '2026-11-17T12:00:00+08:00',
        'verification_level': 'BUSINESS_READ',
        'source_complete': source_complete,
        'snapshot_complete': True,
        'complete': complete,
        'next_cursor': next_cursor,
        'warnings': list(warnings or []),
        'items': list(items or []),
        'query_time': '2026-11-17T11:59:58+08:00',
    }
    if status in source_state.ERROR_OBJECT_STATUSES:
        value['error'] = deepcopy(error) if error is not None else source_state.public_error(status)
    if extra:
        value.update(deepcopy(extra))
    return value


class SyntheticFacade:
    """Explicit synthetic facade; every method records the call it receives."""

    def __init__(self, **options) -> None:
        self.calls: list[tuple] = []
        self.delays = dict(options.get('delays') or {})
        self.failures = dict(options.get('failures') or {})
        self.statuses = dict(options.get('statuses') or {})
        self.health_rows = deepcopy(options.get('health_rows') or [
            {'service_id': 'learn', 'status': 'OK', 'checked_at': '2026-11-17T12:00:00+08:00',
             'complete': True, 'implementation_state': 'IMPLEMENTED',
             'runtime_availability': 'AVAILABLE', 'evidence_level': 'LIVE_READ'},
            {'service_id': 'library', 'status': 'OK', 'checked_at': '2026-11-17T12:00:00+08:00',
             'complete': True, 'implementation_state': 'BLOCKED',
             'runtime_availability': 'LIMITED', 'evidence_level': 'OFFLINE_CONTRACT'},
        ])
        self.health_status = options.get('health_status', 'OK')
        self.services_items = deepcopy(options.get('services_items') or [
            {'id': 'learn', 'name': '网络学堂', 'category': '教学', 'description': '合成来源'},
            {'id': 'library', 'name': '图书馆', 'category': '图书馆', 'description': '合成来源'},
        ])
        self.semesters = deepcopy(options.get('semesters') or default_semesters())
        self.courses = deepcopy(options.get('courses') or default_courses())
        self.resources_pages = deepcopy(options.get('resources_pages') or default_resources())
        self.resource_page_size = int(options.get('resource_page_size') or 50)
        self.todos_items = deepcopy(options.get('todos_items') or [
            {'event_id': 'SYN-EVENT-1', 'title': '合成作业截止',
             'due_at': iso(datetime.combine(TODAY + timedelta(days=1), datetime.min.time()).replace(hour=23, minute=59, tzinfo=TZ)),
             'time_kind': 'DATE_TIME', 'source_id': 'learn.assignments', 'status': 'OPEN'},
        ])
        self.calendar_items = deepcopy(options.get('calendar_items') or default_calendar_items())
        tomorrow = TODAY + timedelta(days=1)
        self.calendar_conflicts = deepcopy(options.get('calendar_conflicts') if 'calendar_conflicts' in options else [
            {'basis': 'TRUSTED_INTERVAL_OVERLAP', 'events': ['SYN-UID-EXACT', 'SYN-UID-OVERLAP'],
             'start': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=9, minute=15, tzinfo=TZ)),
             'end': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=10, tzinfo=TZ))},
        ])
        self.calendar_excluded = deepcopy(options.get('calendar_excluded') or [])
        self.loans_items = deepcopy(options.get('loans_items') or [
            {'title': '合成借阅甲', 'due_at': (TODAY + timedelta(days=10)).isoformat(),
             'status': 'RETURNED', 'location': '合成馆藏地'},
        ])
        self.reservations_items = deepcopy(options.get('reservations_items') or [
            {'room_name': '合成研讨间', 'start': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=14, tzinfo=TZ)),
             'end': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=15, tzinfo=TZ)),
             'status': 'ACTIVE'},
        ])
        self.orders_items = deepcopy(options.get('orders_items') or [
            {'scene_name': '合成体育场馆', 'start': iso(datetime.combine(tomorrow, datetime.min.time()).replace(hour=18, tzinfo=TZ)),
             'status': 'PAID'},
        ])

    # -- delay and failure ------------------------------------------------

    def _wait(self, key: str, cancel) -> None:
        delay = float(self.delays.get(key, 0) or 0)
        if not delay:
            return
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel is not None and getattr(cancel, 'cancelled', False):
                raise CampusError('CANCELLED')
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))

    def _fail(self, *keys: str) -> None:
        for key in keys:
            if key in self.failures:
                raise CampusError(self.failures[key])

    def _status(self, operation: str) -> str:
        return self.statuses.get(operation, 'OK')

    def _items(self, service: str, operation: str, items: list, *,
               next_cursor: str | None = None, extra: dict | None = None) -> dict:
        status = self._status(operation)
        if status == 'EMPTY':
            return valid_envelope(service, operation, status='EMPTY', items=[],
                                  complete=True, source_complete=True,
                                  next_cursor=None, extra=extra)
        if status in source_state.ERROR_OBJECT_STATUSES:
            return valid_envelope(service, operation, status=status, items=[],
                                  complete=False, source_complete=False,
                                  warnings=[f'SOURCE_{status}'], extra=extra)
        if status == 'PARTIAL':
            value = valid_envelope(service, operation, status='PARTIAL', items=items,
                                   complete=False, source_complete=False,
                                   next_cursor=next_cursor,
                                   warnings=['MISSING_SOURCE_PAGES'], extra=extra)
            value['missing_pages'] = [2]
            return value
        return valid_envelope(service, operation, items=items, next_cursor=next_cursor,
                              extra=extra)

    # -- facade protocol --------------------------------------------------

    def health(self, *, service_id=None, cancel=None) -> dict:
        self.calls.append(('health', service_id))
        self._wait('health', cancel)
        self._fail('health')
        return {'status': self.health_status, 'checked_at': '2026-11-17T12:00:00+08:00',
                'services': deepcopy(self.health_rows), 'warnings': []}

    def services(self, *, query=None, category=None, cancel=None) -> dict:
        self.calls.append(('services', query, category))
        self._wait('services', cancel)
        self._fail('services')
        rows = deepcopy(self.services_items)
        if query:
            rows = [row for row in rows if query in row.get('name', '') or query in row.get('id', '')]
        if category:
            rows = [row for row in rows if category in row.get('category', '')]
        value = valid_envelope('local', 'services', items=rows)
        value['items'] = rows
        return value

    def describe(self, service_id, cancel=None) -> dict:
        self.calls.append(('describe', service_id))
        self._wait('describe', cancel)
        self._fail('describe')
        return {'service_id': service_id,
                'operations': [{'operation': 'courses', 'category': 'read',
                                'adapter_version': 'query-v1'}],
                'actions': [], 'registry_version': 'business-facade-v1'}

    def query(self, service_id, operation, parameters, *, cursor=None, page_size=50,
              cancel=None) -> dict:
        self.calls.append(('query', service_id, operation, deepcopy(parameters)))
        self._wait(operation, cancel)
        self._fail(f'query:{operation}', operation)
        if operation == 'semesters':
            return self._items(service_id, operation, self.semesters)
        if operation == 'courses':
            return self._items(service_id, operation, self.courses)
        if operation == 'loans':
            return self._items('library', 'loans', self.loans_items)
        if operation == 'reservations':
            return self._items('library', 'reservations', self.reservations_items)
        if operation == 'orders':
            return self._items('sports', 'orders', self.orders_items)
        return self._items(service_id, operation, [])

    def todos(self, *, start_date=None, end_date=None, cursor=None, page_size=50,
              cancel=None) -> dict:
        self.calls.append(('todos', start_date, end_date))
        self._wait('todos', cancel)
        self._fail('todos')
        return self._items('agenda', 'todos', self.todos_items)

    def calendar(self, *, start_date=None, end_date=None, cursor=None, page_size=50,
                 cancel=None) -> dict:
        self.calls.append(('calendar', start_date, end_date))
        self._wait('calendar', cancel)
        self._fail('calendar')
        extra = {
            'views': {'upcoming': [], 'overdue': [], 'undated': [], 'completed': [],
                      'by_source': {}},
            'conflicts': deepcopy(self.calendar_conflicts),
            'excluded': deepcopy(self.calendar_excluded),
            'as_of': '2026-11-17T12:00:00+08:00',
            'window': {'start_date': start_date, 'end_date': end_date},
            'background_polling': False,
            'message_delivery': False,
            'delivery_state': 'NOT_DELIVERED',
        }
        return self._items('agenda', 'calendar', self.calendar_items, extra=extra)

    def resources(self, *, query=None, course_id=None, semester=None, cursor=None,
                  page_size=50, cancel=None) -> dict:
        self.calls.append(('resources', {'query': query, 'course_id': course_id,
                                         'semester': semester, 'cursor': cursor}))
        self._wait('resources', cancel)
        self._fail('resources')
        if not semester:
            value = valid_envelope('resources', 'resources', status='ERROR',
                                   complete=False, source_complete=False,
                                   warnings=['SEMESTER_REQUIRED'])
            value['error'] = source_state.public_error('INVALID_ARGUMENT_VALUE')
            return value
        index = 0
        if cursor:
            match = re.fullmatch(r'page([2-9][0-9]*)', str(cursor))
            index = int(match.group(1)) - 1 if match else 0
        pages = deepcopy(self.resources_pages)
        all_rows = [row for page in pages for row in page]
        filtered = [row for row in all_rows if self._resource_matches(row, query, course_id, semester)]
        size = max(1, min(int(page_size or 50), self.resource_page_size))
        page_rows = filtered[index * size:(index + 1) * size]
        next_cursor = f'page{index + 2}' if (index + 1) * size < len(filtered) else None
        value = valid_envelope('resources', 'resources', items=page_rows,
                               next_cursor=next_cursor,
                               url='https://learn.tsinghua.edu.cn/')
        value['total_items'] = len(filtered)
        value['returned_items'] = len(page_rows)
        return value

    @staticmethod
    def _resource_matches(row: dict, query, course_id, semester) -> bool:
        if semester and row.get('semester') != semester:
            return False
        if course_id and row.get('course_id') != course_id:
            return False
        if query:
            haystack = ' '.join(str(row.get(key) or '') for key in
                                ('title', 'course_name', 'course_id', 'stable_resource_uid'))
            if query not in haystack:
                return False
        return True


def make_server(tmp_path: Path, facade, *, static_dir=WEB, clock=None, limits=None):
    state = tmp_path / f'state-{time.monotonic_ns()}'
    instance = local_api.LocalApiServer(
        facade, bind='127.0.0.1', port=0, static_dir=static_dir, state_dir=state,
        clock=clock or FakeClock(), limits=limits)
    return instance.start()


def raw_get(server, path: str, headers: dict | None = None):
    connection = http.client.HTTPConnection('127.0.0.1', server.port, timeout=5)
    try:
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Browser fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def browser():
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except Exception as error:  # pragma: no cover - environment skip
            pytest.skip(f'chromium unavailable: {type(error).__name__}')
        yield instance
        instance.close()


@pytest.fixture
def scene_factory(browser, tmp_path):
    resources: list[tuple] = []

    def start(facade=None, *, viewport=(1280, 900), clock=None, limits=None,
              hash_route: str = ''):
        facade = facade if facade is not None else SyntheticFacade()
        clock = clock or FakeClock()
        server = make_server(tmp_path, facade, clock=clock, limits=limits)
        context = browser.new_context(viewport={'width': viewport[0], 'height': viewport[1]})
        page = context.new_page()
        records: list[dict] = []
        console: list[tuple[str, str]] = []
        page.on('request', lambda request: records.append({
            'method': request.method, 'url': request.url, 'headers': dict(request.headers)}))
        page.on('console', lambda message: console.append((message.type, message.text)))
        page.on('pageerror', lambda error: console.append(('pageerror', str(error))))
        page.goto(server.base_url + '/' + hash_route)
        page.wait_for_selector('html[data-app="ready"]', timeout=15000)
        scene = SimpleNamespace(server=server, facade=facade, page=page, context=context,
                                records=records, console=console, clock=clock,
                                base=server.base_url)
        resources.append((server, context))
        return scene

    yield start
    for server, context in resources:
        try:
            context.close()
        except Exception:
            pass
        server.stop()


def wait_cards(page, timeout: int = 10000) -> None:
    page.wait_for_function(
        "() => document.querySelectorAll('[data-card]').length === 7", timeout=timeout)
    page.wait_for_function(
        "() => [...document.querySelectorAll('[data-card]')].every("
        "el => el.dataset.cardState !== 'LOADING' && el.dataset.cardState !== 'IDLE')",
        timeout=timeout)


def wait_quiet(page, selector: str, timeout: int = 10000) -> None:
    page.wait_for_selector(selector, timeout=timeout)
    page.wait_for_function(
        "(sel) => { const root = document.querySelector(sel);"
        " return root && !root.querySelector('.loading-state')"
        " && root.textContent.trim().length > 0; }",
        arg=selector, timeout=timeout)


def open_page(page, name: str) -> None:
    page.click(f'[data-page="{name}"]')
    page.wait_for_selector(f'#view[data-view="{name}"]', timeout=10000)


def json_requests(scene) -> list[dict]:
    result = []
    for row in scene.records:
        parsed = urlsplit(row['url'])
        if parsed.path.startswith('/api/v1/'):
            result.append({**row, 'parsed': parsed, 'query': parse_qs(parsed.query)})
    return result


def facade_methods(scene) -> list[str]:
    return [call[0] for call in scene.facade.calls]


# ---------------------------------------------------------------------------
# Static asset contract
# ---------------------------------------------------------------------------

def test_module_exposes_the_static_directory_and_entry_file() -> None:
    assert web_assets.static_dir() == WEB.resolve()
    assert web_assets.index_file() == WEB.resolve() / 'index.html'
    assert (WEB / 'index.html').is_file()
    assert (ASSETS / 'app.css').is_file()
    assert (ASSETS / 'model.js').is_file()
    assert (ASSETS / 'api.js').is_file()
    assert (ASSETS / 'app.js').is_file()


def test_index_has_no_inline_script_style_or_event_handler() -> None:
    text = INDEX.read_text(encoding='utf-8')
    scripts = re.findall(r'<script\b([^>]*)>(.*?)</script>', text, flags=re.S)
    assert scripts
    for attrs, body in scripts:
        assert ('src="assets/' in attrs or 'src="/assets/' in attrs)
        assert body.strip() == ''
    assert '<style' not in text.lower()
    assert not re.search(r'\sstyle\s*=', text, flags=re.I)
    assert not re.search(r'\son[a-z]+\s*=', text, flags=re.I)


def test_index_declares_language_viewport_referrer_and_readonly_note() -> None:
    text = INDEX.read_text(encoding='utf-8')
    assert 'lang="zh-CN"' in text
    assert 'name="viewport"' in text
    assert 'content="width=device-width, initial-scale=1"' in text
    assert 'name="referrer" content="no-referrer"' in text
    assert '只读看板' in text
    assert '跳到主要内容' in text


def test_javascript_has_no_html_injection_or_storage_calls() -> None:
    forbidden = (r'\.innerHTML\b', r'\.outerHTML\b', r'insertAdjacentHTML\s*\(',
                 r'document\.write\s*\(', r'\beval\s*\(', r'\bnew Function\b',
                 r'\blocalStorage\.', r'\bsessionStorage\.', r'\bindexedDB\.',
                 r'serviceWorker', r'\bXMLHttpRequest\b', r'\bWebSocket\b',
                 r'sendBeacon\s*\(')
    for path in sorted(ASSETS.glob('*.js')):
        text = path.read_text(encoding='utf-8')
        for pattern in forbidden:
            assert not re.search(pattern, text), f'{path.name} matches {pattern}'


def test_javascript_only_issues_relative_get_requests() -> None:
    for path in sorted(ASSETS.glob('*.js')):
        text = path.read_text(encoding='utf-8')
        assert not re.search(r'method\s*:\s*[\'"](?:POST|PUT|PATCH|DELETE|OPTIONS)', text)
        assert 'http://' not in text
    api = (ASSETS / 'api.js').read_text(encoding='utf-8')
    assert "method: 'GET'" in api
    assert "'/api/v1/'" in api or '/api/v1/' in api
    assert "redirect: 'error'" in api
    assert "credentials: 'same-origin'" in api


def test_assets_do_not_reference_cdn_fonts_analytics_or_school_apis() -> None:
    texts = [INDEX.read_text(encoding='utf-8')]
    texts += [path.read_text(encoding='utf-8') for path in sorted(ASSETS.glob('*'))]
    text = '\n'.join(texts)
    assert not re.search(r'@import\s', text)
    assert not re.search(r'url\(\s*[\'"]?https?://', text)
    for marker in ('cdn.', 'google-analytics', 'googletagmanager', 'fonts.googleapis',
                   'analytics.js', 'segment.io', 'sentry.io'):
        assert marker not in text.lower()
    for url in re.findall(r'https://[^\s\'"<>]+', text):
        host = urlsplit(url).hostname or ''
        assert host == 'tsinghua.edu.cn' or host.endswith('.tsinghua.edu.cn'), url
    assert 'http://' not in text


def test_css_is_responsive_and_never_hides_status_text() -> None:
    css = (ASSETS / 'app.css').read_text(encoding='utf-8')
    assert '@media (max-width: 640px)' in css
    assert 'overflow-wrap: anywhere' in css
    assert ':focus-visible' in css
    assert 'min-width: 0' in css
    for selector in ('.badge', '.state-message', '.next-step', '.card-title'):
        for match in re.finditer(re.escape(selector) + r'[^{]*\{([^}]*)\}', css):
            assert 'display: none' not in match.group(1)
            assert 'visibility: hidden' not in match.group(1)
    assert 'outline: none' not in css


def test_web_assets_ship_no_personal_data_or_embedded_samples() -> None:
    text = '\n'.join(path.read_text(encoding='utf-8')
                     for path in sorted(ASSETS.glob('*')) if path.is_file())
    text += INDEX.read_text(encoding='utf-8')
    for marker in ('SYNTHETIC-', 'SYN-COURSE', '/home/', 'C:\\Users', '学号',
                   'password=', 'api_key=', 'eyJhbGciOi'):
        assert marker not in text, marker


def test_static_files_fit_the_server_type_and_size_limits() -> None:
    for path in sorted(WEB.rglob('*')):
        if not path.is_file():
            continue
        if '__pycache__' in path.parts:
            continue
        relative = path.relative_to(WEB).as_posix()
        if path.suffix == '.py':
            assert path.name == '__init__.py', relative
            continue
        assert re.fullmatch(r'(index\.html|assets/[A-Za-z0-9._/-]{1,200})', relative), relative
        assert path.suffix in server_module.STATIC_TYPES, relative
        assert path.stat().st_size <= server_module.STATIC_MAX_BYTES
        for segment in relative.split('/'):
            if segment == 'assets':
                continue
            assert re.fullmatch(r'[A-Za-z0-9._-]{1,128}', segment), relative


# ---------------------------------------------------------------------------
# Static HTTP contract
# ---------------------------------------------------------------------------

def test_static_shell_is_served_with_csp_cookie_and_no_store(tmp_path) -> None:
    server = make_server(tmp_path, SyntheticFacade())
    try:
        status, headers, body = raw_get(server, '/', {'Sec-Fetch-Site': 'none'})
        assert status == 200
        assert b'Openthu' in body
        assert 'Set-Cookie' in headers
        cookie = headers['Set-Cookie']
        assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Path=/' in cookie
        assert 'Domain=' not in cookie
        csp = headers['Content-Security-Policy']
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert 'unsafe-inline' not in csp
        assert headers['Cache-Control'] == 'no-store'
        assert headers['X-Frame-Options'] == 'DENY'
        assert headers['X-Content-Type-Options'] == 'nosniff'
    finally:
        server.stop()


def test_assets_are_served_with_allowlisted_types_and_no_directory_listing(tmp_path) -> None:
    server = make_server(tmp_path, SyntheticFacade())
    try:
        for name, marker in (('app.css', b'--bg'), ('model.js', b'OpenthuModel'),
                             ('api.js', b'createApi'), ('app.js', b'OpenthuModel')):
            status, headers, body = raw_get(server, '/assets/' + name)
            assert status == 200, name
            assert marker in body
            assert headers['Content-Type'].startswith(
                'text/css' if name.endswith('.css') else 'text/javascript')
            assert headers['Cache-Control'] == 'no-store'
        status, _headers, _body = raw_get(server, '/assets/')
        assert status == 400
        status, _headers, _body = raw_get(server, '/assets/missing.js')
        assert status == 404
        status, _headers, _body = raw_get(server, '/assets/../index.html')
        assert status in (400, 404)
    finally:
        server.stop()


def test_cross_site_asset_and_api_requests_are_refused(tmp_path) -> None:
    server = make_server(tmp_path, SyntheticFacade())
    try:
        status, headers, body = raw_get(server, '/assets/app.js',
                                        {'Sec-Fetch-Site': 'cross-site'})
        assert status == 403
        assert b'LOCAL_API_ORIGIN_NOT_ALLOWED' in body
        assert 'Access-Control-Allow-Origin' not in headers
        status, _headers, body = raw_get(server, '/api/v1/health',
                                         {'Sec-Fetch-Site': 'cross-site'})
        assert status == 403
        assert b'LOCAL_API_ORIGIN_NOT_ALLOWED' in body
    finally:
        server.stop()


def test_without_the_asset_directory_the_api_still_works(tmp_path) -> None:
    server = make_server(tmp_path, SyntheticFacade(), static_dir=None)
    try:
        status, _headers, body = raw_get(server, '/')
        assert status == 404
        payload = json.loads(body)
        assert payload['error']['code'] == 'LOCAL_API_UI_NOT_INSTALLED'
        # The JSON API is independent of the optional static shell.
        status, _headers, _body = raw_get(server, '/api/v1/health')
        assert status == 401
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Model contract (executed in the real browser engine)
# ---------------------------------------------------------------------------

def test_model_constants_match_the_frozen_contract(scene_factory) -> None:
    scene = scene_factory()
    value = scene.page.evaluate("() => ({pages: OpenthuModel.PAGES.map(p => p.id),"
                                " size: OpenthuModel.PAGE_SIZE,"
                                " refresh: OpenthuModel.REFRESH_MIN_SECONDS,"
                                " api: OpenthuModel.API,"
                                " states: Object.keys(OpenthuModel.STATES)})")
    described = sample_module.describe_api()
    paths = [route['path'] for route in described['routes']]
    assert value['size'] == contract.LIMITS['page_size_default']
    assert value['refresh'] == contract.LIMITS['refresh_min_seconds']
    assert value['pages'] == ['overview', 'courses', 'resources', 'calendar',
                             'library', 'health']
    for path in (value['api']['health'], value['api']['services'], value['api']['todos'],
                 value['api']['calendar'], value['api']['resources']):
        assert path in paths
    assert value['api']['query'] == contract.BASE_PATH + '/query/'
    for state in described['display_states']:
        assert state in value['states']
    assert 'NEEDS_INPUT' in value['states'] and 'NOT_ENABLED' in value['states']


def test_model_state_and_freshness_labels_are_distinct(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate("""() => {
      const states = ['READY','EMPTY','PARTIAL','LOGIN_REQUIRED','LOCAL_SESSION_REQUIRED',
        'LIMITED','FORBIDDEN','MAINTENANCE','ERROR','UNKNOWN','NEEDS_INPUT','NOT_ENABLED'];
      const labels = states.map(s => [s, OpenthuModel.stateInfo({state:s}).label]);
      const freshness = ['LIVE_BUSINESS_READ','BUSINESS_READ','DIRECTORY_CACHE',
        'LOCAL_EXECUTION_STATE','NONE'].map(level => [level, OpenthuModel.freshnessInfo(level)]);
      return {labels, freshness};
    }""")
    labels = dict(result['labels'])
    assert labels['EMPTY'] != labels['LOGIN_REQUIRED'] != labels['ERROR']
    assert labels['NEEDS_INPUT'] == '需要选择'
    assert labels['NOT_ENABLED'] == '未启用'
    fresh = dict(result['freshness'])
    assert fresh['LIVE_BUSINESS_READ']['label'] == '实时读取'
    assert fresh['BUSINESS_READ']['label'] == '读取快照'
    assert fresh['DIRECTORY_CACHE']['label'] == '历史缓存'
    assert fresh['NONE']['label'] == '未标注实时性'


def test_model_rejects_lookalike_and_cas_links(scene_factory) -> None:
    scene = scene_factory()
    values = scene.page.evaluate("""() => ({
      good: OpenthuModel.isSchoolHttps('https://learn.tsinghua.edu.cn/course/1'),
      plain: OpenthuModel.isSchoolHttps('http://learn.tsinghua.edu.cn/'),
      lookalike: OpenthuModel.isSchoolHttps('https://tsinghua.edu.cn.evil.example/'),
      evil: OpenthuModel.isSchoolHttps('https://evil.example/'),
      javascript: OpenthuModel.isSchoolHttps('javascript:alert(1)'),
      cas: OpenthuModel.isSchoolHttps('https://learn.tsinghua.edu.cn/cas/login?service=x'),
      ticket: OpenthuModel.isSchoolHttps('https://learn.tsinghua.edu.cn/?ticket=abc'),
      userinfo: OpenthuModel.isSchoolHttps('https://tsinghua.edu.cn@evil.example/')
    })""")
    assert values['good'] is True
    for key in ('plain', 'lookalike', 'evil', 'javascript', 'cas', 'ticket', 'userinfo'):
        assert values[key] is False, key


def test_model_completeness_never_calls_an_incomplete_result_success(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate("""() => {
      const body = {status:'OK', source:{service_id:'library', operation:'loans',
        adapter_version:'v1', url:null}, checked_at:'2026-11-17T12:00:00+08:00',
        verification_level:'BUSINESS_READ', source_complete:false, snapshot_complete:true,
        complete:false, next_cursor:null, warnings:[], items:[],
        display:{state:'READY', message:'m', next_step:'n',
          completeness:{source_complete:false, snapshot_complete:true, complete:false},
          provenance:{verification_level:'BUSINESS_READ', checked_at:'2026-11-17T12:00:00+08:00'},
          paging:{returned_items:0,total_items:0,has_more:false}}};
      const model = OpenthuModel.envelopeModel(body);
      return {state:model.state, complete:model.completeness.complete,
              label:model.completeness.label, freshness:model.freshness.code};
    }""")
    assert result['state'] == 'READY'
    assert result['complete'] is False
    assert '未完整' in result['label'] or '不完整' in result['label'] or '未确认' in result['label']
    assert result['freshness'] == 'SNAPSHOT'


def test_model_calendar_handles_sample_and_business_time_shapes(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate("""() => {
      const now = new Date().toISOString();
      const nested = OpenthuModel.calendarItem({event_uid:'A', title:'nested',
        time:{kind:'DATE_TIME', start:'2026-11-20T09:00:00+08:00',
              end:'2026-11-20T10:00:00+08:00'}, calendar_valid:true}, now);
      const flat = OpenthuModel.calendarItem({event_uid:'B', title:'flat',
        start:'2026-11-20T09:00:00+08:00', end:'2026-11-20T10:00:00+08:00',
        time_semantics:'INTERVAL'}, now);
      const date = OpenthuModel.calendarItem({event_uid:'C', title:'date',
        time:{kind:'DATE', date:'2026-11-20'}, calendar_valid:true}, now);
      const bad = OpenthuModel.calendarItem({event_id:'D', title:'bad',
        calendar_valid:false}, now);
      return {nested:nested.kind, flat:flat.kind, date:date.kind, bad:bad.valid,
              dateText:date.timeLabel, nestedText:nested.timeLabel};
    }""")
    assert result['nested'] == 'DATE_TIME' and result['flat'] == 'DATE_TIME'
    assert result['date'] == 'DATE'
    assert result['bad'] is False
    assert result['dateText'] == '全天'
    assert '09:00' in result['nestedText']


def test_model_selection_and_paging_rules(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate("""() => {
      const selection = OpenthuModel.createSelection();
      selection.add('learn:FILE:B'); selection.add('learn:FILE:A');
      const first = selection.values();
      selection.remove('learn:FILE:B');
      const after = selection.values();
      const snapshot = selection.reset();
      const reset = selection.values();
      const changed = OpenthuModel.nextQuery({semester:'1', query:''}, {query:'x'});
      const same = OpenthuModel.nextQuery({semester:'1', query:''}, {query:''});
      const query = OpenthuModel.resourceQuery({semester:'1', courseId:'C', query:'q'}, 'page2');
      return {first, after, reset, snapshot, changed:changed.changed, same:same.changed, query};
    }""")
    assert result['first'] == ['learn:FILE:A', 'learn:FILE:B']
    assert result['after'] == ['learn:FILE:A']
    assert result['reset'] == [] and result['snapshot'] == 1
    assert result['changed'] is True and result['same'] is False
    assert result['query']['cursor'] == 'page2'
    assert result['query']['semester'] == '1'
    assert result['query']['page_size'] == contract.LIMITS['page_size_default']
    assert result['query']['course_id'] == 'C'


def test_model_filters_courses_and_resources_locally(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate("""() => {
      const courses = [
        {course_id:'A', course_name:'合成课程', semester:'1', role:'student'},
        {course_id:'B', course_name:'另一个课程', semester:'1', role:'teacher'},
        {course_id:'C', course_name:'合成课程', semester:'2', role:'student'}];
      const resources = [
        {stable_resource_uid:'learn:FILE:A', title:'讲义.pdf', semester:'1',
         course_id:'A', mime_type:'application/pdf'},
        {stable_resource_uid:'learn:FILE:B', title:'表格', semester:'1',
         course_id:'A', mime_type:'application/vnd.ms-excel'},
        {stable_resource_uid:'learn:FILE:C', title:'图片.png', semester:'2',
         course_id:'B', mime_type:'image/png'}];
      return {
        courses: OpenthuModel.filterCourses(courses, {semester:'1', kind:'student', query:'合成'}).map(r => r.course_id),
        resources: OpenthuModel.filterResources(resources, {semester:'1', courseId:'A', kind:'表格'}).map(r => r.stable_resource_uid),
        types: [OpenthuModel.resourceType(resources[0]), OpenthuModel.resourceType(resources[1]),
                OpenthuModel.resourceType(resources[2])],
        facets: OpenthuModel.resourceFacets(resources).kinds.map(k => k.value)
      };
    }""")
    assert result['courses'] == ['A']
    assert result['resources'] == ['learn:FILE:B']
    assert result['types'] == ['PDF', '表格', '图片']
    assert 'PDF' in result['facets'] and '表格' in result['facets']


def test_model_text_strips_control_and_bidi_characters(scene_factory) -> None:
    scene = scene_factory()
    result = scene.page.evaluate(
        "() => OpenthuModel.text('A\\u202eB\\u0000C\\ufeff', 20)")
    assert result == 'ABC'


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def test_overview_renders_seven_cards_and_readonly_notice(scene_factory) -> None:
    scene = scene_factory()
    wait_cards(scene.page)
    assert scene.page.locator('[data-card]').count() == 7
    body = scene.page.inner_text('body')
    assert '只读看板' in body
    assert '成绩、密钥、写入和模型生成不在看板中提供' in body
    assert '取消全部读取' in body


def test_overview_card_shows_source_time_freshness_completeness_and_next_step(scene_factory) -> None:
    scene = scene_factory()
    wait_cards(scene.page)
    card = scene.page.locator('[data-card="loans"]')
    text = card.inner_text()
    assert '来源时间' in text and '2026-11-17 12:00' in text
    assert '实时性' in text and '读取快照' in text
    assert '完整性' in text and '范围完整' in text
    assert '下一步：无需操作' in text
    assert card.locator('[data-state="READY"]').count() == 1


def test_empty_unauthenticated_and_failed_cards_are_visibly_distinct(scene_factory) -> None:
    facade = SyntheticFacade(statuses={'loans': 'EMPTY', 'reservations': 'AUTH_REQUIRED',
                                       'orders': 'TIMEOUT'})
    scene = scene_factory(facade)
    wait_cards(scene.page)
    loans = scene.page.locator('[data-card="loans"]')
    reservations = scene.page.locator('[data-card="reservations"]')
    orders = scene.page.locator('[data-card="orders"]')
    assert loans.locator('[data-state="EMPTY"]').count() == 1
    assert reservations.locator('[data-state="LOGIN_REQUIRED"]').count() == 1
    assert orders.locator('[data-state="LIMITED"]').count() == 1
    loan_text, login_text, error_text = loans.inner_text(), reservations.inner_text(), orders.inner_text()
    assert '没有记录' in loan_text
    assert '未登录' in login_text and '认证' in login_text
    assert '受限' in error_text or '超时' in error_text
    assert loan_text != login_text and login_text != error_text


def test_partial_source_keeps_data_and_is_not_called_complete(scene_factory) -> None:
    facade = SyntheticFacade(statuses={'loans': 'PARTIAL'})
    scene = scene_factory(facade)
    page = scene.page
    wait_cards(page)
    card = page.locator('[data-card="loans"]')
    assert card.locator('[data-state="PARTIAL"]').count() == 1
    text = card.inner_text()
    assert '部分读取' in text
    assert '合成借阅甲' in text
    assert '缺少第 2 页' in text
    assert '范围完整' not in text


def test_api_loss_shows_a_network_error_without_hiding_other_cards(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    scene.context.route(re.compile(r'.*/api/v1/query/library/loans.*'),
                       lambda route: route.abort())
    page.click('[data-card="loans"] [data-action="refresh-card"]')
    page.wait_for_selector('[data-card="loans"][data-card-state="ERROR"]', timeout=5000)
    text = page.locator('[data-card="loans"]').inner_text()
    assert '无法连接本地看板服务' in text
    assert page.locator('[data-card="health"][data-card-state="READY"]').count() == 1
    assert page.locator('[data-card="todos"][data-card-state="READY"]').count() == 1


def test_one_failed_card_does_not_hide_the_other_cards(scene_factory) -> None:
    facade = SyntheticFacade(statuses={'orders': 'TIMEOUT'})
    scene = scene_factory(facade)
    wait_cards(scene.page)
    assert scene.page.locator('[data-card="health"][data-card-state="READY"]').count() == 1
    assert scene.page.locator('[data-card="todos"][data-card-state="READY"]').count() == 1
    assert scene.page.locator('[data-card="courses"][data-card-state="READY"]').count() == 1
    assert scene.page.locator('[data-card="orders"][data-card-state="LIMITED"]').count() == 1
    assert '其他卡片不受影响' in scene.page.locator('[data-card="orders"]').inner_text()


def test_cancelled_loading_keeps_other_cards_and_reports_cancelled(scene_factory) -> None:
    facade = SyntheticFacade(delays={'todos': 5.0})
    scene = scene_factory(facade)
    page = scene.page
    page.wait_for_selector('[data-card="todos"] [data-action="cancel"]', timeout=5000)
    page.click('[data-card="todos"] [data-action="cancel"]')
    page.wait_for_selector('[data-card="todos"][data-card-state="CANCELLED"]', timeout=5000)
    assert '已取消' in page.locator('[data-card="todos"]').inner_text()
    assert page.locator('[data-card="health"][data-card-state="READY"]').count() == 1
    assert page.locator('[data-card="courses"][data-card-state="READY"]').count() == 1


def test_refresh_is_disabled_while_a_page_read_is_pending(scene_factory) -> None:
    facade = SyntheticFacade(delays={'calendar': 2.0})
    scene = scene_factory(facade)
    page = scene.page
    page.click('[data-page="calendar"]')
    page.wait_for_selector('#view[data-view="calendar"]', timeout=10000)
    page.wait_for_selector('[data-calendar-results] .loading-state', timeout=5000)
    assert page.is_disabled('[data-action="refresh-calendar"]')
    assert page.locator(
        '[data-calendar-results] .loading-state button[data-action="cancel"]').count() == 1
    page.wait_for_function(
        "() => !document.querySelector('[data-calendar-results] .loading-state')",
        timeout=10000)
    assert not page.is_disabled('[data-action="refresh-calendar"]')


def test_overview_offers_no_grades_or_write_buttons(scene_factory) -> None:
    scene = scene_factory()
    wait_cards(scene.page)
    text = scene.page.inner_text('body')
    assert '成绩' in text  # only the explicit "not shown" note
    assert '成绩单' not in text
    labels = scene.page.eval_on_selector_all(
        'button', 'els => els.map(el => el.textContent.trim())')
    for forbidden in ('预约', '提交作业', '付款', '续借', '取消预约', '删除', '编辑密钥'):
        assert forbidden not in labels, forbidden
    assert all(label in ('刷新', '重试', '取消加载', '取消全部读取', '应用筛选', '重置',
                         '加载更多', '清除选择', '在资料中查看', '重新打开看板',
                         '时间线', '列表') or '选择' in label for label in labels), labels


def test_recent_read_is_reused_instead_of_hitting_the_refresh_limit(scene_factory) -> None:
    scene = scene_factory()
    wait_cards(scene.page)
    open_page(scene.page, 'courses')
    wait_quiet(scene.page, '[data-course-results]')
    assert '10 秒内复用上次读取结果' in scene.page.locator(
        '[data-course-results]').inner_text()
    course_calls = [call for call in scene.facade.calls
                    if call[0] == 'query' and call[2] == 'courses']
    assert len(course_calls) == 1


# ---------------------------------------------------------------------------
# Escaping and links
# ---------------------------------------------------------------------------

def test_script_and_html_titles_are_rendered_as_text(scene_factory) -> None:
    pages = [[{'stable_resource_uid': 'learn:FILE:XSS-1',
               'title': '<script>window.__xss=1</script>危险标题',
               'course_id': 'SYN-COURSE-A', 'course_name': '合成课程甲',
               'semester': '2026-2027-1', 'mime_type': 'text/html',
               'state': 'ACTIVE',
               'source_link': 'javascript:window.__link=1'}]]
    scene = scene_factory(SyntheticFacade(resources_pages=pages))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    assert page.locator('[data-resource="learn:FILE:XSS-1"]').count() == 1
    assert page.evaluate('window.__xss') is None
    assert page.evaluate('window.__link') is None
    assert page.locator('[data-resource-results] script').count() == 0
    assert page.locator('[data-resource-results] img').count() == 0
    assert '<script>' in page.locator('[data-resource="learn:FILE:XSS-1"]').inner_text()
    assert page.locator('a[href^="javascript"]').count() == 0
    assert page.locator('a[href*="evil.example"]').count() == 0


def test_library_links_are_https_school_only_with_noopener(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'library')
    page.wait_for_function(
        "() => !document.querySelector('[data-library-cards] .loading-state')", timeout=10000)
    anchors = page.eval_on_selector_all(
        'a[data-official-link]',
        """els => els.map(a => ({href:a.href, rel:a.rel, target:a.target,
                                text:a.textContent}))""")
    assert anchors
    for anchor in anchors:
        assert anchor['href'].startswith('https://')
        assert 'tsinghua.edu.cn' in urlsplit(anchor['href']).hostname
        assert 'noopener' in anchor['rel'] and 'noreferrer' in anchor['rel']
        assert anchor['target'] == '_blank'
        assert 'token' not in anchor['href'].lower()
        assert 'ticket=' not in anchor['href'].lower()


def test_library_page_contains_no_write_controls(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'library')
    page.wait_for_function(
        "() => !document.querySelector('[data-library-cards] .loading-state')", timeout=10000)
    labels = page.eval_on_selector_all(
        'button', 'els => els.map(el => el.textContent.trim())')
    for forbidden in ('预约', '取消', '续借', '付款', '提交'):
        assert all(forbidden not in label for label in labels), (forbidden, labels)
    body = page.inner_text('body')
    assert '本页没有预约、取消、续借或付款按钮' in body


# ---------------------------------------------------------------------------
# Courses and resources
# ---------------------------------------------------------------------------

def test_courses_page_filters_by_semester_kind_and_keyword(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'courses')
    wait_quiet(page, '[data-course-results]')
    assert page.locator('[data-course]').count() == 2
    page.select_option('[data-filter="semester"]', '2026-2027-2')
    page.select_option('[data-filter="kind"]', 'student')
    page.fill('[data-filter="query"]', '丙')
    page.click('[data-action="apply-filters"]')
    page.wait_for_function(
        "() => document.querySelectorAll('[data-course]').length === 1", timeout=5000)
    assert page.locator('[data-course="SYN-COURSE-C"]').count() == 1
    page.select_option('[data-filter="semester"]', '')
    page.fill('[data-filter="query"]', '')
    page.select_option('[data-filter="kind"]', '')
    page.click('[data-action="apply-filters"]')
    page.wait_for_function(
        "() => document.querySelectorAll('[data-course]').length === 3", timeout=5000)


def test_course_filter_submits_with_keyboard_enter(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'courses')
    wait_quiet(page, '[data-course-results]')
    page.focus('[data-filter="query"]')
    page.fill('[data-filter="query"]', '甲')
    page.keyboard.press('Enter')
    page.wait_for_function(
        "() => document.querySelectorAll('[data-course]').length === 1", timeout=5000)
    assert page.locator('[data-course="SYN-COURSE-A"]').count() == 1


def test_course_row_opens_resources_with_the_course_filter(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'courses')
    wait_quiet(page, '[data-course-results]')
    page.click('[data-course="SYN-COURSE-A"] [data-action="open-resources"]')
    page.wait_for_selector('#view[data-view="resources"]', timeout=10000)
    wait_quiet(page, '[data-resource-results]')
    assert page.input_value('[data-filter="courseId"]') == 'SYN-COURSE-A'
    resource_calls = [call for call in scene.facade.calls if call[0] == 'resources']
    assert resource_calls and resource_calls[-1][1]['course_id'] == 'SYN-COURSE-A'


def test_resources_requires_a_semester_and_does_not_fake_an_empty_result(scene_factory) -> None:
    facade = SyntheticFacade(failures={'semesters': 'AUTH_REQUIRED'})
    scene = scene_factory(facade)
    page = scene.page
    open_page(page, 'resources')
    page.wait_for_selector('[data-resource-results]', timeout=10000)
    assert page.locator('[data-resource-results] .empty-state').count() == 1
    assert '需要先选择学期' in page.locator('[data-resource-results]').inner_text()
    assert [call for call in scene.facade.calls if call[0] == 'resources'] == []
    page.fill('[data-filter="semester"]', '2026-2027-1')
    page.click('[data-action="apply-filters"]')
    wait_quiet(page, '[data-resource-results]')
    assert page.locator('[data-resource]').count() >= 1


def test_resources_filters_by_semester_course_type_and_keyword(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    assert page.locator('[data-resource]').count() == 3
    page.fill('[data-filter="courseId"]', 'SYN-COURSE-A')
    page.select_option('[data-filter="kind"]', 'PDF')
    page.fill('[data-filter="query"]', '讲义一')
    page.click('[data-action="apply-filters"]')
    page.wait_for_selector('[data-resource="learn:FILE:REC-001"]', timeout=5000)
    assert page.locator('[data-resource]').count() == 1
    page.fill('[data-filter="courseId"]', '')
    page.select_option('[data-filter="kind"]', '')
    page.fill('[data-filter="query"]', '')
    page.click('[data-action="apply-filters"]')
    page.wait_for_selector('[data-resource="learn:FILE:REC-002"]', timeout=5000)
    assert page.locator('[data-resource]').count() == 3


def test_resource_selection_survives_paging_by_stable_uid(scene_factory) -> None:
    scene = scene_factory(SyntheticFacade(resource_page_size=2))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    page.check('[data-select-uid="learn:FILE:REC-001"]')
    assert page.inner_text('[data-selection-count]') == '1'
    page.click('[data-action="load-more"]')
    page.wait_for_selector('[data-resource="learn:FILE:REC-003"]', timeout=5000)
    page.check('[data-select-uid="learn:FILE:REC-003"]')
    assert page.inner_text('[data-selection-count]') == '2'
    plan = page.inner_text('[data-plan]')
    assert 'learn:FILE:REC-001' not in plan  # plan lists titles, not raw ids
    assert '导出范围' in plan
    assert page.locator('[data-resource="learn:FILE:REC-001"] input').is_checked()
    assert page.locator('[data-resource="learn:FILE:REC-003"] input').is_checked()


def test_changing_filters_clears_selection_and_cursor(scene_factory) -> None:
    scene = scene_factory(SyntheticFacade(resource_page_size=2))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    page.check('[data-select-uid="learn:FILE:REC-001"]')
    page.click('[data-action="load-more"]')
    page.wait_for_selector('[data-resource="learn:FILE:REC-003"]', timeout=5000)
    page.check('[data-select-uid="learn:FILE:REC-003"]')
    page.fill('[data-filter="query"]', '讲义三')
    page.click('[data-action="apply-filters"]')
    page.wait_for_selector('[data-resource="learn:FILE:REC-003"]', timeout=5000)
    assert page.inner_text('[data-selection-count]') == '0'
    assert page.locator('[data-resource]').count() == 1
    assert page.locator('[data-resource="learn:FILE:REC-001"]').count() == 0
    latest = [call for call in scene.facade.calls if call[0] == 'resources'][-1]
    assert latest[1]['cursor'] is None
    assert 'learn:FILE:REC-001' not in page.inner_text('[data-plan]')


def test_resources_without_stable_uid_are_not_selectable(scene_factory) -> None:
    pages = [[{'title': '无编号资料', 'course_id': 'SYN-COURSE-A',
               'semester': '2026-2027-1', 'mime_type': 'application/pdf'}]]
    scene = scene_factory(SyntheticFacade(resources_pages=pages))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    checkbox = page.locator('[data-resource="no-uid"] input')
    assert checkbox.is_disabled()
    assert page.inner_text('[data-selection-count]') == '0'


def test_resources_append_without_duplicating_a_repeated_uid(scene_factory) -> None:
    pages = [
        [{'stable_resource_uid': 'learn:FILE:DUP', 'title': '第一页',
          'course_id': 'SYN-COURSE-A', 'semester': '2026-2027-1',
          'mime_type': 'application/pdf'}],
        [{'stable_resource_uid': 'learn:FILE:DUP', 'title': '第一页',
          'course_id': 'SYN-COURSE-A', 'semester': '2026-2027-1',
          'mime_type': 'application/pdf'},
         {'stable_resource_uid': 'learn:FILE:NEW', 'title': '第二页',
          'course_id': 'SYN-COURSE-A', 'semester': '2026-2027-1',
          'mime_type': 'application/pdf'}],
    ]
    scene = scene_factory(SyntheticFacade(resources_pages=pages, resource_page_size=2))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    page.click('[data-action="load-more"]')
    page.wait_for_selector('[data-resource="learn:FILE:NEW"]', timeout=5000)
    assert page.locator('[data-resource]').count() == 2
    assert page.locator('[data-resource="learn:FILE:DUP"]').count() == 1


def test_resources_export_scope_is_visible_and_never_a_download(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    plan = page.inner_text('[data-plan]')
    assert '导出范围' in plan
    assert '本页不执行下载或导出' in plan
    labels = page.eval_on_selector_all('button', 'els => els.map(el => el.textContent.trim())')
    assert '下载' not in labels and '导出' not in labels


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def test_calendar_switches_timeline_and_list_without_refetch(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'calendar')
    wait_quiet(page, '[data-calendar-results]')
    calendar_calls = len([call for call in scene.facade.calls if call[0] == 'calendar'])
    assert page.locator('[data-calendar-mode="timeline"]').count() == 1
    page.click('[data-action="calendar-mode"][data-mode="list"]')
    page.wait_for_selector('[data-calendar-mode="list"]', timeout=3000)
    assert page.get_attribute('[data-action="calendar-mode"][data-mode="list"]',
                              'aria-pressed') == 'true'
    page.click('[data-action="calendar-mode"][data-mode="timeline"]')
    page.wait_for_selector('[data-calendar-mode="timeline"]', timeout=3000)
    assert len([call for call in scene.facade.calls if call[0] == 'calendar']) == calendar_calls


def test_calendar_distinguishes_date_exact_and_overdue(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'calendar')
    wait_quiet(page, '[data-calendar-results]')
    assert page.locator('[data-event="SYN-UID-DATE"][data-kind="DATE"]').count() == 1
    assert page.locator('[data-event="SYN-UID-EXACT"][data-kind="DATE_TIME"]').count() == 1
    assert page.locator('[data-event="SYN-UID-OVERDUE"][data-overdue="true"]').count() == 1
    assert page.locator('[data-bucket="overdue"]').count() == 1
    assert page.locator('[data-bucket="undated"]').count() == 1
    text = page.locator('[data-event="SYN-UID-DATE"]').inner_text()
    assert '全天' in text and '日期级' in text


def test_calendar_reports_conflicts_and_missing_period_limitations(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'calendar')
    wait_quiet(page, '[data-calendar-results]')
    assert page.locator('[data-conflict-count="1"]').count() == 1
    conflict = page.locator('[data-conflict-count="1"]').inner_text()
    assert '合成精确日程' in conflict
    assert page.locator('[data-limitation="true"]').count() == 1
    limitation = page.locator('[data-limitation="true"]').inner_text()
    assert '缺少校历' in limitation or '节次表' in limitation
    undated = page.locator('[data-event="SYN-UID-UNDATED"]').inner_text()
    assert '原始周期' in undated
    assert '不编造日期' in undated


def test_calendar_mode_toggle_is_keyboard_operable(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'calendar')
    wait_quiet(page, '[data-calendar-results]')
    page.focus('[data-action="calendar-mode"][data-mode="list"]')
    page.keyboard.press('Space')
    page.wait_for_selector('[data-calendar-mode="list"]', timeout=3000)
    assert page.get_attribute('[data-action="calendar-mode"][data-mode="list"]',
                              'aria-pressed') == 'true'


# ---------------------------------------------------------------------------
# Health and unimplemented capability
# ---------------------------------------------------------------------------

def test_health_page_shows_implementation_runtime_and_evidence(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    open_page(page, 'health')
    page.wait_for_selector('[data-health-row="learn"]', timeout=10000)
    row = page.locator('[data-health-row="learn"]').inner_text()
    assert '已实现' in row and '可用' in row and '真实读取' in row
    body = page.inner_text('main')
    assert '本地读取不等于校园可用' in body
    assert page.locator('[data-service-catalog]').count() == 1


def test_unimplemented_capability_has_its_own_text(scene_factory) -> None:
    facade = SyntheticFacade(failures={'orders': 'ADAPTER_REQUIRED'})
    scene = scene_factory(facade)
    page = scene.page
    wait_cards(page)
    card = page.locator('[data-card="orders"]')
    assert card.locator('[data-state="NOT_ENABLED"]').count() == 1
    assert '未启用' in card.inner_text()
    assert '没有记录' not in card.inner_text()


# ---------------------------------------------------------------------------
# Session, storage and accessibility
# ---------------------------------------------------------------------------

def test_session_expiry_clears_previous_details(scene_factory) -> None:
    facade = SyntheticFacade(loans_items=[
        {'title': '敏感借阅旧数据', 'due_at': (TODAY + timedelta(days=10)).isoformat(),
         'status': 'BORROWED'}])
    clock = FakeClock()
    scene = scene_factory(facade, clock=clock)
    page = scene.page
    wait_cards(page)
    assert '敏感借阅旧数据' in page.inner_text('body')
    clock.advance(20000)  # past session_ttl_seconds (14400)
    page.click('[data-card="health"] [data-action="refresh-card"]')
    page.wait_for_selector('#session-banner:not([hidden])', timeout=10000)
    body = page.inner_text('body')
    assert '本地看板会话已失效' in body
    assert '敏感借阅旧数据' not in body
    assert '密码' in body and '不需要重新登录学校' in body
    assert page.locator('input[type="password"]').count() == 0
    assert page.locator('[data-session-lost="true"]').count() == 1


def test_no_browser_storage_is_used(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    page.check('[data-select-uid="learn:FILE:REC-001"]')
    values = page.evaluate("""() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      cookie: document.cookie,
      serviceWorker: 'serviceWorker' in navigator ? !!navigator.serviceWorker.controller : false
    })""")
    assert values['local'] == []
    assert values['session'] == []
    assert 'openthu' not in values['cookie'].lower()
    assert values['serviceWorker'] is False
    cookies = scene.context.cookies()
    names = {cookie['name'] for cookie in cookies}
    assert 'openthu_local_session' in names
    assert all(cookie['httpOnly'] for cookie in cookies if cookie['name'] == 'openthu_local_session')


def test_skip_link_is_first_tab_stop_and_focus_is_visible(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    page.evaluate("() => document.body.focus()")
    page.keyboard.press('Tab')
    active = page.evaluate("""() => ({
      cls: document.activeElement.className,
      tag: document.activeElement.tagName,
      top: document.activeElement.getBoundingClientRect().top,
      outline: getComputedStyle(document.activeElement).outlineStyle
    })""")
    assert 'skip-link' in active['cls'] and active['tag'] == 'A'
    assert active['top'] >= 0
    assert active['outline'] != 'none'


def test_navigation_is_keyboard_operable_and_aria_current_updates(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    page.focus('[data-page="courses"]')
    page.keyboard.press('Enter')
    page.wait_for_selector('#view[data-view="courses"]', timeout=10000)
    assert page.get_attribute('[data-page="courses"]', 'aria-current') == 'page'
    assert page.title().startswith('课程')
    page.focus('[data-page="calendar"]')
    page.keyboard.press('Enter')
    page.wait_for_selector('#view[data-view="calendar"]', timeout=10000)
    assert page.get_attribute('[data-page="calendar"]', 'aria-current') == 'page'
    assert page.get_attribute('[data-page="courses"]', 'aria-current') is None


def test_all_interactive_elements_have_accessible_names(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    for name in ('courses', 'resources', 'calendar', 'library', 'health'):
        open_page(page, name)
        page.wait_for_timeout(300)
        missing = page.evaluate("""() => {
          const nodes = [...document.querySelectorAll('button,a[href],input,select')];
          return nodes.filter(el => {
            if (el.type === 'hidden') return false;
            const label = (el.getAttribute('aria-label') || el.textContent || '').trim();
            const labelled = el.id && document.querySelector('label[for="' + el.id + '"]');
            return !label && !labelled;
          }).map(el => el.outerHTML.slice(0, 120));
        }""")
        assert missing == [], (name, missing)


def test_narrow_screen_keeps_status_and_error_visible(scene_factory) -> None:
    facade = SyntheticFacade(statuses={'orders': 'TIMEOUT'})
    scene = scene_factory(facade, viewport=(360, 640))
    page = scene.page
    wait_cards(page)
    widths = page.evaluate("() => ({scroll: document.documentElement.scrollWidth,"
                           " inner: window.innerWidth})")
    assert widths['scroll'] <= widths['inner'] + 1
    badge = page.locator('[data-card="orders"] [data-state="LIMITED"]')
    assert badge.is_visible()
    box = badge.bounding_box()
    assert box and box['x'] >= 0 and box['x'] + box['width'] <= 361
    message = page.locator('[data-card="orders"] .state-message')
    assert message.is_visible()
    message_box = message.bounding_box()
    assert message_box and message_box['width'] <= 360


def test_long_chinese_title_wraps_without_hiding_state(scene_factory) -> None:
    long_title = '超长中文资料标题' * 20
    pages = [[{'stable_resource_uid': 'learn:FILE:LONG', 'title': long_title,
               'course_id': 'SYN-COURSE-A', 'course_name': '合成课程甲',
               'semester': '2026-2027-1', 'mime_type': 'application/pdf'}]]
    scene = scene_factory(SyntheticFacade(resources_pages=pages), viewport=(360, 640))
    page = scene.page
    open_page(page, 'resources')
    wait_quiet(page, '[data-resource-results]')
    metrics = page.evaluate("""() => {
      const row = document.querySelector('[data-resource="learn:FILE:LONG"]');
      const title = row.querySelector('.resource-title');
      return {scroll: document.documentElement.scrollWidth, inner: window.innerWidth,
              row: row.getBoundingClientRect().width,
              titleScroll: title.scrollWidth, titleClient: title.clientWidth};
    }""")
    assert metrics['scroll'] <= metrics['inner'] + 1
    assert metrics['titleScroll'] <= metrics['titleClient'] + 1
    assert page.locator('[data-selection-count]').is_visible()


def test_unknown_hash_route_falls_back_to_overview(scene_factory) -> None:
    scene = scene_factory(hash_route='#/not-a-page')
    page = scene.page
    assert page.get_attribute('html', 'data-route') == 'overview'
    assert page.locator('#view[data-view="overview"]').count() == 1


# ---------------------------------------------------------------------------
# Network and side-effect boundaries
# ---------------------------------------------------------------------------

def test_only_local_get_requests_are_made_on_a_full_visit(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    for name in ('courses', 'resources', 'calendar', 'library', 'health', 'overview'):
        open_page(page, name)
        page.wait_for_timeout(250)
    for record in scene.records:
        parsed = urlsplit(record['url'])
        assert record['method'] == 'GET', record
        assert parsed.scheme == 'http' and parsed.hostname == '127.0.0.1', record
        if parsed.path == '/' or parsed.path.startswith('/assets/'):
            continue
        if parsed.path == '/favicon.ico':
            continue
        route, _params = contract.route_for(parsed.path)
        assert route is not None, parsed.path
    assert all(record['method'] == 'GET' for record in scene.records)


def test_no_external_cdn_or_analytics_request_is_made(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    assert scene.records
    third_party = [record['url'] for record in scene.records
                   if urlsplit(record['url']).hostname != '127.0.0.1']
    assert third_party == []
    assert not [row for row in scene.console if row[0] == 'pageerror']


def test_csp_blocks_inline_script_and_external_connect(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    try:
        page.add_script_tag(content='window.__inline_script = 1;')
    except Exception as error:
        assert 'Content Security Policy' in str(error)
    page.wait_for_timeout(150)
    assert page.evaluate('window.__inline_script') is None
    outcome = page.evaluate("""async () => {
      try { await fetch('https://example.com/', {mode:'no-cors'}); return 'allowed'; }
      catch (error) { return 'blocked'; }
    }""")
    assert outcome == 'blocked'


def test_business_facade_receives_only_read_methods(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    for name in ('courses', 'resources', 'calendar', 'library', 'health', 'overview'):
        open_page(page, name)
        page.wait_for_timeout(250)
    allowed = {'health', 'services', 'describe', 'query', 'todos', 'calendar', 'resources'}
    assert set(facade_methods(scene)) <= allowed
    assert not {'prepare', 'execute', 'cancel', 'result', 'submit', 'pay', 'book',
                'renew', 'model'} & set(facade_methods(scene))


def test_refresh_and_detail_navigation_do_not_write(scene_factory) -> None:
    scene = scene_factory()
    page = scene.page
    wait_cards(page)
    page.click('[data-card="health"] [data-action="refresh-card"]')
    page.wait_for_timeout(400)
    open_page(page, 'library')
    page.wait_for_function(
        "() => !document.querySelector('[data-library-cards] .loading-state')", timeout=10000)
    page.click('[data-card="loans"] [data-action="refresh-card"]')
    page.wait_for_timeout(400)
    assert set(facade_methods(scene)) <= {'health', 'services', 'describe', 'query',
                                          'todos', 'calendar', 'resources'}
    write_like = [record for record in scene.records
                  if record['method'] != 'GET'
                  or re.search(r'/(execute|prepare|submit|pay|cancel|renew|book|upload)',
                               urlsplit(record['url']).path)]
    assert write_like == []
