"""The single business facade (module 6.1).

MCP, the CLI and the local read dashboard call this object instead of deciding
by themselves which service is available, which write needs authorization and
which result is complete.  The six methods are the frozen ``business-facade-v1``
set:

``query``    read a registered operation              (READ_ONLY)
``health``   report capability state                  (READ_ONLY, no live probe)
``prepare``  build one encrypted local plan           (LOCAL_WRITE_ENCRYPTED_PLAN)
``execute``  the only campus-write path               (HOST_AUTHORIZATION_REQUIRED)
``result``   reconcile without resending              (REMOTE_READ_ONLY)
``cancel``   cancel a local queued/running request    (LOCAL_ONLY)

Local exports and the media preparation entry are explicitly *not* campus
methods; they are described beside the six so a caller never mistakes one for a
school operation.
"""
from __future__ import annotations

import os
import time
from .. import source_state
from ..bridge import CampusError
from . import registry as reg
from .errors import FacadeError, gap
from .requests import RequestRegistry, current_id

VERSION = reg.VERSION
METHODS = ('query', 'health', 'prepare', 'execute', 'result', 'cancel')
LOCAL_METHODS = ('describe', 'describe_service', 'export_agenda')

# A read entry may never forward an HTTP method, an authentication header, a
# token or a body to an upstream.  ``url`` stays allowed only for the already
# registered page/read operations, which validate the host separately.
RESERVED_KEYS = frozenset({
    'method', 'http_method', 'headers', 'header', 'body', 'authorization', 'token',
    'access_token', 'refresh_token', 'cookie', 'cookies', 'password', 'secret',
    'api_key', 'x_csrf', 'csrf', 'bearer',
})
WRITE_OPERATIONS = frozenset({
    'create', 'cancel', 'create_reservation', 'cancel_reservation',
    'create_seat_reservation', 'create_room_reservation', 'renew_loan',
    'submit_assignment', 'prepare', 'execute',
})
_MAX_SCAN = 500


