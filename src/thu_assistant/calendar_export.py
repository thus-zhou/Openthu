"""Deterministic local exports of the unified agenda (module 5.4).

The export is a pure transformation of already-normalised agenda items.  It
never re-reads a school service, never fills an unknown field and never
publishes through a message channel.

Rules:

* a scope (inclusive start/end date, optional source list and label) must be
  chosen explicitly; there is no "export everything" default;
* iCalendar output follows RFC 5545 escaping, 75-octet UTF-8-safe folding and
  the ``Asia/Shanghai`` timezone; a date-only deadline stays ``VALUE=DATE``
  and is never padded to midnight;
* the UID is stable across exports, the sequence comes from the incremental
  snapshot and an explicit cancellation stays ``STATUS:CANCELLED``, so
  re-importing a newer file updates the same event instead of duplicating it;
* remote titles never become a path, a formula, HTML or a script.  Markdown
  and JSON exports use sanitised fields and an https-only source link;
* a write goes to a temporary file in the target directory, is fsynced and is
  atomically renamed, so a failure does not leave a half file that looks
  complete.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import events, source_state
from .bridge import CampusError

VERSION = 'calendar-export-v1'
PRODID = '-//Openthu//Calendar Export v1//EN'
TZ = timezone(timedelta(hours=8))
TZID = 'Asia/Shanghai'

_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_UID = re.compile(r'^[A-Za-z0-9_.:+-]{1,200}(?:@[A-Za-z0-9_.-]{1,80})?$')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_FORMULA = re.compile(r'^[=+\-@\t\r]')


def _text(value, limit=500):
    return events.clean_text(value, limit)


def validate_scope(scope):
    if not isinstance(scope, dict) or not scope:
        raise CampusError('EXPORT_SCOPE_REQUIRED')
    start, end = scope.get('start_date'), scope.get('end_date')
    if not isinstance(start, str) or not _DATE.fullmatch(start):
        raise CampusError('EXPORT_SCOPE_INVALID')
    if not isinstance(end, str) or not _DATE.fullmatch(end):
        raise CampusError('EXPORT_SCOPE_INVALID')
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise CampusError('EXPORT_SCOPE_INVALID') from None
    if first > last:
        raise CampusError('EXPORT_SCOPE_INVALID')
    sources = scope.get('sources')
    if sources is not None:
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 16
                or any(not isinstance(item, str) or not item for item in sources)
                or len(set(sources)) != len(sources)):
            raise CampusError('EXPORT_SCOPE_INVALID')
    label = scope.get('label')
    if label is not None and (not isinstance(label, str) or len(label) > 120):
        raise CampusError('EXPORT_SCOPE_INVALID')
    return {
        'start_date': start,
        'end_date': end,
        'sources': list(sources) if sources else None,
        'label': _text(label, 120) if label else None,
    }


def _date_span(item):
    time_value = item.get('time') if isinstance(item.get('time'), dict) else {}
    if time_value.get('kind') == 'DATE':
        value = time_value.get('date')
        if isinstance(value, str) and _DATE.fullmatch(value):
            return date.fromisoformat(value), date.fromisoformat(value)
        return None
    if time_value.get('kind') == 'DATE_TIME':
        try:
            start = datetime.fromisoformat(str(time_value.get('start')).replace('Z', '+00:00'))
            end = (datetime.fromisoformat(str(time_value.get('end')).replace('Z', '+00:00'))
                   if time_value.get('end') else start)
        except (ValueError, TypeError):
            return None
        if not start.tzinfo:
            start = start.replace(tzinfo=TZ)
        if not end.tzinfo:
            end = end.replace(tzinfo=TZ)
        return start.astimezone(TZ).date(), end.astimezone(TZ).date()
    return None


def select_events(items, scope):
    """Select events inside the explicit scope; excluded ids are reported."""
    scope = validate_scope(scope)
    first, last = date.fromisoformat(scope['start_date']), date.fromisoformat(scope['end_date'])
    included, excluded = [], []
    seen = set()
    items = list(items or ())
    for item in items:
        if not isinstance(item, dict) or item.get('calendar_valid') is not True:
            raise CampusError('EXPORT_EVENT_INVALID')
        uid = item.get('event_uid')
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            raise CampusError('EXPORT_EVENT_INVALID')
        if uid in seen:
            raise CampusError('EXPORT_DUPLICATE_UID')
        seen.add(uid)
        span = _date_span(item)
        if span is None or span[1] < first or span[0] > last:
            excluded.append(uid)
            continue
        if scope['sources'] is not None and item.get('source_id') not in scope['sources']:
            excluded.append(uid)
            continue
        included.append(item)
    included.sort(key=lambda item: (
        item.get('time', {}).get('start') or item.get('time', {}).get('date') or '9999-12-31',
        item.get('event_uid') or ''))
    return {'scope': scope, 'included': included, 'excluded': excluded}


def _ics_escape(value):
    text = _text(value, 4000)
    return (text.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,')
            .replace('\n', '\\n'))


def _fold(line):
    """Fold one logical line at 75 octets without splitting a UTF-8 character."""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    chunks, first = [], True
    while raw:
        limit = 75 if first else 74
        cut = min(limit, len(raw))
        while 0 < cut < len(raw) and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        chunks.append(raw[:cut].decode('utf-8'))
        raw = raw[cut:]
        first = False
    return '\r\n '.join(chunks)


def _utc_stamp(value):
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            parsed = None
    if not isinstance(parsed, datetime):
        parsed = datetime.now(TZ)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _local_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise CampusError('EXPORT_EVENT_INVALID') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def _uid(item):
    value = item.get('ics_uid') or item.get('event_uid')
    if not isinstance(value, str) or not _UID.fullmatch(value):
        raise CampusError('EXPORT_EVENT_INVALID')
    return value if '@' in value else value + '@openthu.invalid'


def _ics_status(item):
    status = item.get('status')
    if status in ('CONFIRMED', 'TENTATIVE', 'CANCELLED'):
        return status, None
    if status == 'UNKNOWN':
        return 'TENTATIVE', 'UNKNOWN'
    raise CampusError('EXPORT_EVENT_INVALID')


def _event_lines(item, *, now):
    uid = _uid(item)
    time_value = item.get('time') if isinstance(item.get('time'), dict) else {}
    status, unknown_status = _ics_status(item)
    sequence = max(0, int(item.get('sequence') or 0))
    last_modified = _utc_stamp(item.get('last_modified') or item.get('checked_at'))
    lines = ['BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{_utc_stamp(now)}',
             f'LAST-MODIFIED:{last_modified}', f'SEQUENCE:{sequence}', f'STATUS:{status}']
    if unknown_status:
        lines.append('X-OPENTHU-CALENDAR-STATUS:UNKNOWN')
    lines.append('SUMMARY:' + _ics_escape(item.get('title') or '未命名事项'))
    if time_value.get('kind') == 'DATE':
        value = str(time_value.get('date') or '')
        if not _DATE.fullmatch(value):
            raise CampusError('EXPORT_EVENT_INVALID')
        compact = value.replace('-', '')
        lines.append(f'DTSTART;VALUE=DATE:{compact}')
        lines.append(f'X-OPENTHU-DATE-PRECISION:date')
        lines.append(f'X-OPENTHU-TIME-SEMANTICS:{_ics_escape(item.get("time_semantics") or "UNKNOWN")}')
    elif time_value.get('kind') == 'DATE_TIME':
        start = _local_datetime(time_value.get('start'))
        lines.append(f'DTSTART;TZID={TZID}:{start.strftime("%Y%m%dT%H%M%S")}')
        if time_value.get('end'):
            end = _local_datetime(time_value['end'])
            lines.append(f'DTEND;TZID={TZID}:{end.strftime("%Y%m%dT%H%M%S")}')
        lines.append('X-OPENTHU-DATE-PRECISION:' + _ics_escape(item.get('time_precision') or 'minute'))
        lines.append('X-OPENTHU-TIME-SEMANTICS:' + _ics_escape(item.get('time_semantics') or 'UNKNOWN'))
    else:
        raise CampusError('EXPORT_EVENT_INVALID')
    source = item.get('source') if isinstance(item.get('source'), dict) else {}
    lines.append('X-OPENTHU-EVENT-UID:' + _ics_escape(item.get('event_uid')))
    lines.append('X-OPENTHU-SOURCE:' + _ics_escape(item.get('source_id')))
    lines.append('X-OPENTHU-SOURCE-KIND:' + _ics_escape(source.get('kind')))
    lines.append('X-OPENTHU-SOURCE-RECORD:' + _ics_escape(source.get('record_id')))
    lines.append('X-OPENTHU-COMPLETENESS:' +
                 ('COMPLETE' if (item.get('completeness') or {}).get('complete') is True
                  else 'PARTIAL_OR_UNKNOWN'))
    if item.get('business_status'):
        lines.append('X-OPENTHU-BUSINESS-STATUS:' + _ics_escape(item.get('business_status')))
    if item.get('checked_at'):
        lines.append('X-OPENTHU-CHECKED-AT:' + _ics_escape(item.get('checked_at')))
    if item.get('location'):
        lines.append('LOCATION:' + _ics_escape(item.get('location')))
    description = _text(item.get('description'), 4000)
    url = source_state.sanitize_url(item.get('source_url'))
    if description or url:
        lines.append('DESCRIPTION:' + _ics_escape(description or ('来源：' + url)))
    if url and re.fullmatch(r'https://[^\s,;]+', url):
        lines.append('URL:' + url)
    lines.append('END:VEVENT')
    return lines


def _timezone_block():
    return [
        'BEGIN:VTIMEZONE', f'TZID:{TZID}', 'X-LIC-LOCATION:' + TZID,
        'BEGIN:STANDARD', 'DTSTART:19700101T000000', 'TZOFFSETFROM:+0800',
        'TZOFFSETTO:+0800', 'TZNAME:CST', 'END:STANDARD', 'END:VTIMEZONE',
    ]


def build_ics(items, *, scope, now=None, calendar_name='Openthu 日程'):
    """Return deterministic iCalendar text for the explicitly scoped events."""
    selected = select_events(items, scope)
    now = now or datetime.now(TZ)
    lines = [
        'BEGIN:VCALENDAR', 'VERSION:2.0', f'PRODID:{PRODID}', 'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH', 'X-WR-CALNAME:' + _ics_escape(_text(calendar_name, 120)),
        'X-WR-TIMEZONE:' + TZID,
        'X-OPENTHU-EXPORT-VERSION:' + VERSION,
        'X-OPENTHU-GENERATED-AT:' + _utc_stamp(now),
        'X-OPENTHU-EVENT-COUNT:' + str(len(selected['included'])),
        'X-OPENTHU-EXCLUDED-COUNT:' + str(len(selected['excluded'])),
        'X-OPENTHU-SCOPE:' + _ics_escape(
            f"{selected['scope']['start_date']}..{selected['scope']['end_date']}"),
        *_timezone_block(),
    ]
    for item in selected['included']:
        lines.extend(_event_lines(item, now=now))
    lines.append('END:VCALENDAR')
    return '\r\n'.join(_fold(line) for line in lines) + '\r\n'


def _md_cell(value, limit=500):
    text = _text(value, limit)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'([\\`*_\[\]#|])', r'\\\1', text)
    text = text.replace('\n', ' / ')
    if _FORMULA.match(text):
        text = "'" + text
    return text


def _md_link(url):
    safe = source_state.sanitize_url(url)
    if not safe or not safe.startswith('https://'):
        return ''
    safe = safe.replace('<', '%3C').replace('>', '%3E').replace(' ', '%20')
    return f'[来源](<{safe}>)'


def _display_time(item):
    time_value = item.get('time') if isinstance(item.get('time'), dict) else {}
    if time_value.get('kind') == 'DATE':
        return str(time_value.get('date') or '')
    if time_value.get('kind') == 'DATE_TIME':
        start = str(time_value.get('start') or '')
        end = time_value.get('end')
        return start if not end else f'{start} - {end}'
    return ''


def build_markdown(items, *, scope, now=None):
    """Return a sanitised Markdown table for the scoped events."""
    selected = select_events(items, scope)
    now = now or datetime.now(TZ)
    scope_row = selected['scope']
    lines = [
        '# 日程导出',
        '',
        f"- 范围：{scope_row['start_date']} 至 {scope_row['end_date']}（{TZID}）",
        f"- 生成时间：{now.isoformat(timespec='seconds')}",
        f"- 事件数：{len(selected['included'])}",
        '',
        '| 时间 | 事件 | 来源 | 业务状态 | 链接 |',
        '|---|---|---|---|---|',
    ]
    for item in selected['included']:
        lines.append('| {} | {} | {} | {} | {} |'.format(
            _md_cell(_display_time(item), 120),
            _md_cell(item.get('title') or '未命名事项'),
            _md_cell(item.get('source_id'), 120),
            _md_cell(item.get('business_status') or item.get('status'), 60),
            _md_link(item.get('source_url')),
        ))
    if not selected['included']:
        lines.append('| - | 该范围没有可导出事件 | - | - | - |')
    return '\n'.join(lines) + '\n'


def _summary_event(item):
    time_value = item.get('time') if isinstance(item.get('time'), dict) else {}
    source = item.get('source') if isinstance(item.get('source'), dict) else {}
    safe_url = source_state.sanitize_url(item.get('source_url'))
    return {
        'event_uid': item.get('event_uid'),
        'title': _text(item.get('title'), 500),
        'time': {
            'kind': time_value.get('kind'),
            **({'date': time_value.get('date')} if time_value.get('kind') == 'DATE' else
               {'start': time_value.get('start'), 'end': time_value.get('end')}),
            'timezone': time_value.get('timezone'),
        },
        'time_semantics': item.get('time_semantics'),
        'time_precision': item.get('time_precision'),
        'status': item.get('status'),
        'business_status': item.get('business_status'),
        'sequence': max(0, int(item.get('sequence') or 0)),
        'source': item.get('source_id'),
        'source_kind': source.get('kind'),
        'source_record_id': _text(source.get('record_id'), 240),
        'checked_at': item.get('checked_at'),
        'location': _text(item.get('location'), 300) or None,
        'source_url': safe_url if safe_url and safe_url.startswith('https://') else None,
        'warnings': sorted(value for value in item.get('warnings') or ()
                           if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,100}', value)),
    }


def build_summary(items, *, scope, now=None):
    """Return a machine-readable, sanitised summary."""
    selected = select_events(items, scope)
    now = now or datetime.now(TZ)
    summary_events = [_summary_event(item) for item in selected['included']]
    by_source, by_status = {}, {}
    for item in summary_events:
        by_source[item['source']] = by_source.get(item['source'], 0) + 1
        by_status[item['status']] = by_status.get(item['status'], 0) + 1
    return {
        'version': VERSION,
        'generated_at': now.isoformat(timespec='seconds'),
        'timezone': TZID,
        'scope': selected['scope'],
        'counts': {
            'included': len(summary_events),
            'excluded': len(selected['excluded']),
            'by_source': dict(sorted(by_source.items())),
            'by_status': dict(sorted(by_status.items())),
        },
        'events': summary_events,
        'excluded_event_uids': selected['excluded'],
        'remote_fetch': False,
        'rerun_safe': True,
    }


def _render(items, *, scope, format, now, calendar_name):
    if format in ('ics', 'ical'):
        return build_ics(items, scope=scope, now=now, calendar_name=calendar_name)
    if format in ('md', 'markdown'):
        return build_markdown(items, scope=scope, now=now)
    if format in ('json', 'summary'):
        return json.dumps(build_summary(items, scope=scope, now=now),
                          ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    raise CampusError('EXPORT_FORMAT_UNSUPPORTED')


def write_export(path, items, *, scope, format='ics', overwrite=False, now=None,
                 calendar_name='Openthu 日程'):
    """Write one export atomically to an explicit local destination."""
    if not isinstance(path, (str, os.PathLike)):
        raise CampusError('EXPORT_DESTINATION_REQUIRED')
    text = str(path)
    if not text.strip() or '\x00' in text or '\n' in text or '\r' in text:
        raise CampusError('EXPORT_DESTINATION_INVALID')
    target = Path(text).expanduser()
    if not target.is_absolute():
        raise CampusError('EXPORT_DESTINATION_INVALID')
    parent = target.parent
    if not parent.is_dir():
        raise CampusError('EXPORT_DESTINATION_INVALID')
    if target.is_symlink() or (target.exists() and target.is_dir()):
        raise CampusError('EXPORT_DESTINATION_INVALID')
    if target.exists() and not overwrite:
        raise CampusError('EXPORT_DESTINATION_EXISTS')
    items = list(items or ())
    content = _render(items, scope=scope, format=format, now=now,
                      calendar_name=calendar_name)
    payload = content.encode('utf-8')
    temp = parent / ('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temp, 'xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        raise CampusError('EXPORT_WRITE_FAILED') from None
    finally:
        temp.unlink(missing_ok=True)
    selected = select_events(items, scope)
    return {
        'version': VERSION,
        'path': str(target),
        'format': format,
        'bytes': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(),
        'event_count': len(selected['included']),
        'excluded': selected['excluded'],
        'scope': selected['scope'],
        'atomic': True,
        'overwrite': bool(overwrite),
        'remote_fetch': False,
    }


__all__ = [
    'PRODID', 'TZID', 'VERSION', 'build_ics', 'build_markdown', 'build_summary',
    'select_events', 'validate_scope', 'write_export',
]
