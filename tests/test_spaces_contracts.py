"""4.3 seat and study-room read contracts: scoped capacity, partial pages, redaction."""
import json
from copy import deepcopy

import pytest

from thu_assistant import precheck
from thu_assistant import spaces as s
from thu_assistant.bridge import CampusError

CHECKED = '2026-11-17T12:00:00+08:00'
TABLE_HEADERS = ['预约号', '预约空间', '开始时间', '结束时间', '当前状态', '预约目的', '操作']


def room_fixture(**changes):
    row = {'devId': 1, 'devName': '测试间', 'kindId': 12, 'minUser': 3, 'maxUser': 10,
           'openState': 1, 'devStatus': 1,
           'openTimes': [{'openStartTime': '08:00', 'openEndTime': '22:00', 'openLimit': 1}],
           'resvInfo': [{'startTime': '2026-11-06 10:00:00', 'endTime': '2026-11-06 11:00:00',
                         'resvStatus': 2, 'trueName': 'DO_NOT_LEAK', 'logonName': 'DO_NOT_LEAK',
                         'title': 'DO_NOT_LEAK', 'phone': 'DO_NOT_LEAK'}],
           'resvRule': {'minResvTime': 30, 'maxResvTime': 240, 'cancelTime': 20,
                        'memo': '<b>官方规则</b>', 'uuid': 'DO_NOT_LEAK'}}
    row.update(changes)
    return row


def room_result(rows, **extra):
    return s.result(rows, s.ROOM + '/ic-web/reserve',
                    resource={'kind_id': '12', 'name': '研讨间'}, date='2026-11-06',
                    account_restrictions={'active_records': 0}, complete=True, **extra)


def test_seat_area_capacity_and_slot_free_count_are_never_mixed():
    seats = [{'seat_id': str(n), 'name': str(n), 'area_id': '45', 'state': 'RESERVED',
              'state_mapping': 'MAPPED', 'platform_status': 2, 'platform_label': '已预约',
              'free': False} for n in range(1, 4)]
    raw = s.result(seats, s.SEAT + '/web/area',
                   resource={'area_id': '45', 'name': 'A区', 'path': ['馆', 'A区'], 'capacity': 300,
                             'currently_enabled': True},
                   date='2026-11-06', start_time='09:00', end_time='10:00', segment_id='77',
                   free_count=0, slot_free_count=0, area_capacity=300,
                   capacity_scope='AREA_TOTAL_SEATS_NOT_SLOT',
                   capacity_is_slot_availability=False, rules=['不可转借'],
                   rule_source=s.SEAT + '/home/web/seat/area/45', complete=True)
    block = precheck.build('library', 'seat_availability', raw, seats, {'area_id': '45', 'date': '2026-11-06'}, CHECKED)
    summary = block['summary']
    assert summary['availability']['capacity'] == 300
    assert summary['availability']['free_count'] == 0
    assert summary['availability']['capacity_scope'] == 'AREA_TOTAL_SEATS'
    assert summary['availability']['capacity_is_slot_availability'] is False
    assert summary['availability']['state'] == 'FULL'
    assert summary['resource']['capacity']['scope'] == 'AREA_TOTAL_SEATS'
    assert summary['preparation_state'] == 'NOT_PREPARABLE'
    assert summary['selection']['state'] == 'UNIQUE' and summary['selection']['by'] == 'ID'
    assert all(item['precheck']['availability']['state'] == 'FULL' for item in seats)


def test_missing_seat_capacity_stays_unknown_instead_of_zero():
    raw = s.result([], s.SEAT + '/web/area',
                   resource={'area_id': '45', 'name': 'A区', 'path': ['馆', 'A区'], 'capacity': None},
                   date='2026-11-06', start_time='09:00', end_time='10:00', segment_id='77',
                   free_count=0, complete=True)
    block = precheck.build('library', 'seat_availability', raw, [], {'area_id': '45'}, CHECKED)
    assert block['summary']['availability']['capacity'] is None
    assert block['summary']['resource']['capacity'] is None
    assert block['summary']['availability']['capacity_is_slot_availability'] is False