class BusinessFacade:
    """Composition-root object shared by every public entry point."""

    def __init__(self, owner, *, env=None, clock=None, submissions=None, resources=None,
                 media=None, academics=None, service_ids=None,
                 submissions_factory=None):
        self.owner = owner
        self.env = dict(os.environ if env is None else env)
        self.clock = clock or time.time
        self.requests = RequestRegistry(clock=self.clock)
        self.resources = resources
        self.media = media
        self.academics = academics
        self._submissions = submissions
        self._submissions_factory = submissions_factory
        self._submissions_attempted = submissions is not None
        self._submissions_failure: str | None = None
        self._service_ids = tuple(service_ids) if service_ids else None
        self._registration_cache: list | None = None
        self._registration_signature = None
        self._registration_at = 0.0
        self._registration_ttl = 5.0
        self._registration_failures: list[dict] = []

    # -- providers ---------------------------------------------------------
    def service_ids(self) -> tuple[str, ...]:
        """Current catalog ids plus the pseudo services.

        Recomputed on every read so ``refresh_service_directory`` is visible
        without a process restart; the catalog files are small.
        """
        if self._service_ids is not None:
            return self._service_ids
        from .. import catalog
        ids: list[str] = []
        try:
            for row in catalog.load_services():
                identifier = row.get('id') if isinstance(row, dict) else None
                if isinstance(identifier, str) and identifier and identifier not in ids:
                    ids.append(identifier)
        except Exception:
            ids = []
        for fallback in ('info', 'learn', 'library', 'sports', 'campus'):
            if fallback not in ids:
                ids.append(fallback)
        from .. import local_ops
        if local_ops.SERVICE_ID not in ids:
            ids.append(local_ops.SERVICE_ID)
        return tuple(ids)

    def submissions(self):
        """Build the 5.3 boundary once; a failure stays a capability gap."""
        if self._submissions_attempted:
            return self._submissions
        self._submissions_attempted = True
        factory = self._submissions_factory
        if factory is None:
            from ..submissions import Submissions
            def factory():
                return Submissions(account_provider=self._account)
        try:
            self._submissions = factory()
        except Exception as error:
            # The boundary registers its action contract in its constructor; a
            # failure here is reported, never replaced by a fake provider.
            self._submissions = None
            self._submissions_failure = getattr(error, 'code', None) or type(error).__name__
        return self._submissions

    def _account(self) -> str:
        identity = self.owner.bridge('action_identity')
        account = identity.get('account_id') if isinstance(identity, dict) else None
        if type(account) is int:
            return str(account)
        if isinstance(account, str) and account:
            return account
        raise CampusError('ACTION_ACCOUNT_MISMATCH')

    def providers(self) -> reg.Providers:
        return reg.Providers(
            actions=getattr(self.owner, 'actions', None),
            queries=getattr(self.owner, 'queries', None),
            todos=getattr(self.owner, 'todos', None),
            submissions=self.submissions(),
            resources=self.resources,
            media=self.media,
            academics=self.academics,
        )

    # -- registration ------------------------------------------------------
    def registrations(self, service_id: str | None = None) -> list[reg.Registration]:
        entries = self._cached_registrations()
        if service_id is None:
            return entries
        return [entry for entry in entries if entry.service_id == service_id]

    def _cached_registrations(self) -> list[reg.Registration]:
        """Rebuild only when a switch or the catalog changed."""
        from ..queries import describe as describe_queries
        service_ids = self.service_ids()
        signature = (tuple(sorted((key, str(value)) for key, value in self.env.items()
                                  if key.startswith(('THU_', 'OPENTHU_')))),
                     len(service_ids), tuple(service_ids[:3]))
        now = time.monotonic()
        if (self._registration_cache is not None and signature == self._registration_signature
                and now - self._registration_at < self._registration_ttl):
            return self._registration_cache
        entries, failures = reg.registrations(self.providers(), service_ids=service_ids,
                                              describe=describe_queries, env=self.env)
        self._registration_cache = entries
        self._registration_failures = failures
        self._registration_signature = signature
        self._registration_at = now
        return entries

    def capability(self, service_id: str, operation: str) -> reg.Registration | None:
        for entry in self.registrations():
            if entry.service_id == service_id and entry.operation == operation:
                return entry
        return None

    # -- description -------------------------------------------------------
    def describe(self, service_id: str | None = None) -> dict:
        if service_id is not None:
            return self.describe_service(service_id)
        entries = self.registrations()
        services = sorted({entry.service_id for entry in entries})
        state = {'states': list(reg.STATES), 'services': {}}
        for name in services:
            rows = [entry for entry in entries if entry.service_id == name]
            state['services'][name] = {
                'reads': sum(1 for row in rows if row.kind == 'READ'),
                'writes': sum(1 for row in rows if row.kind == 'CAMPUS_WRITE'),
                'available': sum(1 for row in rows if row.state == 'AVAILABLE'),
                'blocked': sum(1 for row in rows if row.state != 'AVAILABLE'),
                'state': _aggregate(rows),
            }
        return {
            'version': VERSION,
            'methods': {name: _METHOD_DOC[name] for name in METHODS},
            'local_methods': {name: _LOCAL_DOC[name] for name in LOCAL_METHODS},
            'tool_count': 29,
            'legacy_tools_unchanged': True,
            'services': state['services'],
            'states': state['states'],
            'blockers': [entry.public() for entry in entries if entry.blocking],
            'registration_failures': list(self._registration_failures),
            'live_probe': False,
            'note': '能力是否存在由提供者、写开关和来源契约共同决定；代码存在不构成可用证据。',
        }

    def describe_service(self, service_id: str) -> dict:
        from ..queries import describe as describe_queries
        entries = self.registrations(service_id)
        known = {entry.service_id for entry in self.registrations()} | set(self.service_ids())
        if service_id not in known:
            raise FacadeError('FACADE_SERVICE_UNKNOWN')
        if service_id not in set(self.service_ids()) and entries:
            # A facade-only service (for example 4.1 academics) has no catalog
            # entry; describe it from its own registrations instead of raising.
            base = {'service_id': service_id, 'operations': [], 'actions': [],
                    'registry_version': VERSION, 'catalog_registered': False,
                    'note': '该服务不在校园服务目录中，描述来自能力登记表。'}
        else:
            try:
                base = describe_queries(service_id)
            except CampusError:
                raise FacadeError('FACADE_SERVICE_UNKNOWN') from None
        by_operation = {(entry.service_id, entry.operation): entry.public() for entry in entries}
        operations = []
        for operation in base.get('operations') or []:
            name = operation.get('operation')
            operations.append({**operation,
                               'facade': by_operation.get((service_id, name), {'state': 'UNKNOWN'})})
        actions = []
        for action in base.get('actions') or []:
            name = action.get('operation')
            actions.append({**action,
                            'facade': by_operation.get((service_id, name), {'state': 'UNKNOWN'})})
        if not operations and entries:
            # Registry-only services still expose their operations with the
            # public capability block, so a client never sees an empty shell.
            operations = [{'operation': entry.operation, 'category': 'read',
                           'pagination': 'local_snapshot', 'adapter_version': entry.adapter_version,
                           'facade': by_operation[(entry.service_id, entry.operation)]}
                          for entry in entries if entry.kind == 'READ']
        capabilities = [entry.public() for entry in entries if entry.kind != 'READ']
        return {
            **base,
            'operations': operations,
            'actions': actions,
            'facade': {
                'version': VERSION,
                'service_domain': source_state.service_domain(service_id),
                'methods': list(METHODS),
                'capabilities': capabilities,
                'blockers': [entry.public() for entry in entries if entry.blocking],
                'live_probe': False,
                'registration_rule': '缺提供者、写开关关闭或来源契约未验证的能力不进入默认可用列表。',
            },
        }

    # -- frozen methods ----------------------------------------------------
    def query(self, service_id: str, operation: str, parameters: dict | None = None,
              cursor: str | None = None, page_size: int = 50) -> dict:
        parameters = {} if parameters is None else parameters
        if isinstance(operation, str) and operation in WRITE_OPERATIONS:
            raise FacadeError('FACADE_WRITE_NOT_REGISTERED', detail='WRITE_ON_READ_ENTRY')
        entry = self.capability(service_id, operation)
        if entry is None:
            # No fallback to QueryEngine: the registration table is the read
            # authority.  The legacy code keeps old clients' error handling.
            raise CampusError('UNREGISTERED_QUERY_OPERATION')
        reg.require(entry, write=False)
        _reject_proxy_fields(parameters, allow_url=entry.service_id == 'campus'
                             and entry.operation == 'page')
        if not entry.executable:
            raise gap('FACADE_CAPABILITY_UNAVAILABLE', entry)
        result = self.owner.queries.query(service_id=service_id, operation=operation,
                                          parameters=parameters, cursor=cursor,
                                          page_size=page_size)
        return self._stamp(result, service_id, operation, 'READ', entry)

    def health(self, service_id: str | None = None) -> dict:
        """Report capability state without contacting any source.

        One service whose description fails is recorded as ``UNKNOWN`` and does
        not stop the other services from being reported.
        """
        checked = source_state.now_iso()
        entries = self.registrations()
        known = set(self.service_ids()) | {entry.service_id for entry in entries}
        if service_id is not None:
            if not isinstance(service_id, str) or service_id not in known:
                raise FacadeError('FACADE_SERVICE_UNKNOWN')
            selected = [service_id]
        else:
            selected = sorted(known)
        rows, warnings = [], []
        for name in selected:
            try:
                rows.append(self._health_row(name, entries, checked))
            except Exception:
                warnings.append(f'{name}:HEALTH_PROBE_FAILED')
                rows.append({'service_id': name, 'state': 'UNKNOWN', 'read_operations': 0,
                             'write_operations': 0, 'available': 0, 'blocked': 0,
                             'blockers': ['HEALTH_PROBE_FAILED'], 'checked_at': checked,
                             'warnings': ['HEALTH_PROBE_FAILED']})
        aggregates = {row['state'] for row in rows}
        known_states = {item for item in aggregates if item != 'UNKNOWN'}
        if not rows:
            status = 'UNKNOWN'
        elif aggregates == {'AVAILABLE'}:
            status = 'OK'
        elif 'AVAILABLE' in aggregates or 'PARTIAL' in aggregates:
            status = 'PARTIAL'
        elif not known_states:
            status = 'UNKNOWN'
        elif known_states == {'BLOCKED'} or 'BLOCKED' in known_states:
            status = 'BLOCKED'
        elif 'DISABLED' in known_states:
            status = 'DISABLED'
        else:
            status = 'MISSING'
        return {'status': status, 'checked_at': checked, 'services': rows,
                'warnings': sorted(warnings), 'live_probe': False,
                'probe': 'STATIC_REGISTRATION_ONLY',
                'note': '健康状态来自本地能力登记；不代表已登录、来源可读或业务可用。',
                'facade': {'version': VERSION, 'request_id': current_id(), 'kind': 'READ'}}

    def prepare(self, service_id: str, operation: str, parameters: dict) -> dict:
        _reject_proxy_fields(parameters)
        entry = reg.require(self.capability(service_id, operation), write=True)
        if entry.state == 'BLOCKED' and not entry.prepare_allowed:
            # An unverified source contract is not a plan; the legacy
            # ``ADAPTER_REQUIRED`` preview path stays available for operations
            # without an adapter so old clients keep their plan shape.
            raise gap('FACADE_CAPABILITY_UNAVAILABLE', entry)
        if entry.state == 'DISABLED' and entry.reason:
            # The switch code is already registered and is more useful than a
            # generic gap; nothing is dispatched either way.
            raise FacadeError(entry.reason, capability=entry.public())
        if service_id == 'learn':
            provider = self.submissions()
            if provider is None:
                raise gap('FACADE_CAPABILITY_UNAVAILABLE', entry)
            result = provider.prepare(**parameters)
        else:
            result = self.owner.actions.prepare(service_id, operation, parameters)
        return self._stamp(result, service_id, operation, 'CAMPUS_WRITE', entry)

    def execute(self, plan_id: str, authorization: str) -> dict:
        record = self._record(plan_id)
        service, operation = record.get('service_id'), record.get('operation')
        entry = reg.require(self.capability(service, operation), write=True)
        if entry.state == 'BLOCKED':
            # No preflight, no adapter call and no browser page is touched for a
            # write whose source contract is unverified.
            raise gap('FACADE_CAPABILITY_UNAVAILABLE', entry)
        # AVAILABLE, DISABLED and MISSING delegate: the engine keeps its own
        # exact refusal (LIBRARY_WRITES_DISABLED / ADAPTER_REQUIRED) and never
        # dispatches without an adapter, a switch and trusted evidence.
        engine = self._engine_for(record)
        return self._stamp(engine.execute(plan_id, authorization), service, operation,
                           'CAMPUS_WRITE', entry)

    def result(self, operation_id: str) -> dict:
        record = self._record(operation_id)
        service, operation = record.get('service_id'), record.get('operation')
        entry = self.capability(service, operation)
        engine = self._engine_for(record)
        return self._stamp(engine.result(operation_id), service, operation, 'READ', entry)

    def cancel(self, request_id: str, reason: str | None = None) -> dict:
        value = self.requests.cancel(request_id, reason=reason)
        # Media staging is bound to a real in-flight request only; cancelling an
        # unknown or finished id must not touch an unrelated task of the same name.
        value['media_cleanup'] = self._cancel_media(request_id, value.get('state'))
        value['remote_effect'] = 'NONE'
        value['facade'] = {'version': VERSION, 'request_id': str(request_id),
                           'kind': 'LOCAL_ONLY'}
        return value

    # -- local, explicitly non-campus operations ---------------------------
    def export_agenda(self, path, *, start_date=None, end_date=None, services=None,
                      format='ics', overwrite=False, allow_partial=False) -> dict:
        entry = self.capability(_LOCAL, 'agenda')
        if entry is None or entry.state != 'AVAILABLE':
            raise gap('FACADE_CAPABILITY_UNAVAILABLE',
                      entry or {'service_id': _LOCAL, 'operation': 'agenda', 'state': 'MISSING'})
        if not isinstance(path, (str, os.PathLike)):
            raise CampusError('EXPORT_DESTINATION_REQUIRED')
        if type(overwrite) is not bool or type(allow_partial) is not bool:
            raise CampusError('INVALID_ARGUMENT_TYPE')
        if format not in ('ics', 'markdown', 'json'):
            raise CampusError('EXPORT_FORMAT_UNSUPPORTED')
        # Absolute-path, overwrite and existing-target rules stay owned by 5.4's
        # calendar_export; the facade only refuses an unusable call shape.
        result = self.owner.todos.export_agenda(
            path, start_date=start_date, end_date=end_date, services=services,
            format=format, overwrite=overwrite, allow_partial=allow_partial)
        return self._stamp(result, _LOCAL, 'agenda_export', 'LOCAL_WRITE', entry)

    # -- internals ---------------------------------------------------------
    def _stamp(self, result, service_id, operation, kind, entry=None) -> dict:
        if not isinstance(result, dict):
            raise FacadeError('FACADE_RESULT_INVALID')
        out = dict(result)
        existing = out.get('facade') if isinstance(out.get('facade'), dict) else {}
        out['facade'] = {
            **existing,
            'version': VERSION,
            'request_id': current_id(),
            'service_id': service_id,
            'operation': operation,
            'kind': kind,
            'service_domain': source_state.service_domain(service_id),
            'capability_state': entry.state if entry is not None else 'UNKNOWN',
            # A query reads its source; prepare/execute/result/cancel do not
            # perform a discovery probe.  ``live_probe`` stays reserved for the
            # static health/describe report.
            'probe': 'SOURCE_READ' if kind == 'READ' else 'NONE',
        }
        return out

    def _record(self, plan_id: str) -> dict:
        stores = []
        actions = getattr(self.owner, 'actions', None)
        if actions is not None and getattr(actions, 'store', None) is not None:
            stores.append(actions.store)
        provider = self.submissions()
        if provider is not None and getattr(provider, 'store', None) is not None:
            stores.append(provider.store)
        if not stores:
            raise FacadeError('FACADE_CAPABILITY_UNAVAILABLE',
                              capability={'service_id': 'local', 'operation': 'action',
                                           'state': 'MISSING', 'reason': 'ACTION_ENGINE_UNAVAILABLE'})
        for store in stores:
            try:
                return store.read(plan_id)
            except CampusError as error:
                if error.code != 'ACTION_NOT_FOUND':
                    raise
        raise CampusError('ACTION_NOT_FOUND')

    def _engine_for(self, record: dict):
        if record.get('service_id') == 'learn':
            provider = self.submissions()
            engine = getattr(provider, 'engine', None) if provider is not None else None
            if engine is None:
                raise FacadeError('FACADE_CAPABILITY_UNAVAILABLE',
                                  capability={'service_id': 'learn', 'operation': 'submit_assignment',
                                               'state': 'MISSING',
                                               'reason': self._submissions_failure or 'SUBMISSION_PROVIDER_UNAVAILABLE'})
            return engine
        actions = getattr(self.owner, 'actions', None)
        if actions is None:
            raise FacadeError('FACADE_CAPABILITY_UNAVAILABLE',
                              capability={'service_id': record.get('service_id'),
                                           'operation': record.get('operation'),
                                           'state': 'MISSING', 'reason': 'ACTION_ENGINE_UNAVAILABLE'})
        return actions

    def _health_row(self, service_id: str, entries, checked: str) -> dict:
        rows = [entry for entry in entries if entry.service_id == service_id]
        reads = [entry for entry in rows if entry.kind == 'READ']
        writes = [entry for entry in rows if entry.kind == 'CAMPUS_WRITE']
        blockers = sorted({f'{entry.operation}:{entry.reason}' for entry in rows
                           if entry.blocking and entry.reason})
        warnings = []
        if not rows:
            warnings.append('NO_REGISTERED_CAPABILITY')
        return {
            'service_id': service_id,
            'state': _aggregate(rows),
            'read_operations': len(reads),
            'write_operations': len(writes),
            'available': sum(1 for entry in rows if entry.state == 'AVAILABLE'),
            'blocked': sum(1 for entry in rows if entry.blocking),
            'blockers': blockers,
            'checked_at': checked,
            'warnings': warnings,
            'live_probe': False,
        }

    def _cancel_media(self, request_id, state: str | None) -> str:
        # Only an in-flight request has staging to clean.  A queued request never
        # started, and an unknown/finished id must not cancel an unrelated task.
        if state not in ('RUNNING', 'CANCELLATION_REQUESTED'):
            return 'NOT_APPLICABLE'
        if self.media is None:
            return 'UNAVAILABLE'
        cancel = getattr(self.media, 'cancel_task', None)
        if not callable(cancel):
            return 'UNAVAILABLE'
        try:
            cancel(str(request_id))
        except Exception:
            return 'FAILED'
        return 'CANCELLED'


