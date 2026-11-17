"""Capability registration for the public facade (module 6.1).

A module file existing is not evidence that a capability is available.  Every
entry below is built from the owning module's own capability report or from a
composition-root provider, and an absent provider, an off write switch or an
unverified source contract keeps the capability out of the default available
set.  The table is the single source for ``describe_service`` state, for the
``prepare``/``execute`` gates and for the health report.

Nothing here performs I/O, reads credentials or contacts a campus service.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import FacadeError

VERSION = 'business-facade-v1'
KINDS = ('READ', 'LOCAL_WRITE', 'CAMPUS_WRITE', 'MODEL')
STATES = ('AVAILABLE', 'DISABLED', 'BLOCKED', 'MISSING')
AUTHORIZATIONS = ('NONE', 'TRUSTED_HOST_AUTHORIZATION')

# Side effects are frozen descriptions, not prose: the public description and
# the actual call path must agree.
NO_EFFECT = 'NONE'
LOCAL_FILE_WRITE = 'LOCAL_FILE_WRITE'
LOCAL_PLAN_WRITE = 'LOCAL_ENCRYPTED_PLAN_WRITE'
CAMPUS_WRITE = 'CAMPUS_WRITE_AFTER_PREFLIGHT_AND_HOST_AUTHORIZATION'
MODEL_REQUEST = 'MODEL_REQUEST_TO_SCHOOL_ENDPOINT'

# Module that owns each read family.  Registration stays with 6.1; the provider
# and its evidence belong to the module that implemented it.
READ_OWNERS = {
    'page': '3.2', 'status': '3.2', 'directory': '3.2',
    'courses': '3.2', 'semesters': '3.2', 'notices': '3.2',
    'notice_detail': '3.2', 'assignments': '3.2', 'assignment_detail': '3.2',
    'files': '3.2', 'calendar': '3.2',
    'catalog_search': '4.3', 'catalog_detail': '4.3', 'loans': '5.1',
    'database_links': '4.3', 'seat_areas': '4.3', 'seat_availability': '4.3',
    'room_types': '4.3', 'room_availability': '4.3', 'reservations': '4.3',
    'scenes': '4.3', 'availability': '4.3', 'orders': '4.3', 'order_detail': '4.3',
    'agenda': '5.4', 'agenda_changes': '5.4', 'resources': '4.2',
}


@dataclass(frozen=True)
class Registration:
    """One public capability and the exact conditions under which it runs."""

    service_id: str
    operation: str
    kind: str
    owner: str
    adapter_version: str
    state: str
    reason: str | None = None
    entry: str = 'query_service'
    authorization: str = 'NONE'
    side_effects: tuple[str, ...] = ()
    evidence: str = 'OFFLINE_CONTRACT'
    prepare_allowed: bool = False
    executable: bool = False
    note: str = ''
    extra: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f'unknown capability kind: {self.kind}')
        if self.state not in STATES:
            raise ValueError(f'unknown capability state: {self.state}')
        if self.authorization not in AUTHORIZATIONS:
            raise ValueError(f'unknown authorization class: {self.authorization}')

    def public(self) -> dict:
        """A description safe for MCP results, CLI JSON and the health report."""
        value = {
            'service_id': self.service_id,
            'operation': self.operation,
            'kind': self.kind,
            'owner': self.owner,
            'adapter_version': self.adapter_version,
            'state': self.state,
            'reason': self.reason,
            'entry': self.entry,
            'authorization': self.authorization,
            'side_effects': list(self.side_effects),
            'requires_authorization': self.authorization != 'NONE',
            'evidence': self.evidence,
            'prepare_allowed': self.prepare_allowed,
            'executable': self.executable,
            'subject_to_campus_authorization': self.kind == 'CAMPUS_WRITE',
        }
        if self.note:
            value['note'] = self.note
        for key, item in self.extra:
            value.setdefault(key, item)
        return value

    @property
    def blocking(self) -> bool:
        return self.state != 'AVAILABLE'


@dataclass
class Providers:
    """Composition-root providers.  ``None`` means the provider is absent."""

    actions: Any = None
    queries: Any = None
    todos: Any = None
    submissions: Any = None
    resources: Any = None
    media: Any = None
    academics: Any = None

    def snapshot(self) -> dict[str, bool]:
        return {name: getattr(self, name) is not None
                for name in ('actions', 'queries', 'todos', 'submissions', 'resources',
                             'media', 'academics')}


def _query_entries(providers: Providers, service_ids: Iterable[str],
                   describe) -> tuple[list[Registration], list[dict]]:
    entries: list[Registration] = []
    failures: list[dict] = []
    local = _local_table()
    for service in service_ids:
        try:
            operations = describe(service).get('operations') or []
        except Exception:
            # A service whose description fails keeps an explicit failure record
            # and no invented operations.  Exception text is never copied.
            failures.append({'service_id': service, 'stage': 'QUERY_DESCRIPTION',
                             'code': 'CAPABILITY_REPORT_FAILED'})
            continue
        for operation in operations:
            name = operation.get('operation')
            if not isinstance(name, str):
                continue
            if service == _LOCAL_SERVICE and name in local:
                continue  # handled by _local_entries with its real state
            entries.append(Registration(
                service_id=service, operation=name, kind='READ',
                owner=READ_OWNERS.get(name, '3.2'),
                adapter_version=str(operation.get('adapter_version') or 'query-v1'),
                state='AVAILABLE', entry='query_service',
                evidence='REGISTERED_LOCAL_QUERY', executable=True,
                note=str(operation.get('note') or ''),
            ))
    return entries, failures


def _local_table() -> dict:
    from .. import local_ops
    return local_ops.operations()


_LOCAL_SERVICE = 'local'


def _local_entries(providers: Providers, env=None) -> list[Registration]:
    local = _local_table()
    env = os.environ if env is None else env
    agenda_state, agenda_reason = _agenda_state(providers, env)
    resource_state = 'AVAILABLE' if providers.resources is not None else 'MISSING'
    resource_reason = None if resource_state == 'AVAILABLE' else 'RESOURCE_PROVIDER_UNAVAILABLE'
    return [
        Registration(_LOCAL_SERVICE, 'agenda', 'READ', local['agenda']['owner'],
                     local['agenda']['adapter_version'], agenda_state,
                     reason=agenda_reason, evidence='LOCAL_DERIVED_READ',
                     executable=agenda_state == 'AVAILABLE', note=local['agenda']['note']),
        Registration(_LOCAL_SERVICE, 'agenda_changes', 'READ', local['agenda_changes']['owner'],
                     local['agenda_changes']['adapter_version'], agenda_state,
                     reason=agenda_reason, evidence='LOCAL_DERIVED_READ',
                     executable=agenda_state == 'AVAILABLE', note=local['agenda_changes']['note']),
        # 4.2 export_summary is a host API.  6.1 has no CLI/MCP entry for it, so
        # advertising it as executable would contradict the call surface.
        Registration(_LOCAL_SERVICE, 'resources_export', 'LOCAL_WRITE', local['resources']['owner'],
                     local['resources']['adapter_version'], 'MISSING',
                     reason='NO_PUBLIC_ENTRY', entry='none', side_effects=(LOCAL_FILE_WRITE,),
                     evidence='NO_PUBLIC_ENTRY', executable=False,
                     note='4.2 的 export_summary 只在宿主 API 可用；6.1 未提供 CLI/MCP 入口，'
                          '因此不进入默认可用能力表。'),
        Registration(_LOCAL_SERVICE, 'resources', 'READ', local['resources']['owner'],
                     local['resources']['adapter_version'], resource_state,
                     reason=resource_reason, evidence='LOCAL_INDEX_READ',
                     executable=resource_state == 'AVAILABLE', note=local['resources']['note']),
    ]


def _agenda_state(providers: Providers, env) -> tuple[str, str | None]:
    """THU_TODOS=0 suspends the todo/agenda read, so the capability is disabled."""
    if providers.todos is None:
        return 'MISSING', 'AGENDA_PROVIDER_UNAVAILABLE'
    if str(env.get('THU_TODOS', '1')) == '0':
        return 'DISABLED', 'TODOS_DISABLED'
    return 'AVAILABLE', None


def _action_state(enabled: bool, reason: str | None, env) -> tuple[str, str | None, bool]:
    """Map an action contract to a public state, gap reason and prepare gate."""
    if enabled:
        return 'AVAILABLE', None, True
    text = str(reason or 'ADAPTER_REQUIRED')
    if text in ('WRITES_DISABLED', 'SPORTS_WRITES_DISABLED', 'LIBRARY_WRITES_DISABLED',
                'LIBRARY_RENEW_DISABLED'):
        return 'DISABLED', text, False
    if text == 'SOURCE_CONTRACT_UNVERIFIED':
        # Renewal may still prepare a plan and hand off to the official page;
        # execution stays blocked until a verified request contract exists.
        return 'BLOCKED', text, True
    return 'MISSING', text, False


def _action_entries(providers: Providers, env) -> tuple[list[Registration], list[dict]]:
    from .. import actions
    entries: list[Registration] = []
    failures: list[dict] = []
    try:
        services = sorted({service for service, _ in actions.REGISTRY})
    except Exception:
        return entries, [{'service_id': '*', 'stage': 'ACTION_REGISTRY',
                          'code': 'CAPABILITY_REPORT_FAILED'}]
    for service in services:
        try:
            rows = actions.describe_actions(service)
        except Exception:
            failures.append({'service_id': service, 'stage': 'ACTION_DESCRIPTION',
                             'code': 'CAPABILITY_REPORT_FAILED'})
            continue
        for row in rows:
            operation = row.get('operation')
            if not isinstance(operation, str):
                continue
            state, reason, prepare_allowed = _action_state(bool(row.get('enabled')), row.get('reason'), env)
            if service == 'library' and operation == 'renew_loan' and state == 'BLOCKED':
                # The renewal switch is separate from the general library one.
                state, reason, prepare_allowed = _renewal_state(env)
            kind = 'CAMPUS_WRITE'
            entries.append(Registration(
                service_id=service, operation=operation, kind=kind,
                owner=_ACTION_OWNERS.get(service, '4.4'),
                adapter_version=str(row.get('adapter_version') or 'action-v1'),
                state=state, reason=reason, entry='prepare_service_action',
                authorization='TRUSTED_HOST_AUTHORIZATION',
                side_effects=(LOCAL_PLAN_WRITE, CAMPUS_WRITE) if state != 'MISSING' else (),
                evidence=('VERIFIED_ADAPTER' if state == 'AVAILABLE' else 'CONTRACT_ONLY'
                          if state == 'BLOCKED' else 'SWITCHED_OFF'
                          if state == 'DISABLED' else 'NO_PROVIDER'),
                prepare_allowed=prepare_allowed, executable=state == 'AVAILABLE',
                note=str(row.get('reconciliation_scope') or row.get('reconciliation') or ''),
                extra=(('prechecks', list(row.get('prechecks') or [])),
                       ('request_sequence', row.get('request_sequence')),
                       ('risk_conditions', list(row.get('risk_conditions') or []))),
            ))
    return entries, failures


def _renewal_state(env) -> tuple[str, str | None, bool]:
    """A switched-off renewal must not still advertise a prepare path."""
    from .. import library_contracts
    if not library_contracts.enabled():
        return 'DISABLED', 'LIBRARY_WRITES_DISABLED', False
    if not library_contracts.renewal_enabled():
        return 'DISABLED', 'LIBRARY_RENEW_DISABLED', False
    return 'BLOCKED', 'SOURCE_CONTRACT_UNVERIFIED', True


_ACTION_OWNERS = {'library': '5.1', 'sports': '5.2', 'learn': '5.3'}


def _module_entries(providers: Providers, env) -> tuple[list[Registration], list[dict]]:
    entries: list[Registration] = []
    failures: list[dict] = []
    # 5.3 submission capability: blocked source evidence, own write switch.
    try:
        from .. import submissions
        from ..submissions import capability_report
        report = capability_report()
        entries.append(Registration(
            service_id=str(report.get('service_id') or 'learn'),
            operation=str(report.get('operation') or 'submit_assignment'),
            kind='CAMPUS_WRITE', owner='5.3',
            adapter_version=str(report.get('adapter_version') or 'submission-v1'),
            state='BLOCKED', reason=str(report.get('start_conclusion') or 'BLOCKED_SOURCE_EVIDENCE'),
            entry='prepare_service_action', authorization='TRUSTED_HOST_AUTHORIZATION',
            side_effects=(), evidence='BLOCKED_SOURCE_EVIDENCE',
            prepare_allowed=False, executable=False,
            note='生产上传、草稿与正式提交均未取得来源契约；写开关默认关闭。',
            extra=(('write_switch', report.get('write_switch')),
                   ('write_switch_enabled', bool(report.get('enabled'))),
                   ('blocked_scope', list(report.get('blocked_scope') or [])),
                   ('side_effects_if_enabled', list(report.get('side_effects') or []))),
        ))
    except Exception as error:  # pragma: no cover - defensive, provider optional
        if not isinstance(error, FacadeError):
            failures.append({'service_id': 'learn', 'stage': 'SUBMISSION_CAPABILITY',
                             'code': 'SUBMISSION_PROVIDER_UNAVAILABLE'})
            entries.append(Registration(
                service_id='learn', operation='submit_assignment', kind='CAMPUS_WRITE',
                owner='5.3', adapter_version='unavailable', state='MISSING',
                reason='SUBMISSION_PROVIDER_UNAVAILABLE', entry='prepare_service_action',
                authorization='TRUSTED_HOST_AUTHORIZATION', evidence='NO_PROVIDER',
            ))
    # 4.1 academic reads: three capabilities, all blocked source evidence.
    try:
        from .. import academics
        report = academics.capability_report()
        for name, spec in (report.get('capabilities') or {}).items():
            operation = str(spec.get('operation') or name)
            entries.append(Registration(
                service_id='academics', operation=operation, kind='READ', owner='4.1',
                adapter_version=str(report.get('version') or 'academic-enablement-v1'),
                state='BLOCKED', reason=str(spec.get('start_conclusion') or 'BLOCKED_SOURCE_EVIDENCE'),
                entry='get_todos' if operation in ('exams', 'timetable') else 'query_service',
                evidence='BLOCKED_SOURCE_EVIDENCE', executable=False, prepare_allowed=False,
                note='个人课表、考试、成绩生产来源未取得；未选择能力时不会读取。',
                extra=(('sensitive', bool(spec.get('sensitive'))),
                       ('blocked_scope', list(spec.get('blocked_scope') or []))),
            ))
    except Exception:
        # Keep the three known academic operations visible as a specific gap
        # instead of losing the service from describe/health.
        failures.append({'service_id': 'academics', 'stage': 'ACADEMIC_CAPABILITY',
                         'code': 'CAPABILITY_REPORT_FAILED'})
        for operation in ('exams', 'timetable', 'grades'):
            entries.append(Registration(
                service_id='academics', operation=operation, kind='READ', owner='4.1',
                adapter_version='unavailable', state='MISSING',
                reason='CAPABILITY_REPORT_FAILED', entry='get_todos' if operation in ('exams', 'timetable') else 'query_service',
                evidence='NO_PROVIDER', executable=False, prepare_allowed=False,
                note='4.1 能力报告不可用；未选择能力时不会读取，也不产生空成功。',
            ))
    # 3.3/4.5 media: registration and model transport are host APIs.  6.1 exposes
    # no public entry for preparation, so it is not advertised as executable;
    # the model image transport is blocked because the protocol registry is empty.
    entries.append(Registration(
        service_id=_LOCAL_SERVICE, operation='media_prepare', kind='LOCAL_WRITE', owner='3.3',
        adapter_version='media-reference-v1', state='MISSING',
        reason='NO_PUBLIC_ENTRY', entry='none', side_effects=(LOCAL_FILE_WRITE,),
        evidence='NO_PUBLIC_ENTRY', prepare_allowed=False, executable=False,
        note='3.3 的 register_user_file 只在宿主 API 可用；6.1 未提供 CLI/MCP 入口，'
             '因此不进入默认可用能力表。',
    ))
    entries.append(Registration(
        service_id=_LOCAL_SERVICE, operation='model_image_input', kind='MODEL', owner='4.5',
        adapter_version='model-image-capability-v1', state='BLOCKED',
        reason='MODEL_IMAGE_INPUT_UNAVAILABLE', entry='model_chat',
        side_effects=(MODEL_REQUEST,), evidence='NO_VERIFIED_PROTOCOL',
        executable=False, prepare_allowed=False,
        note='只有已登记协议且媒体管道可用时才允许发送；目录标注不构成传输证据。',
    ))
    return entries, failures


def registrations(providers: Providers, *, service_ids: Iterable[str], describe,
                  env=None) -> tuple[list[Registration], list[dict]]:
    """Build the full registration list, de-duplicated by service and operation.

    A write operation wins over a same-named read entry: the stricter entry is
    the one a caller must satisfy.  A module report that raises leaves an
    explicit failure record and never disappears silently.
    """
    env = os.environ if env is None else env
    entries: list[Registration] = []
    failures: list[dict] = []
    entries.extend(_local_entries(providers, env))
    query_entries, query_failures = _query_entries(providers, service_ids, describe)
    entries.extend(query_entries)
    failures.extend(query_failures)
    action_entries, action_failures = _action_entries(providers, env)
    entries.extend(action_entries)
    failures.extend(action_failures)
    module_entries, module_failures = _module_entries(providers, env)
    entries.extend(module_entries)
    failures.extend(module_failures)
    chosen: dict[tuple[str, str], Registration] = {}
    for entry in entries:
        key = (entry.service_id, entry.operation)
        current = chosen.get(key)
        if current is None or _strictness(entry) >= _strictness(current):
            chosen[key] = entry
    return list(chosen.values()), failures


def _strictness(entry: Registration) -> tuple[int, int, int]:
    """A stricter entry wins a same-name collision (writes beat reads)."""
    return (1 if entry.kind != 'READ' else 0,
            0 if entry.executable else 1,
            0 if entry.prepare_allowed else 1)


def require(entry: Registration | None, *, write: bool) -> Registration:
    """Refuse a call that is not registered for the requested entry point."""
    if entry is None:
        raise FacadeError('FACADE_OPERATION_NOT_REGISTERED')
    if write and entry.kind != 'CAMPUS_WRITE':
        raise FacadeError('FACADE_WRITE_NOT_REGISTERED', capability=entry.public())
    if not write and entry.kind != 'READ':
        raise FacadeError('FACADE_WRITE_NOT_REGISTERED', capability=entry.public())
    return entry
