"""Account-bound incremental snapshots over the unified agenda.

Module 5.4.  The rules implemented here:

* a snapshot is bound to the account, the selected sources, the query window
  and the canonical event version.  A different binding never compares;
* a change is ``ADDED``, ``MODIFIED``, ``CANCELLED`` (an explicit source
  cancellation), ``DELETED`` or ``SOURCE_UNAVAILABLE``/``SOURCE_INCOMPLETE``;
* a disappearance is a deletion only when the previous and the current read
  were both complete for that source.  A failed page, a disconnected source or
  a partial read never turns every event into a cancellation;
* a source that is unavailable or incomplete carries its previously verified
  events into the new baseline as historical entries, so nothing is lost and a
  later complete read can still make a real deletion decision;
* the cursor is opaque, versioned, bound to one account and scope and expires;
  last-checked timestamps are never treated as a reliable breakpoint;
* every change has a deterministic change id for external de-duplication;
* producing a change set is not delivery: no delivered/pushed state is stored.

No network, model or message call is made here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

from learn_assistant.auth import _dpapi_protect, _dpapi_unprotect

from . import source_state
from .bridge import CampusError

VERSION = 'event-snapshot-v1'
CURSOR_VERSION = 'evc1'
DEFAULT_MAX_AGE = timedelta(days=7)
DEFAULT_CURSOR_TTL = 600
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

TZ = timezone(timedelta(hours=8))
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CODE = re.compile(r'^[A-Z][A-Z0-9_]{0,100}$')

# Only semantic fields participate in a modification.  ``checked_at``,
# ``freshness`` and evidence bookkeeping change on every read and must not
# produce a new event version.
SEMANTIC_FIELDS = (
    'title', 'time', 'status', 'business_status', 'time_semantics',
    'time_precision', 'location', 'source_id',
)
CHANGE_TYPES = ('ADDED', 'MODIFIED', 'CANCELLED', 'DELETED',
                'SOURCE_UNAVAILABLE', 'SOURCE_INCOMPLETE')
SCOPE_KEYS = ('start_date', 'end_date', 'sources', 'include_completed',
              'include_overdue', 'semester', 'allow_stale', 'max_source_pages')


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def scope_digest(scope):
    """Hash a validated scope; the raw scope may contain no tokens."""
    return _digest(scope)


def validate_scope(scope):
    if not isinstance(scope, dict) or not scope:
        raise CampusError('SNAPSHOT_SCOPE_REQUIRED')
    start = scope.get('start_date')
    end = scope.get('end_date')
    if not isinstance(start, str) or not _DATE.fullmatch(start):
        raise CampusError('SNAPSHOT_SCOPE_INVALID')
    if not isinstance(end, str) or not _DATE.fullmatch(end):
        raise CampusError('SNAPSHOT_SCOPE_INVALID')
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise CampusError('SNAPSHOT_SCOPE_INVALID') from None
    if first > last:
        raise CampusError('SNAPSHOT_SCOPE_INVALID')
    sources = scope.get('sources')
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 16
            or any(not isinstance(item, str) or not item for item in sources)
            or len(set(sources)) != len(sources)):
        raise CampusError('SNAPSHOT_SCOPE_INVALID')
    return scope


def semantic_payload(item):
    return {key: item.get(key) for key in SEMANTIC_FIELDS}


def semantic_digest(item):
    return _digest(semantic_payload(item))


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def _source_state(row):
    row = row if isinstance(row, dict) else {}
    errors = []
    for error in row.get('errors') or ():
        code = error.get('code') if isinstance(error, dict) else error
        if isinstance(code, str) and _CODE.fullmatch(code):
            errors.append(code)
    return {
        'status': row.get('status') or 'UNKNOWN',
        'complete': row.get('complete') is True,
        'pages_read': row.get('pages_read') if type(row.get('pages_read')) is int else 0,
        'checked_at': source_state.normalize_timestamp(row.get('checked_at')),
        'error_codes': sorted(set(errors)),
        'warnings': sorted({value for value in row.get('warnings') or ()
                            if isinstance(value, str) and _CODE.fullmatch(value)}),
        'capability_state': row.get('capability_state'),
        'implementation_state': row.get('implementation_state'),
    }


def _source_map(statuses):
    if isinstance(statuses, dict):
        return {source: _source_state(row) for source, row in statuses.items()}
    return {row.get('source'): _source_state(row)
            for row in statuses or () if isinstance(row, dict) and row.get('source')}


def _unavailable(state):
    return (state.get('status') == 'FAILED'
            or (state.get('complete') is not True and state.get('pages_read', 0) == 0))


def _snapshot_id(events, scope_hash, canonical_version):
    rows = sorted((item.get('event_uid') or item.get('event_id'), semantic_digest(item))
                  for item in events)
    return 'snap_' + _digest({'scope': scope_hash, 'version': canonical_version,
                              'events': rows})[:32]


def _change_id(scope_hash, baseline_id, change_type, key, before=None, after=None):
    payload = [scope_hash, baseline_id, change_type, key, before or '', after or '']
    return 'evtchg_' + _digest(payload)[:32]


def _carry(item):
    carried = deepcopy(item)
    carried['freshness'] = 'HISTORICAL'
    carried['carried_forward'] = True
    completeness = carried.get('completeness') if isinstance(carried.get('completeness'), dict) else {}
    completeness = {**completeness, 'source_complete': False, 'complete': False}
    carried['completeness'] = completeness
    carried.setdefault('warnings', [])
    carried['warnings'] = sorted(set(carried['warnings']) | {'CARRIED_FORWARD_INCOMPLETE_SOURCE'})
    return carried


def _diff_fields(previous, current):
    return [key for key in SEMANTIC_FIELDS if previous.get(key) != current.get(key)]


def _known_row(known, uid):
    row = (known or {}).get(uid)
    if not isinstance(row, dict):
        return None
    sequence = row.get('sequence')
    if type(sequence) is not int or sequence < 0:
        sequence = 0
    return {'sequence': sequence, 'digest': row.get('digest')}


def _sequence_for(item, before, known):
    """Monotonic per-UID sequence, independent of a comparable baseline."""
    uid = item.get('event_uid') or item.get('event_id')
    digest = semantic_digest(item)
    remembered = _known_row(known, uid)
    remembered_sequence = remembered['sequence'] if remembered else 0
    if remembered and remembered.get('digest') == digest:
        base = max(remembered_sequence,
                   max(0, int((before or {}).get('sequence') or 0)))
        return base
    if before is not None:
        base = max(max(0, int(before.get('sequence') or 0)), remembered_sequence)
        return base + 1 if _diff_fields(before, item) else base
    return remembered_sequence + 1 if remembered else 0


def compare(previous, current, *, account, scope, now=None, canonical_version=VERSION,
            max_age=DEFAULT_MAX_AGE, known_sequences=None):
    """Compare one current agenda snapshot with the previous baseline.

    ``known_sequences`` is the account-scoped ``{uid: {'sequence', 'digest'}}``
    map.  It keeps ICS SEQUENCE monotonic across baseline rebuilds and scope
    changes.  The returned dict includes a private ``snapshot`` record for
    persistence; callers must remove it before exposing the result.
    """
    validate_scope(scope)
    now = now or datetime.now(TZ)
    if isinstance(now, str):
        parsed = _parse_time(now)
        now = parsed or datetime.now(TZ)
    if not isinstance(account, str) or not account:
        raise CampusError('SNAPSHOT_ACCOUNT_MISMATCH')
    scope_hash = scope_digest({key: scope.get(key) for key in SCOPE_KEYS})
    current_sources = _source_map(current.get('sources'))
    current_events = [item for item in current.get('events') or ()
                      if isinstance(item, dict) and item.get('calendar_valid') is True]
    baseline_state = 'CREATED'
    reasons = []
    previous_record = None
    if isinstance(previous, dict):
        if previous.get('account') != account:
            raise CampusError('SNAPSHOT_ACCOUNT_MISMATCH')
        if previous.get('canonical_version') != canonical_version:
            baseline_state = 'REBUILT'
            reasons.append('CANONICAL_VERSION_CHANGED')
        elif previous.get('scope_digest') != scope_hash:
            baseline_state = 'REBUILT'
            reasons.append('SCOPE_CHANGED')
        else:
            checked = _parse_time(previous.get('checked_at'))
            if checked is None or now - checked > max_age or checked > now + timedelta(minutes=5):
                baseline_state = 'REBUILT'
                reasons.append('BASELINE_EXPIRED')
            else:
                baseline_state = 'COMPARABLE'
                previous_record = previous
    if previous_record is None:
        previous_events = []
        previous_sources = {}
        baseline_id = None
    else:
        previous_events = [item for item in previous_record.get('events') or ()
                           if isinstance(item, dict)]
        previous_sources = previous_record.get('sources') or {}
        baseline_id = previous_record.get('snapshot_id')

    previous_by_uid = {item.get('event_uid') or item.get('event_id'): item
                       for item in previous_events}
    current_by_uid = {item.get('event_uid') or item.get('event_id'): item
                      for item in current_events}
    changes = []
    carried = []
    comparison = {}
    warnings = set()
    for source in scope.get('sources'):
        before = previous_sources.get(source) if isinstance(previous_sources, dict) else None
        after = current_sources.get(source)
        before = before if isinstance(before, dict) else None
        after = after if isinstance(after, dict) else None
        before_complete = bool(before and before.get('complete') is True)
        after_complete = bool(after and after.get('complete') is True)
        comparison[source] = {
            'previous_complete': before_complete,
            'current_complete': after_complete,
            'deletions': ('CHECKED' if before_complete and after_complete
                          else 'SUPPRESSED_NOT_COMPARABLE'),
        }
        if after is None or _unavailable(after):
            if before_complete or any(item.get('source_id') == source for item in previous_events):
                changes.append({
                    'change_id': _change_id(scope_hash, baseline_id, 'SOURCE_UNAVAILABLE',
                                            source, before=before and before.get('status')),
                    'type': 'SOURCE_UNAVAILABLE',
                    'source': source,
                    'status': after.get('status') if after else 'MISSING',
                    'error_codes': list((after or {}).get('error_codes') or []),
                    'previous_event_count': sum(1 for item in previous_events
                                                if item.get('source_id') == source),
                    'delivery_state': 'NOT_DELIVERED',
                })
            for item in previous_events:
                if item.get('source_id') == source:
                    carried.append(_carry(item))
            continue
        if not after_complete:
            warnings.add('DELETION_SUPPRESSED_SOURCE_INCOMPLETE')
            changes.append({
                'change_id': _change_id(scope_hash, baseline_id, 'SOURCE_INCOMPLETE',
                                        source, before=before and before.get('status'),
                                        after=after.get('status')),
                'type': 'SOURCE_INCOMPLETE',
                'source': source,
                'status': after.get('status'),
                'error_codes': list(after.get('error_codes') or []),
                'delivery_state': 'NOT_DELIVERED',
            })
            for item in previous_events:
                key = item.get('event_uid') or item.get('event_id')
                if item.get('source_id') == source and key not in current_by_uid:
                    carried.append(_carry(item))
    for key, item in sorted(current_by_uid.items(), key=lambda pair: str(pair[0])):
        before = previous_by_uid.get(key)
        sequence = _sequence_for(item, before, known_sequences)
        item['sequence'] = sequence
        if before is None:
            changes.append({
                'change_id': _change_id(scope_hash, baseline_id, 'ADDED', key),
                'type': 'ADDED', 'event_uid': key, 'source': item.get('source_id'),
                'sequence': sequence, 'event': deepcopy(item), 'delivery_state': 'NOT_DELIVERED',
            })
            continue
        fields = _diff_fields(before, item)
        if not fields:
            continue
        change_type = 'CANCELLED' if (item.get('status') == 'CANCELLED'
                                      and before.get('status') != 'CANCELLED') else 'MODIFIED'
        changes.append({
            'change_id': _change_id(scope_hash, baseline_id, change_type, key,
                                    before=semantic_digest(before),
                                    after=semantic_digest(item)),
            'type': change_type, 'event_uid': key, 'source': item.get('source_id'),
            'sequence': sequence, 'fields': fields,
            'event': deepcopy(item), 'delivery_state': 'NOT_DELIVERED',
        })
    deleted = 0
    for key, item in sorted(previous_by_uid.items(), key=lambda pair: str(pair[0])):
        if key in current_by_uid:
            continue
        source = item.get('source_id')
        if source not in comparison:
            warnings.add('DELETED_EVENT_OUTSIDE_SCOPE')
            continue
        if not (comparison[source]['previous_complete'] and comparison[source]['current_complete']):
            warnings.add('DELETION_SUPPRESSED_NOT_COMPARABLE')
            carried.append(_carry(item))
            continue
        deleted += 1
        changes.append({
            'change_id': _change_id(scope_hash, baseline_id, 'DELETED', key,
                                    before=semantic_digest(item)),
            'type': 'DELETED', 'event_uid': key, 'source': source,
            'previous': deepcopy(item), 'delivery_state': 'NOT_DELIVERED',
        })
    merged = {item.get('event_uid') or item.get('event_id'): deepcopy(item)
              for item in current_events}
    carried_unique = {}
    for item in carried:
        carried_unique.setdefault(item.get('event_uid') or item.get('event_id'), item)
    carried = list(carried_unique.values())
    for item in carried:
        key = item.get('event_uid') or item.get('event_id')
        merged.setdefault(key, item)
    snapshot_events = sorted(merged.values(), key=lambda item: str(item.get('event_uid')))
    snapshot_id = _snapshot_id(snapshot_events, scope_hash, canonical_version)
    checked_at = source_state.normalize_timestamp(current.get('checked_at'), now.isoformat(timespec='seconds'))
    snapshot = {
        'version': VERSION,
        'canonical_version': canonical_version,
        'account': account,
        'scope_digest': scope_hash,
        'scope': deepcopy(scope),
        'checked_at': checked_at,
        'snapshot_id': snapshot_id,
        'events': snapshot_events,
        'sources': current_sources,
    }
    changes.sort(key=lambda change: (change.get('source') or '', change.get('type') or '',
                                     change.get('event_uid') or ''))
    counts = {name.lower(): 0 for name in CHANGE_TYPES}
    for change in changes:
        counts[change['type'].lower()] = counts.get(change['type'].lower(), 0) + 1
    counts['carried_forward'] = len(carried)
    complete_all = all((current_sources.get(source) or {}).get('complete') is True
                       for source in scope.get('sources'))
    if complete_all:
        status = 'OK' if snapshot_events else 'EMPTY'
    else:
        status = 'PARTIAL'
    if status in ('EMPTY',) and changes:
        # An incomplete source can still produce changes; EMPTY only when the
        # complete snapshot has no events.
        status = 'OK'
    if baseline_state in ('CREATED', 'REBUILT'):
        changes = []
        counts.update({name.lower(): 0 for name in CHANGE_TYPES})
        counts['carried_forward'] = 0
    warnings |= ({reason for reason in reasons} if reasons else set())
    if carried:
        warnings.add('HISTORICAL_EVENTS_CARRIED_FORWARD')
    return {
        'version': VERSION,
        'status': status,
        'baseline_state': baseline_state,
        'baseline': baseline_state,
        'baseline_snapshot_id': baseline_id,
        'snapshot_id': snapshot_id,
        'checked_at': checked_at,
        'scope': deepcopy(scope),
        'changes': changes,
        'counts': counts,
        'comparison': comparison,
        'warnings': sorted(warnings),
        'source_complete': complete_all,
        'complete': complete_all,
        'delivery_state': 'NOT_DELIVERED',
        'delivery_evidence': False,
        'pushed': False,
        'snapshot': snapshot,
    }


class CursorRegistry:
    """Opaque, versioned, account- and scope-bound cursors with an expiry."""

    def __init__(self, *, ttl=DEFAULT_CURSOR_TTL, max_entries=64, clock=None):
        if type(ttl) is not int or not 1 <= ttl <= 3600:
            raise CampusError('SNAPSHOT_SCOPE_INVALID')
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock or (lambda: datetime.now(TZ))
        self.entries = {}

    def purge(self):
        now = self.clock()
        for token in [key for key, value in self.entries.items() if value['expires'] <= now]:
            self.entries.pop(token, None)

    def issue(self, *, account, scope_hash, snapshot):
        self.purge()
        token = CURSOR_VERSION + '.' + secrets.token_urlsafe(24)
        record = {**snapshot, 'events': deepcopy(list(snapshot.get('events') or ()))}
        self.entries[token] = {
            'account': account,
            'scope_digest': scope_hash,
            'snapshot': record,
            'expires': self.clock() + timedelta(seconds=self.ttl),
        }
        while len(self.entries) > self.max_entries:
            self.entries.pop(next(iter(self.entries)))
        return token

    def resolve(self, token, *, account, scope_hash):
        self.purge()
        if (not isinstance(token, str) or len(token) > 128
                or not token.startswith(CURSOR_VERSION + '.')):
            raise CampusError('EVENT_CURSOR_INVALID')
        entry = self.entries.get(token)
        if entry is None:
            raise CampusError('EVENT_CURSOR_EXPIRED')
        if entry['account'] != account or entry['scope_digest'] != scope_hash:
            raise CampusError('EVENT_CURSOR_SCOPE_MISMATCH')
        return deepcopy(entry['snapshot'])


class SnapshotStore:
    """Encrypted last-snapshot storage, never a plaintext fallback.

    The preferred backend is the 2.2 ``SecureStore`` with the reserved
    ``EVENT_CURSOR`` purpose.  When no secure key is configured, the same
    protected-file wrapper used by the existing todo cache is used; if that
    cannot encrypt either, ``save`` fails and the caller keeps an in-memory
    baseline instead of writing plaintext.
    """

    MAGIC = b'THU-EVENTS-1\0'
    SEQUENCE_SCOPE = 'sequences-v1'

    def __init__(self, root=None, *, backend=None, protect=None, unprotect=None):
        self.root = Path(root) if root else None
        self.backend = backend
        self.protect = protect or _dpapi_protect
        self.unprotect = unprotect or _dpapi_unprotect

    def record_id(self, account, scope_hash):
        return 'evsnap-' + _digest([account, scope_hash])[:40]

    def _path(self, account, scope_hash):
        root = self.root
        if root is None:
            from .catalog import state_root
            root = state_root() / 'events-v1'
        return Path(root) / (hashlib.sha256(_canonical([account, scope_hash]).encode()).hexdigest() + '.evsnap')

    def save(self, record):
        if not isinstance(record, dict):
            raise CampusError('SNAPSHOT_WRITE_FAILED')
        payload = _canonical(record).encode('utf-8')
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise CampusError('SNAPSHOT_TOO_LARGE')
        if self.backend is not None:
            self.backend.save(self.record_id(record.get('account'), record.get('scope_digest')), record)
            return
        blob = self.protect(payload)
        if not blob:
            raise CampusError('SNAPSHOT_ENCRYPT_FAILED')
        path = self._path(record.get('account'), record.get('scope_digest'))
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, 'xb') as handle:
                handle.write(self.MAGIC + blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            raise CampusError('SNAPSHOT_WRITE_FAILED') from None
        finally:
            temp.unlink(missing_ok=True)

    def load(self, account, scope_hash):
        if self.backend is not None:
            record = self.backend.load(self.record_id(account, scope_hash))
            return self._validate(record, account, scope_hash)
        path = self._path(account, scope_hash)
        if not path.exists():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            raise CampusError('SNAPSHOT_CORRUPT') from None
        if size > MAX_SNAPSHOT_BYTES:
            raise CampusError('SNAPSHOT_CORRUPT')
        blob = path.read_bytes()
        if not blob.startswith(self.MAGIC):
            raise CampusError('SNAPSHOT_CORRUPT')
        try:
            record = json.loads(self.unprotect(blob[len(self.MAGIC):]))
        except CampusError:
            raise
        except Exception:
            raise CampusError('SNAPSHOT_CORRUPT') from None
        return self._validate(record, account, scope_hash)

    @staticmethod
    def _validate(record, account, scope_hash):
        if record is None:
            return None
        if (not isinstance(record, dict) or record.get('version') != VERSION
                or record.get('account') != account
                or record.get('scope_digest') != scope_hash):
            raise CampusError('SNAPSHOT_CORRUPT')
        return record

    def load_sequences(self, account):
        record = self.load(account, self.SEQUENCE_SCOPE)
        if record is None:
            return {}
        rows = record.get('sequences') if isinstance(record.get('sequences'), dict) else {}
        return {key: value for key, value in rows.items()
                if isinstance(key, str) and isinstance(value, dict)}

    def save_sequences(self, account, sequences):
        self.save({
            'version': VERSION, 'canonical_version': VERSION, 'account': account,
            'scope_digest': self.SEQUENCE_SCOPE, 'scope': {}, 'checked_at': source_state.now_iso(),
            'snapshot_id': 'sequences', 'events': [], 'sources': {},
            'sequences': sequences,
        })


class MemoryBackend:
    """In-memory backend for tests and for callers without a secure key."""

    def __init__(self):
        self.data = {}

    def save(self, record_id, value):
        self.data[record_id] = deepcopy(value)

    def load(self, record_id):
        return deepcopy(self.data.get(record_id))


class SecureStoreBackend:
    """Adapter over the 2.2 ``SecureStore`` using the EVENT_CURSOR purpose."""

    def __init__(self, store):
        self.store = store

    def save(self, record_id, value):
        try:
            self.store.write_json(record_id, value, purpose='EVENT_CURSOR',
                                  account_domain='CAMPUS', service_domain='LOCAL')
        except Exception:
            raise CampusError('SNAPSHOT_WRITE_FAILED') from None

    def load(self, record_id):
        try:
            return self.store.read_json(record_id, purpose='EVENT_CURSOR',
                                        account_domain='CAMPUS', service_domain='LOCAL')
        except Exception as error:
            if type(error).__name__ in ('SecureRecordNotFound', 'FileNotFoundError'):
                return None
            raise CampusError('SNAPSHOT_CORRUPT') from None


def _default_backend():
    if os.environ.get('THU_EVENTS_SNAPSHOT_MEMORY') == '1':
        return MemoryBackend()
    try:
        from .secure_store import open_store_from_environment
        return SecureStoreBackend(open_store_from_environment())
    except Exception:
        return None


class IncrementalStore:
    """On-demand change query; no background polling and no delivery claims.

    The encrypted store is preferred, but a process-local snapshot and sequence
    map are always kept.  That fallback is what makes repeated ``changes()`` and
    monotonic ICS sequences work on a Linux default install without a secure key;
    it is explicitly process-local and never written as plaintext.
    """

    def __init__(self, store=None, cursors=None, now=None):
        self.store = store if store is not None else SnapshotStore(backend=_default_backend())
        self.now = now or (lambda: datetime.now(TZ))
        self.cursors = cursors or CursorRegistry(clock=self.now)
        self._memory_snapshots = {}
        self._memory_sequences = {}

    def _scope_hash(self, scope):
        return scope_digest({key: scope.get(key) for key in SCOPE_KEYS})

    def _load_previous(self, account, scope_hash):
        key = (account, scope_hash)
        if key in self._memory_snapshots:
            return self._memory_snapshots[key]
        try:
            return self.store.load(account, scope_hash)
        except CampusError as error:
            if error.code == 'SNAPSHOT_CORRUPT':
                return None
            raise

    def _load_sequences(self, account):
        if account in self._memory_sequences:
            return deepcopy(self._memory_sequences[account])
        try:
            rows = self.store.load_sequences(account)
        except CampusError:
            rows = {}
        self._memory_sequences[account] = deepcopy(rows)
        return deepcopy(rows)

    def _save_sequences(self, account, sequences):
        self._memory_sequences[account] = deepcopy(sequences)
        try:
            self.store.save_sequences(account, sequences)
        except CampusError:
            return False
        return True

    def observe(self, events, *, account):
        """Assign monotonic sequences without creating or advancing a baseline.

        Used by ``agenda()`` so a pure local export can consume correct
        SEQUENCE values even when the caller never requested changes.
        """
        known = self._load_sequences(account)
        updated = {}
        for item in events or ():
            if not isinstance(item, dict) or item.get('calendar_valid') is not True:
                continue
            uid = item.get('event_uid') or item.get('event_id')
            if not isinstance(uid, str):
                continue
            digest = semantic_digest(item)
            previous = _known_row(known, uid)
            if previous and previous.get('digest') == digest:
                sequence = previous['sequence']
            elif previous:
                sequence = previous['sequence'] + 1
            else:
                sequence = 0
            item['sequence'] = sequence
            updated[uid] = {'sequence': sequence, 'digest': digest}
        self._save_sequences(account, updated)
        return {uid: row['sequence'] for uid, row in updated.items()}

    def query(self, current, *, account, scope, cursor=None, canonical_version=VERSION,
              persist=True):
        validate_scope(scope)
        scope_hash = self._scope_hash(scope)
        now = self.now()
        previous = None
        cursor_used = False
        if cursor is not None:
            previous = self.cursors.resolve(cursor, account=account, scope_hash=scope_hash)
            cursor_used = True
        else:
            previous = self._load_previous(account, scope_hash)
        known = self._load_sequences(account)
        result = compare(previous, current, account=account, scope=scope, now=now,
                         canonical_version=canonical_version, known_sequences=known)
        snapshot = result.pop('snapshot')
        self._memory_snapshots[(account, scope_hash)] = deepcopy(snapshot)
        result['sequences'] = {item.get('event_uid') or item.get('event_id'):
                               max(0, int(item.get('sequence') or 0))
                               for item in snapshot.get('events') or ()}
        result['cursor_used'] = cursor_used
        result['baseline_source'] = 'CURSOR' if cursor_used else 'STORED'
        result['cursor_scope'] = 'PROCESS_LOCAL'
        warnings = set(result.get('warnings') or ())
        if persist:
            try:
                self.store.save(snapshot)
            except CampusError as error:
                warnings.add(error.code)
                result['baseline_persisted'] = False
            else:
                result['baseline_persisted'] = True
        else:
            result['baseline_persisted'] = False
        sequence_rows = {}
        for item in snapshot.get('events') or ():
            uid = item.get('event_uid') or item.get('event_id')
            if isinstance(uid, str):
                sequence_rows[uid] = {
                    'sequence': max(0, int(item.get('sequence') or 0)),
                    'digest': semantic_digest(item),
                }
        if not self._save_sequences(account, sequence_rows):
            warnings.add('SNAPSHOT_SEQUENCE_PERSIST_FAILED')
        result['next_cursor'] = self.cursors.issue(account=account, scope_hash=scope_hash,
                                                   snapshot=snapshot)
        result['cursor_version'] = CURSOR_VERSION
        result['cursor_ttl_seconds'] = self.cursors.ttl
        result['warnings'] = sorted(warnings)
        return result


__all__ = [
    'CHANGE_TYPES', 'CursorRegistry', 'DEFAULT_CURSOR_TTL', 'DEFAULT_MAX_AGE',
    'IncrementalStore', 'MemoryBackend', 'SEMANTIC_FIELDS', 'SecureStoreBackend',
    'SnapshotStore', 'VERSION', 'compare', 'scope_digest', 'semantic_digest',
    'semantic_payload', 'validate_scope',
]