def test_room_can_be_free_and_not_preparable_at_the_same_time():
    row = s.room_row(room_fixture(), '2026-11-06', 2, ('09:00', '10:00'))
    assert row['interval_state'] == 'FREE_TIME_ONLY'
    assert row['party_state'] == 'PARTY_TOO_SMALL'
    block = precheck.build('library', 'room_availability', room_result([row]), [row],
                           {'kind_id': '12', 'people': 2}, CHECKED)
    item = row['precheck']
    assert item['availability']['state'] == 'FREE'
    assert item['eligibility']['state'] == 'INELIGIBLE'
    assert 'PARTY_TOO_SMALL' in item['eligibility']['reasons']
    assert item['preparation_state'] == 'NOT_PREPARABLE'
    assert block['summary']['availability']['state'] == 'LISTED'
    assert block['summary']['preparation_state'] == 'SEE_ITEMS'


def test_room_without_a_duration_rule_is_not_treated_as_unlimited():
    row = s.room_row(room_fixture(resvRule={'cancelTime': 20}), '2026-11-06', 3, ('09:00', '10:00'))
    assert row['interval_state'] == 'RULE_UNVERIFIED'
    assert row['duration_rule_state'] == 'MISSING'
    block = precheck.build('library', 'room_availability', room_result([row]), [row],
                           {'kind_id': '12'}, CHECKED)
    rules = {item['raw_field']: item for item in row['precheck']['rules']['items']}
    assert rules['minResvTime']['state'] == 'MISSING'
    assert rules['maxResvTime']['state'] == 'MISSING'
    assert row['precheck']['rules']['state'] == 'PARTIAL'
    assert row['precheck']['preparation_state'] == 'NEEDS_RULE_CHECK'
    assert 'FEE_STATE' in block['summary']['unknowns']


def test_room_rule_with_wrong_type_is_not_converted():
    row = s.room_row(room_fixture(resvRule={'limit': 'many', 'minResvTime': 30, 'maxResvTime': 240}),
                     '2026-11-06', 3, ('09:00', '10:00'))
    precheck.build('library', 'room_availability', room_result([row]), [row], {'kind_id': '12'}, CHECKED)
    rules = {item['raw_field']: item for item in row['precheck']['rules']['items']}
    assert rules['limit']['state'] == 'TYPE_MISMATCH'
    assert rules['limit']['value'] is None


def test_same_name_area_returns_candidates_instead_of_picking_one():
    rows = [{'area_id': '1', 'name': 'A区', 'path': ['一馆', 'A区']},
            {'area_id': '2', 'name': 'A区', 'path': ['二馆', 'A区']}]
    chosen, problem = s.select(rows, {'name': 'A区'}, 'area_id', 'name')
    assert chosen is None and problem['status'] == 'NEEDS_USER_ACTION'
    raw = s.result([], s.SEAT, status='NEEDS_USER_ACTION', candidates=problem['candidates'],
                   reason=problem['reason'])
    block = precheck.build('library', 'seat_availability', raw, [], {'name': 'A区'}, CHECKED)
    selection = block['summary']['selection']
    assert selection['state'] == 'AMBIGUOUS' and selection['by'] == 'NAME'
    assert [row['area_id'] for row in selection['candidates']] == ['1', '2']
    assert block['summary']['resource']['resource_id'] is None
    assert block['summary']['preparation_state'] == 'NEEDS_USER_ACTION'


def own_rows(total, page, count):
    start = (page - 1) * 20
    rows = []
    for number in range(start, min(start + count, total)):
        rows.append({'key': 'list_' + str(number + 1),
                     'cells': [str(10000 + number), '馆/区/座位', '2026-11-06 09:00:00',
                               '2026-11-06 10:00:00', '预约成功', '', '取消 查看']})
    return {'headers': TABLE_HEADERS, 'rows': rows, 'pagination': f'共 {total} 条记录',
            'current': [str(page)]}


def test_short_seat_history_page_keeps_rows_and_continuation():
    data = own_rows(40, 1, 5)
    rows, meta = s.parse_seat_reservations(data, 1, strict=False)
    assert len(rows) == 5
    assert meta['complete'] is False
    assert meta['next_page'] == 1
    assert meta['warnings'] == ['SEAT_HISTORY_PAGE_INCOMPLETE']
    assert meta['fixed_page_size'] is True and meta['upstream_page_size'] == 20
    with pytest.raises(CampusError, match='PAGINATION_CHANGED'):
        s.parse_seat_reservations(data, 1)


def test_short_room_page_keeps_rows_and_continuation():
    payload = {'code': 0, 'data': [{'resvId': 1}], 'count': 20}
    rows, meta = s.page_window(payload, 1, 10, strict=False)
    assert len(rows) == 1 and meta['complete'] is False
    assert meta['next_page'] == 1
    assert meta['warnings'] == ['UPSTREAM_PAGE_COUNT_MISMATCH']
    with pytest.raises(CampusError, match='PAGINATION_CHANGED'):
        s.page_window(payload, 1, 10)