_LOCAL = 'local'

_METHOD_DOC = {
    'query': {'side_effect': 'NONE', 'authorization': 'NONE',
              'note': '只执行已登记的只读操作；写字段、认证头和任意 HTTP 方法一律拒绝。'},
    'health': {'side_effect': 'NONE', 'authorization': 'NONE',
               'note': '只报告本地能力登记，不探测真实站点。'},
    'prepare': {'side_effect': 'LOCAL_ENCRYPTED_PLAN_WRITE', 'authorization': 'NONE',
                'note': '准备不等于提交；计划默认 600 秒后失效。'},
    'execute': {'side_effect': 'CAMPUS_WRITE_AFTER_PREFLIGHT_AND_HOST_AUTHORIZATION',
                'authorization': 'TRUSTED_HOST_AUTHORIZATION',
                'note': 'authorization 文本不是同意证据；未知结果只核对，禁止自动重发。'},
    'result': {'side_effect': 'REMOTE_READ_ONLY_RECONCILIATION', 'authorization': 'NONE',
               'note': '只读取并核对，不重新派发。'},
    'cancel': {'side_effect': 'LOCAL_ONLY', 'authorization': 'NONE',
               'note': '只取消本地排队或执行中的请求；不撤回校园业务写入。'},
}
_LOCAL_DOC = {
    'describe': {'side_effect': 'NONE', 'note': '登记清单、能力状态与阻塞项。'},
    'describe_service': {'side_effect': 'NONE', 'note': '单个服务的只读操作、写操作与能力缺口。'},
    'export_agenda': {'side_effect': 'LOCAL_FILE_WRITE', 'authorization': 'NONE',
                      'note': '只在本地写文件，必须给出绝对路径；不是消息发送。'},
}


