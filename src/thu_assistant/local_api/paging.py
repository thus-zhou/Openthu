"""Bounded process-local paging for facade results that exceed ``page_size``.

A correct facade pages its own results.  This store is the safety net for a
route whose adapter returns one large list: the response is sliced here so the
dashboard never receives an unbounded payload, the remainder is held under an
opaque ``lac1`` cursor bound to session, route, parameters and page size, and
it expires with the frozen 600-second TTL.  A trailing facade cursor is carried
forward on the last slice so both paging schemes compose.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from . import contract

CURSOR_PREFIX = 'lac1'
MAX_ENTRIES = 16
MAX_BYTES = 16 * 1024 * 1024


def binding(route: str, path: Mapping[str, str], query: Mapping[str, Any],
            page_size: int, session: str, account_scope: str | None = None) -> str:
    payload = {'route': route, 'path': dict(sorted(path.items())),
               'query': dict(sorted((key, query[key]) for key in query
                                    if key not in ('cursor', 'page_size'))),
               'page_size': page_size, 'session': session,
               'account_scope': account_scope}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode('utf-8')).hexdigest()


class PageStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl: float = contract.LIMITS['cursor_ttl_seconds'],
                 max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> None:
        self._clock = clock
        self._ttl = float(ttl)
        self._max_entries = max(1, int(max_entries))
        self._max_bytes = max(1024, int(max_bytes))
        self._entries: dict[str, dict[str, Any]] = {}
        self._cursors: dict[str, tuple[str, int]] = {}
        self._lock = threading.RLock()

    def _purge(self) -> None:
        now = self._clock()
        for key in [key for key, entry in self._entries.items() if now >= entry['expires']]:
            self._drop(key)

    def _drop(self, key: str) -> None:
        self._entries.pop(key, None)
        for token in [token for token, value in self._cursors.items() if value[0] == key]:
            self._cursors.pop(token, None)

    def _slice(self, entry: Mapping[str, Any], offset: int) -> dict[str, Any]:
        items = entry['items']
        size = entry['page_size']
        end = min(offset + size, len(items))
        more = end < len(items)
        out = deepcopy(entry['base'])
        chunk = deepcopy(items[offset:end])
        if entry['had_data']:
            out['data'] = chunk
        out['items'] = chunk
        if more:
            token = entry['tokens'].get(end)
            if token is None:
                token = CURSOR_PREFIX + '.' + secrets.token_urlsafe(24)
                entry['tokens'][end] = token
                self._cursors[token] = (entry['id'], end)
            out['next_cursor'] = token
        else:
            out['next_cursor'] = entry['trailing_cursor']
        snapshot_complete = not more
        out['snapshot_complete'] = snapshot_complete
        out['complete'] = bool(entry['base'].get('complete')) and snapshot_complete \
            and not entry['trailing_cursor']
        if entry['base'].get('total_items') is None:
            out['total_items'] = len(items)
        out['returned_items'] = len(chunk)
        out['item_offset'] = offset
        return out

    def start(self, key: str, envelope: Mapping[str, Any], items: list[Any],
              page_size: int) -> dict[str, Any]:
        with self._lock:
            self._purge()
            base = deepcopy(dict(envelope))
            trailing = base.get('next_cursor')
            had_data = isinstance(base.get('data'), list) and base.get('data') == items
            base.pop('items', None)
            base.pop('data', None)
            base['next_cursor'] = None
            cost = len(json.dumps(items, ensure_ascii=False, default=str).encode('utf-8'))
            if cost > self._max_bytes:
                raise contract.LocalApiError('LOCAL_API_RESPONSE_TOO_LARGE', http_status=502,
                                             message='结果超出本地显示上限，请缩小范围。')
            while self._entries and (len(self._entries) >= self._max_entries
                                     or sum(entry['bytes'] for entry in self._entries.values())
                                     + cost > self._max_bytes):
                self._drop(next(iter(self._entries)))
            key_id = secrets.token_urlsafe(12)
            entry = {'id': key_id, 'binding': key, 'base': base, 'items': deepcopy(items),
                     'page_size': page_size, 'had_data': had_data,
                     'trailing_cursor': trailing if isinstance(trailing, str) else None,
                     'expires': self._clock() + self._ttl, 'bytes': cost, 'tokens': {}}
            self._entries[key_id] = entry
            return self._slice(entry, 0)

    def resume(self, cursor: str, key: str) -> dict[str, Any]:
        with self._lock:
            self._purge()
            if not isinstance(cursor, str) or not cursor.startswith(CURSOR_PREFIX + '.'):
                raise contract.LocalApiError('LOCAL_API_QUERY_INVALID', http_status=400,
                                             message='游标格式无效。')
            if cursor not in self._cursors:
                raise contract.LocalApiError('CURSOR_EXPIRED', http_status=400,
                                             message='分页快照已过期，请重新读取。')
            identifier, offset = self._cursors[cursor]
            entry = self._entries.get(identifier)
            if entry is None:
                raise contract.LocalApiError('CURSOR_EXPIRED', http_status=400,
                                             message='分页快照已过期，请重新读取。')
            if entry['binding'] != key:
                raise contract.LocalApiError('CURSOR_QUERY_MISMATCH', http_status=400,
                                             message='游标与当前查询条件不一致。')
            return self._slice(entry, offset)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._cursors.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