def test_own_reservations_include_the_actual_window_status_scope_and_page_size():
    class RoomReader:
        system = 'room'

        def call(self, path, **params):
            assert path == '/ic-web/reserve/resvInfo'
            assert params['needStatus'] == 6
            return {'code': 0, 'count': 1,
                    'data': [{'resvId': 1, 'resvStatus': 2,
                              'resvDevInfoList': [{'devId': 2, 'devName': '测试间'}],
                              'resvBeginTime': 1793926800000, 'resvEndTime': 1793930400000}]}

    out = s.read_reservations(RoomReader(), {'start_date': '2026-11-06'})
    pagination = out['pagination']
    assert pagination['status_mask'] == 6
    assert pagination['status_scope'].startswith('PLATFORM_STATUS_BITMASK')
    assert pagination['date_range'] == {'start': '2026-11-06', 'end': '2026-11-06'}
    assert pagination['fixed_page_size'] is False

    class SeatReader:
        system = 'seat'

        def goto(self, path):
            self.path = path

        @property
        def page(self):
            return self

        def evaluate(self, script):
            return own_rows(1, 1, 1)

    seat = s.read_reservations(SeatReader(), {})
    assert seat['pagination']['fixed_page_size'] is True
    assert seat['pagination']['page_size'] == 20
    assert seat['pagination']['status_scope'] == 'ALL_STATUSES_OFFICIAL_TABLE'


def test_other_people_occupancy_is_redacted_to_time_and_status():
    row = s.room_row(room_fixture(), '2026-11-06', 3, ('09:00', '10:00'))
    assert row['occupancy_public']['state'] == 'REDACTED'
    assert row['occupancy_public']['items'] == [
        {'start_time': '2026-11-06T10:00:00+08:00', 'end_time': '2026-11-06T11:00:00+08:00',
         'platform_status': 2}]
    assert 'DO_NOT_LEAK' not in json.dumps(row, ensure_ascii=False)
    public = precheck.public_occupancy(row['occupied_intervals'])
    assert public['removed'] == ['identity', 'subject', 'notes']
    assert 'DO_NOT_LEAK' not in json.dumps(public, ensure_ascii=False)


def test_unknown_seat_status_keeps_a_digest_and_is_not_free():
    rows = s.seat_states({'status': 1, 'data': {'list': [
        {'id': 9, 'name': '9', 'area': 45, 'status': 99}]}}, '45')
    row = rows[0]
    assert row['state'] == 'UNKNOWN' and row['state_mapping'] == 'UNKNOWN'
    assert row['source_status_digest'] and row['free'] is None


def test_space_maintenance_rate_limit_and_login_remain_distinct():
    with pytest.raises(CampusError, match='MAINTENANCE'):
        s.check({'status': 503}, 'seat')
    with pytest.raises(CampusError, match='MAINTENANCE'):
        s.check({'code': 0, 'message': '系统维护中'}, 'room')
    with pytest.raises(CampusError, match='RATE_LIMITED'):
        s.check({'code': 429}, 'room')
    with pytest.raises(CampusError, match='AUTH_REQUIRED'):
        s.check({'status': 0, 'msg': '没有登录或登录已超时'}, 'seat')
    with pytest.raises(CampusError, match='SPACE_READ_FAILED'):
        s.check({'status': 0, 'msg': 'timeout'}, 'seat')
    with pytest.raises(CampusError, match='SPACE_RESPONSE_INVALID'):
        s.check({'status': 200, 'msg': 'parsererror'}, 'seat')


