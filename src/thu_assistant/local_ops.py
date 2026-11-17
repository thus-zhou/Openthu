"""Local aggregate operations registered into the existing query entry (6.1).

These are *local* reads: they combine already-registered source reads or read a
local encrypted index.  They are not campus endpoints, so they live beside the
service operations instead of inventing a new MCP tool.

``queries.py`` owns their schemas; ``facade.registry`` uses the same table for
capability state, so a description and the actual dispatch can never disagree
about names.  The module has no import side effects and imports no facade code.
"""
from __future__ import annotations

from typing import Any

SERVICE_ID = 'local'
DATE = {'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$'}
SOURCE = {'type': 'string', 'enum': ['learn', 'library', 'sports', 'academics',
                                      'learn.assignments', 'library.loans', 'library.seat',
                                      'library.room', 'sports.orders',
                                      'academics.exams', 'academics.timetable']}
WINDOW = {
    'start_date': DATE,
    'end_date': DATE,
    'services': {'type': 'array', 'minItems': 1, 'maxItems': 11, 'uniqueItems': True,
                 'items': SOURCE},
    'include_completed': {'type': 'boolean'},
    'include_overdue': {'type': 'boolean'},
    'allow_stale': {'type': 'boolean'},
    'max_source_pages': {'type': 'integer', 'minimum': 1, 'maximum': 50},
    'semester': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.-]{1,80}$'},
}
def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


AGENDA = schema(WINDOW)
CHANGES = schema({**WINDOW, 'change_cursor': {'type': 'string', 'minLength': 1, 'maxLength': 128}})
RESOURCE_FIELDS = {
    'semester': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.-]{1,80}$'},
    'query': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'course_id': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.:-]{1,128}$'},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
}
RESOURCES = schema(RESOURCE_FIELDS, ('semester',))


def _agenda_version() -> str:
    from . import events
    return events.VERSION


def _changes_version() -> str:
    from . import snapshot_diff
    return snapshot_diff.VERSION


def _resources_version() -> str:
    from .resources import api
    return api.FACADE_VERSION


def operations() -> dict[str, dict[str, Any]]:
    """Return the local read table; versions come from the owning modules."""
    agenda, changes, resources = _agenda_version(), _changes_version(), _resources_version()
    return {
        'agenda': {
            'schema': AGENDA, 'pagination': 'local_materialised_snapshot', 'level': 'LOCAL_DERIVED_READ',
            'owner': '5.4', 'adapter_version': agenda,
            'note': '按需读取所有已登记来源并汇总；不启动轮询、不发送消息、不标记已送达。'
                    'views/conflicts 覆盖整个窗口，items/calendar_events 按快照分页。',
        },
        'agenda_changes': {
            'schema': CHANGES, 'pagination': 'local_change_set_snapshot', 'level': 'LOCAL_DERIVED_READ',
            'owner': '5.4', 'adapter_version': changes,
            'note': 'change_cursor 是增量基线游标，next_cursor 只是本次变更集的分页游标；'
                    '两者不可互换。来源失败或未读完时不会产生 DELETED。',
        },
        'resources': {
            'schema': RESOURCES, 'pagination': 'local_index_snapshot', 'level': 'LOCAL_DERIVED_READ',
            'owner': '4.2', 'adapter_version': resources,
            'note': '只读取本地加密资料索引；不下载、不刷新、不调用模型。'
                    '未配置资料库提供者时返回 RESOURCE_PROVIDER_UNAVAILABLE。',
        },
    }
