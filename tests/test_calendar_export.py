"""5.4 local calendar exports: iCalendar, Markdown and structured summary.

All events are synthetic.  The module under test is pure: no campus read, no
model call, no message send and no background process.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

import pytest

from thu_assistant import calendar_export, events, snapshot_diff
from thu_assistant.bridge import CampusError

NOW = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
CHECK = NOW.isoformat()
SCOPE = {'start_date': '2026-11-17', 'end_date': '2026-11-23'}


def todo_id(value):
    return 'todo_' + hashlib.sha256(str(value).encode()).hexdigest()[:32]


def item(value='1', *, title='作业', due_at='2026-11-20', start_at=None, end_at=None,
         status='TODO', precision='date', source='learn.assignments', url=None):
    return events.from_todo({
        'event_id': todo_id(source + value), 'source': source,
        'source_id': json.dumps(['course', value]), 'title': title,
        'due_at': due_at, 'start_at': start_at, 'end_at': end_at,
        'date_precision': precision, 'status': status, 'source_status': {},
        'checked_at': CHECK, 'freshness': 'LIVE_AT_CHECKED_AT', 'warnings': [],
        'source_url': url if url is not None else 'https://learn.tsinghua.edu.cn/',
    })


def unfold(text):
    return text.replace('\r\n ', '')


def parse_events(text):
    blocks = {}
    for block in text.split('BEGIN:VEVENT')[1:]:
        lines = unfold(block.split('END:VEVENT')[0]).split('\r\n')
        row = {}
        for line in lines:
            if ':' in line:
                key, value = line.split(':', 1)
                row[key] = value
        blocks[row.get('UID')] = row
    return blocks


def test_date_only_deadline_stays_date_level_and_is_not_midnight():
    text = calendar_export.build_ics([item()], scope=SCOPE, now=NOW)
    assert 'DTSTART;VALUE=DATE:20261120' in text
    assert 'T000000' not in text.split('BEGIN:VEVENT')[1].split('END:VEVENT')[0]
    assert 'DTEND' not in text
    assert 'X-OPENTHU-TIME-SEMANTICS:DEADLINE' in text
    assert 'X-OPENTHU-DATE-PRECISION:date' in text


def test_cross_midnight_interval_keeps_original_window_and_timezone():
    reservation = item('seat', source='library.seat', due_at=None,
                       start_at='2026-11-20T23:00:00+08:00',
                       end_at='2026-11-21T01:00:00+08:00', status='SCHEDULED',
                       precision='minute')
    text = calendar_export.build_ics([reservation], scope=SCOPE, now=NOW)
    assert 'DTSTART;TZID=Asia/Shanghai:20261120T230000' in text
    assert 'DTEND;TZID=Asia/Shanghai:20261121T010000' in text
    assert 'TZID:Asia/Shanghai' in text and 'TZOFFSETTO:+0800' in text


def test_utc_and_summer_offset_inputs_convert_to_shanghai():
    summer = item('summer', due_at='2026-07-01T16:00:00Z')
    assert summer['time']['kind'] == 'DATE_TIME'
    text = calendar_export.build_ics([summer], scope={'start_date': '2026-07-01',
                                                      'end_date': '2026-07-03'}, now=NOW)
    assert 'DTSTART;TZID=Asia/Shanghai:20260702T000000' in text
    offset = item('offset', due_at='2026-07-01T12:00:00+02:00')
    assert offset['time']['start'].endswith('+08:00')
    assert offset['time']['start'][:19] == '2026-07-01T18:00:00'


def test_special_characters_and_line_folding_do_not_break_structure():
    title = '=SUM(1);,<script>\n第二行\\'
    text = calendar_export.build_ics([item(title=title)], scope=SCOPE, now=NOW)
    assert text.endswith('\r\n')
    assert '\n' not in text.replace('\r\n', '')
    for line in text.split('\r\n'):
        assert len(line.encode('utf-8')) <= 75
    summary = unfold(text).split('SUMMARY:', 1)[1].split('\r\n', 1)[0]
    assert '\\;' in summary and '\\,' in summary and '\\n' in summary and '\\\\' in summary
    parsed = parse_events(text)
    assert len(parsed) == 1
    assert '第二行' in next(iter(parsed.values()))['SUMMARY']


def test_uid_is_stable_and_sequence_updates_on_change():
    first = item('1', title='旧标题')
    text_one = calendar_export.build_ics([first], scope=SCOPE, now=NOW)
    uid_one = next(iter(parse_events(text_one)))
    changed = {**first, 'title': '新标题', 'sequence': 1}
    text_two = calendar_export.build_ics([changed], scope=SCOPE, now=NOW)
    uid_two = next(iter(parse_events(text_two)))
    assert uid_one == uid_two
    assert 'SEQUENCE:0' in text_one and 'SEQUENCE:1' in text_two
    assert 'SUMMARY:新标题' in text_two


def test_cancelled_and_unknown_status_are_exported_without_fake_success():
    cancelled = item('1', status='CANCELLED')
    text = calendar_export.build_ics([cancelled], scope=SCOPE, now=NOW)
    assert 'STATUS:CANCELLED' in text
    unknown = {**item('2'), 'status': 'UNKNOWN'}
    text = calendar_export.build_ics([unknown], scope=SCOPE, now=NOW)
    assert 'STATUS:TENTATIVE' in text
    assert 'X-OPENTHU-CALENDAR-STATUS:UNKNOWN' in text
    assert 'STATUS:CONFIRMED' not in text


def test_export_requires_an_explicit_scope_and_filters_outside_events():
    with pytest.raises(CampusError, match='EXPORT_SCOPE_REQUIRED'):
        calendar_export.build_ics([item()], scope=None)
    with pytest.raises(CampusError, match='EXPORT_SCOPE_INVALID'):
        calendar_export.build_ics([item()], scope={'start_date': '2026-11-23',
                                                   'end_date': '2026-11-17'})
    outside = item('2', due_at='2026-12-20')
    selected = calendar_export.select_events([item(), outside], SCOPE)
    assert len(selected['included']) == 1
    assert len(selected['excluded']) == 1
    text = calendar_export.build_ics([item(), outside], scope=SCOPE, now=NOW)
    assert 'X-OPENTHU-EVENT-COUNT:1' in text


def test_source_filter_and_duplicate_uid_fail_closed():
    lesson = item('1', source='learn.assignments')
    loan = item('2', source='library.loans', due_at='2026-11-20')
    selected = calendar_export.select_events([lesson, loan], {**SCOPE,
                                                             'sources': ['learn.assignments']})
    assert [row['source_id'] for row in selected['included']] == ['learn.assignments']
    with pytest.raises(CampusError, match='EXPORT_DUPLICATE_UID'):
        calendar_export.build_ics([lesson, lesson], scope=SCOPE)


def test_markdown_escapes_html_markdown_and_formula_prefixes():
    nasty = item('1', title='=1+1 <script>|x\n- not a bullet # tag')
    text = calendar_export.build_markdown([nasty], scope=SCOPE, now=NOW)
    assert '<script>' not in text
    assert '&lt;script&gt;' in text
    assert '\\|' in text
    assert "'=1+1" in text
    assert '\n- not a bullet' not in text


def test_structured_summary_is_sanitised_and_has_counts():
    nasty = item('1', title='=1+1\x00 <script>', url='http://insecure.example/item')
    text = calendar_export.build_summary([nasty], scope=SCOPE, now=NOW)
    assert text['counts']['included'] == 1
    assert text['counts']['excluded'] == 0
    assert text['events'][0]['source_url'] is None
    assert '\x00' not in text['events'][0]['title']
    assert 'source_status' not in text['events'][0]
    assert text['remote_fetch'] is False
    assert text['events'][0]['event_uid'].startswith('CAMPUS:')


def test_write_is_atomic_and_refuses_overwrite_or_relative_paths(tmp_path):
    target = tmp_path / 'agenda.ics'
    first = calendar_export.write_export(target, [item()], scope=SCOPE, now=NOW)
    assert target.read_bytes().startswith(b'BEGIN:VCALENDAR')
    assert first['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert not list(tmp_path.glob('*.tmp'))
    with pytest.raises(CampusError, match='EXPORT_DESTINATION_EXISTS'):
        calendar_export.write_export(target, [item()], scope=SCOPE, now=NOW)
    second = calendar_export.write_export(target, [item(title='新')], scope=SCOPE,
                                          now=NOW, overwrite=True)
    assert 'SUMMARY:新' in target.read_text(encoding='utf-8')
    assert second['overwrite'] is True
    with pytest.raises(CampusError, match='EXPORT_DESTINATION_INVALID'):
        calendar_export.write_export('relative.ics', [item()], scope=SCOPE, now=NOW)
    with pytest.raises(CampusError, match='EXPORT_FORMAT_UNSUPPORTED'):
        calendar_export.write_export(tmp_path / 'x.pdf', [item()], scope=SCOPE,
                                     format='pdf', now=NOW)


def test_write_failure_leaves_no_half_file(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('synthetic failure')
    monkeypatch.setattr(calendar_export.os, 'replace', fail)
    with pytest.raises(CampusError, match='EXPORT_WRITE_FAILED'):
        calendar_export.write_export(tmp_path / 'broken.ics', [item()], scope=SCOPE, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_reimport_updates_same_uid_after_a_sequence_bump():
    scope = {'start_date': '2026-11-17', 'end_date': '2026-11-23',
             'sources': ['learn.assignments'], 'include_completed': False,
             'include_overdue': True, 'semester': None}
    first = item('1', title='旧')
    snapshot = {'version': snapshot_diff.VERSION, 'canonical_version': snapshot_diff.VERSION,
                'account': 'A', 'scope_digest': snapshot_diff.scope_digest(
                    {key: scope.get(key) for key in ('start_date', 'end_date', 'sources',
                                                     'include_completed', 'include_overdue',
                                                     'semester')}),
                'scope': scope, 'checked_at': CHECK, 'snapshot_id': 'snap_test',
                'events': [first],
                'sources': {'learn.assignments': {'status': 'SUCCESS', 'complete': True,
                                                   'pages_read': 1, 'checked_at': CHECK,
                                                   'error_codes': [], 'warnings': []}}}
    changed = {**item('1', title='新'), 'sequence': 1}
    diff = snapshot_diff.compare(snapshot, {'events': [changed], 'checked_at': CHECK,
                                            'sources': snapshot['sources']},
                                 account='A', scope=scope, now=NOW)
    updated = diff['changes'][0]['event']
    text = calendar_export.build_ics([updated], scope=scope, now=NOW)
    parsed = parse_events(text)
    assert len(parsed) == 1
    assert next(iter(parsed.values()))['SEQUENCE'] == '1'
    assert next(iter(parsed.values()))['UID'] == unpack_uid(
        calendar_export.build_ics([first], scope=scope, now=NOW))


def unpack_uid(text):
    return next(iter(parse_events(text)))


def test_export_never_uses_a_title_as_a_path(tmp_path):
    title = '../../etc/passwd'
    result = calendar_export.write_export(tmp_path / 'safe.ics', [item(title=title)],
                                          scope=SCOPE, now=NOW)
    assert result['path'] == str(tmp_path / 'safe.ics')
    assert 'passwd' not in result['path']
    assert 'passwd' in (tmp_path / 'safe.ics').read_text(encoding='utf-8')