def test_empty_or_unmapped_seat_rows_are_unknown_not_full():
    raw = s.result([], s.SEAT + '/web/area',
                   resource={'area_id': '45', 'name': 'A', 'path': ['馆'], 'capacity': 100},
                   date='2026-11-06', start_time='09:00', end_time='10:00',
                   segment_id='7', free_count=0, complete=True)
    block = precheck.build('library', 'seat_availability', raw, [],
                           {'area_id': '45'}, CHECKED)['summary']
    assert block['availability']['state'] == 'UNKNOWN'
    assert block['availability']['reason'] == 'NO_SEAT_ROWS'
    assert block['preparation_state'] == 'UNKNOWN'
    assert 'SEAT_MAP_ROWS' in block['unknowns']

    seat = {'seat_id': '9', 'name': '9', 'area_id': '45', 'state': 'UNKNOWN',
            'state_mapping': 'UNKNOWN', 'platform_status': 99, 'free': None,
            'source_status_digest': 'abc123'}
    raw2 = s.result([seat], s.SEAT + '/web/area',
                    resource={'area_id': '45', 'name': 'A', 'path': ['馆'], 'capacity': 100},
                    date='2026-11-06', start_time='09:00', end_time='10:00',
                    segment_id='7', free_count=0, complete=True)
    block = precheck.build('library', 'seat_availability', raw2, [seat],
                           {'area_id': '45'}, CHECKED)
    assert block['summary']['availability']['state'] == 'PARTIAL'
    assert block['summary']['availability']['unknown_seat_count'] == 1
    assert seat['precheck']['availability']['state'] == 'UNKNOWN'
    assert seat['precheck']['preparation_state'] == 'UNKNOWN'
    assert 'SEAT_STATUS_MAPPING' in block['summary']['unknowns']


def test_seat_and_room_item_windows_are_inherited_from_the_summary():
    seat = {'seat_id': '1', 'name': '1', 'area_id': '45', 'state': 'FREE',
            'state_mapping': 'MAPPED', 'platform_status': 1, 'free': True}
    raw = s.result([seat], s.SEAT + '/web/area',
                   resource={'area_id': '45', 'name': 'A', 'path': ['馆'], 'capacity': 10},
                   date='2026-11-06', start_time='09:00', end_time='10:00',
                   segment_id='7', free_count=1, complete=True)
    precheck.build('library', 'seat_availability', raw, [seat], {'area_id': '45'}, CHECKED)
    assert seat['precheck']['window'] == {
        'date': '2026-11-06', 'start_time': '09:00', 'end_time': '10:00',
        'timezone': 'Asia/Shanghai', 'precision': 'MINUTE'}
    date_only = s.result([seat], s.SEAT + '/web/area',
                         resource={'area_id': '45', 'name': 'A', 'path': ['馆'],
                                   'capacity': 10}, date='2026-11-06', complete=True)
    precheck.build('library', 'seat_availability', date_only, [seat],
                   {'area_id': '45'}, CHECKED)
    assert seat['precheck']['window']['precision'] == 'DATE'


def test_ambiguous_room_selection_summary_needs_user_action():
    raw = s.result([], s.ROOM + '/ic-web/roomMenu', status='NEEDS_USER_ACTION',
                   candidates=[{'kind_id': '1', 'name': '研讨间'}])
    block = precheck.build('library', 'room_availability', raw, [],
                           {'name': '研讨间'}, CHECKED)['summary']
    assert block['selection']['state'] == 'AMBIGUOUS'
    assert block['preparation_state'] == 'NEEDS_USER_ACTION'
    assert block['selection']['candidate_count'] == 1


def test_candidate_truncation_reports_the_real_total():
    candidates = [{'area_id': str(i), 'name': 'A'} for i in range(30)]
    raw = s.result([], s.SEAT, status='NEEDS_USER_ACTION', candidates=candidates)
    selection = precheck.build('library', 'seat_availability', raw, [],
                               {'name': 'A'}, CHECKED)['summary']['selection']
    assert selection['candidate_count'] == 30
    assert selection['candidate_total'] == 30
    assert selection['candidates_truncated'] is True
    assert len(selection['candidates']) == 20


def test_cursor_page_updates_top_level_freshness(monkeypatch):
    from types import SimpleNamespace
    from thu_assistant.queries import QueryEngine
    from thu_assistant import source_state
    account = {'account_id': 'one'}
    engine = QueryEngine(SimpleNamespace(bridge=lambda *a: account, browsers=None))
    monkeypatch.setattr(s, 'query', lambda *args: s.result(
        [{'id': '1'}, {'id': '2'}], s.SEAT))
    first = engine.query('library', 'seat_availability', {'area_id': '1'}, page_size=1)
    second = engine.query('library', 'seat_availability', {'area_id': '1'},
                          cursor=first['next_cursor'], page_size=1)
    assert first['freshness'] == 'LIVE_AT_CHECKED_AT'
    assert second['freshness'] == 'SNAPSHOT_ONLY_NOT_LIVE'
    assert second['retrieval']['freshness'] == 'SNAPSHOT_ONLY_NOT_LIVE'
    assert source_state.public_error('ROOM_RULE_UNVERIFIED')['category'] == 'UPSTREAM_CONTRACT'
