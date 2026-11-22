"""Fixed synthetic samples and the interface note the dashboard consumes.

Every value here is synthetic (``SYNTHETIC-*`` identifiers, fixed timestamps)
and free of account, credential or path data.  7.1 can develop against these
samples without a campus account; the module test validates each one against
``result-envelope-v1``.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .. import source_state
from . import contract

CHECKED_AT = '2026-11-17T12:00:00+08:00'
QUERY_TIME = '2026-11-17T11:59:58+08:00'
SOURCE_URL = 'https://learn.tsinghua.edu.cn/'
ADAPTER = 'local-read-api-v1'


def _base(service_id: str, operation: str, *, status: str = 'OK',
          source_complete: bool = True, complete: bool = True) -> dict[str, Any]:
    return {
        'status': status,
        'source': {'service_id': service_id, 'operation': operation,
                   'adapter_version': ADAPTER, 'url': None},
        'checked_at': CHECKED_AT,
        'verification_level': 'BUSINESS_READ',
        'source_complete': source_complete,
        'snapshot_complete': True,
        'complete': complete,
        'next_cursor': None,
        'warnings': [],
        'items': [],
        'query_time': QUERY_TIME,
    }


def health_sample() -> dict[str, Any]:
    value = _base('local', 'health')
    value.update({
        'verification_level': 'NONE',
        'data': {
            'status': 'OK',
            'checked_at': CHECKED_AT,
            'services': [
                {'service_id': 'learn', 'status': 'OK', 'checked_at': CHECKED_AT,
                 'complete': True, 'implementation_state': 'IMPLEMENTED',
                 'runtime_availability': 'AVAILABLE', 'evidence_level': 'SYNTHETIC_TRANSPORT'},
                {'service_id': 'library', 'status': 'OK', 'checked_at': CHECKED_AT,
                 'complete': True, 'implementation_state': 'IMPLEMENTED',
                 'runtime_availability': 'AVAILABLE', 'evidence_level': 'SYNTHETIC_TRANSPORT'},
            ],
            'warnings': [],
        },
    })
    return value


def services_sample() -> dict[str, Any]:
    value = _base('local', 'services')
    value.update({
        'items': [
            {'id': 'learn', 'name': '网络学堂', 'category': '学习', 'description': '合成来源'},
            {'id': 'library', 'name': '图书馆', 'category': '学习', 'description': '合成来源'},
        ],
        'returned_items': 2,
    })
    return value


def service_detail_sample() -> dict[str, Any]:
    value = _base('learn', 'describe')
    value.update({
        'verification_level': 'NONE',
        'data': {
            'service_id': 'learn',
            'catalog': {'id': 'learn', 'name': '网络学堂', 'category': '学习'},
            'describe': {
                'service_id': 'learn',
                'operations': [{'operation': 'courses', 'category': 'read',
                                'adapter_version': 'query-v1'}],
                'actions': [{'operation': 'submit_assignment', 'enabled': False,
                             'reason': 'WRITES_DISABLED'}],
                'registry_version': contract.FACADE_CONTRACT_VERSION,
            },
            'actions': [{'operation': 'submit_assignment', 'enabled': False,
                         'reason': 'WRITES_DISABLED', 'executable_via': 'MCP_CLI_ONLY',
                         'local_route': None}],
            'health': {'status': 'OK', 'checked_at': CHECKED_AT, 'warnings': [],
                       'services': [{'service_id': 'learn', 'status': 'OK',
                                     'checked_at': CHECKED_AT, 'complete': True}]},
            'write_routes': [],
        },
    })
    return value


def query_sample() -> dict[str, Any]:
    value = _base('learn', 'courses')
    value.update({
        'source': {'service_id': 'learn', 'operation': 'courses',
                   'adapter_version': 'query-v1', 'url': SOURCE_URL},
        'items': [{'course_id': 'SYNTHETIC-COURSE-1', 'course_name': '合成课程',
                   'semester': 'SYNTHETIC-TERM'}],
        'returned_items': 1,
        'total_items': 1,
    })
    return value


def todos_sample() -> dict[str, Any]:
    value = _base('agenda', 'todos')
    value.update({
        'verification_level': 'NONE',
        'items': [{'event_id': 'SYNTHETIC-EVENT-1', 'title': '合成作业截止',
                   'due_at': '2026-11-20T23:59:00+08:00', 'time_kind': 'DATE_TIME',
                   'source_id': 'learn.assignments', 'status': 'OPEN'}],
        'returned_items': 1,
        'total_items': 1,
    })
    return value


def calendar_sample() -> dict[str, Any]:
    value = _base('agenda', 'calendar')
    value.update({
        'verification_level': 'NONE',
        'items': [{'event_uid': 'SYNTHETIC-UID-1', 'title': '合成日程',
                   'start': '2026-11-20T09:00:00+08:00',
                   'end': '2026-11-20T10:00:00+08:00',
                   'time_semantics': 'INTERVAL', 'calendar_valid': True}],
        'returned_items': 1,
        'total_items': 1,
        'views': {'upcoming': ['SYNTHETIC-UID-1'], 'overdue': [], 'undated': [],
                  'completed': [], 'by_source': {'learn.assignments': ['SYNTHETIC-UID-1']}},
        'conflicts': [],
        'as_of': CHECKED_AT,
        'window': {'start_date': '2026-11-17', 'end_date': '2026-11-24'},
        'background_polling': False,
        'message_delivery': False,
        'delivery_state': 'NOT_DELIVERED',
    })
    return value


def resources_sample() -> dict[str, Any]:
    value = _base('resources', 'resources')
    value['source']['url'] = SOURCE_URL
    value.update({
        'items': [{'stable_resource_uid': 'SYNTHETIC-RESOURCE-1',
                   'title': '合成讲义', 'course_id': 'SYNTHETIC-COURSE-1',
                   'kind': 'PDF', 'size_bytes': 1234,
                   'source_record_id': 'SYNTHETIC-FILE-1'}],
        'returned_items': 1,
        'total_items': 1,
    })
    return value


def auth_required_sample() -> dict[str, Any]:
    value = _base('learn', 'assignments', status='AUTH_REQUIRED',
                  source_complete=False, complete=False)
    value.update({
        'verification_level': 'NONE',
        'error': source_state.public_error('AUTH_REQUIRED', category='AUTH'),
        'warnings': ['SOURCE_AUTH_REQUIRED'],
    })
    return value


def partial_sample() -> dict[str, Any]:
    value = _base('library', 'loans', status='PARTIAL', source_complete=False,
                  complete=False)
    value.update({
        'verification_level': 'BUSINESS_READ',
        'missing_pages': [2],
        'warnings': ['MISSING_SOURCE_PAGES'],
        'items': [{'title': '合成借阅', 'due_at': '2026-12-01'}],
        'returned_items': 1,
    })
    return value


def error_sample() -> dict[str, Any]:
    value = _base('library', 'loans', status='TIMEOUT', source_complete=False,
                  complete=False)
    value.update({
        'verification_level': 'NONE',
        'error': source_state.public_error('TIMEOUT', category='NETWORK'),
        'warnings': ['SOURCE_TIMEOUT'],
    })
    return value


def samples() -> dict[str, dict[str, Any]]:
    return {
        'GET /api/v1/health': health_sample(),
        'GET /api/v1/services': services_sample(),
        'GET /api/v1/services/{service_id}': service_detail_sample(),
        'GET /api/v1/query/{service_id}/{operation}': query_sample(),
        'GET /api/v1/todos': todos_sample(),
        'GET /api/v1/calendar': calendar_sample(),
        'GET /api/v1/resources': resources_sample(),
        'AUTH_REQUIRED': auth_required_sample(),
        'PARTIAL': partial_sample(),
        'TIMEOUT': error_sample(),
    }


def describe_api() -> dict[str, Any]:
    """The complete interface note for 7.1 and for the delivery record."""
    return {
        'contract': contract.CONTRACT_VERSION,
        'facade_contract': contract.FACADE_CONTRACT_VERSION,
        'base_path': contract.BASE_PATH,
        'methods': list(contract.METHOD_ALLOWLIST),
        'bind': {
            'default': contract.DEFAULT_BIND,
            'allowed': list(contract.ALLOWED_BINDS),
            'public_bind': False,
            'host_header_allowlist': list(contract.HOST_ALLOWLIST),
        },
        'limits': deepcopy(contract.LIMITS),
        'routes': contract.route_table(),
        'display_states': contract.display_states(),
        'error_codes': sorted(contract.ERROR_CATEGORIES),
        'forbidden_route_tokens': list(contract.FORBIDDEN_ROUTE_TOKENS),
        'samples': samples(),
        'notes': [
            'Every API request needs a local session; the dashboard obtains one by opening / '
            'through the launcher or by the 0600 bootstrap file used by automated clients.',
            'Only GET and HEAD are accepted; there is no prepare, execute, payment, upload, '
            'key or model route.',
            'Items are display-projected: credentials, member values, private paths and '
            'non-school links are removed before the browser sees them.',
            'A failed or unauthenticated source is never returned as an empty success; '
            'read display.state and display.next_step.',
            'background_polling=false and message_delivery=false: refresh is user-driven and '
            'the API advertises refresh_min_seconds=10.',
        ],
    }