def _aggregate(rows) -> str:
    if not rows:
        return 'UNKNOWN'
    states = {row.state for row in rows}
    if states == {'AVAILABLE'}:
        return 'AVAILABLE'
    if 'AVAILABLE' in states:
        return 'PARTIAL'
    if 'DISABLED' in states:
        return 'DISABLED'
    if 'BLOCKED' in states:
        return 'BLOCKED'
    return 'MISSING'


def _reject_proxy_fields(parameters, depth: int = 0, seen: list | None = None,
                         allow_url: bool = False) -> None:
    """Refuse a read/write payload that tries to become a generic HTTP proxy.

    ``url`` is accepted only for the registered ``campus.page`` read, whose host
    is validated by the gateway; every other operation must use a service id.
    """
    if depth > 5:
        raise FacadeError('FACADE_QUERY_CANNOT_PROXY')
    if seen is None:
        seen = []
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            folded = key.strip().casefold() if isinstance(key, str) else ''
            if folded in RESERVED_KEYS or (folded == 'url' and not allow_url
                                           and depth == 0):
                raise FacadeError('FACADE_QUERY_CANNOT_PROXY', detail=str(key)[:40])
            seen.append(key)
            if len(seen) > _MAX_SCAN:
                raise FacadeError('FACADE_QUERY_CANNOT_PROXY')
            _reject_proxy_fields(value, depth + 1, seen, allow_url)
    elif isinstance(parameters, (list, tuple)):
        for value in parameters:
            seen.append(value)
            if len(seen) > _MAX_SCAN:
                raise FacadeError('FACADE_QUERY_CANNOT_PROXY')
            _reject_proxy_fields(value, depth + 1, seen, allow_url)
