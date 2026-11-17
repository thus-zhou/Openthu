"""4.3 sports read contracts: session capacity, exact fees, partial order pages."""
import json

import pytest

from thu_assistant import precheck
from thu_assistant import sports as s
from thu_assistant.bridge import CampusError

CHECKED = '2026-11-17T12:00:00+08:00'
DAY = '2099-02-03'
SLOT = {'uuid': 'slot', 'beginDate': 20990203, 'endDate': 20990203,
        'beginTime': '09:00', 'endTime': '10:00', 'sceneUseType': 'SPORT_GROUP',
        'reserveStatus': {'reserveStatus': 'Y'},
        'userFeeDetails': {'chargingUnitPrice': 123456, 'payableAmount': 123456,
                           'chargingMode': 'PER_SESSION', 'payType': 3},
        'allowUserNum': 6, 'resvUserNum': 5}
SITE = {'uuid': 'court', 'sceneUuid': 'scene', 'siteName': '合成场地', 'siteType': 'DEV',
        'sessionVo': [SLOT], 'formRuleVo': None,
        'reserveRule': {'cancelBeforeStart': 10, 'cancelTime': 30}}
ORDER = {'resvUuid': 'rec', 'sceneUuid': 'scene', 'sceneName': '合成项目',
         'resvTime': {'startTime': DAY + ' 09:00:00', 'endTime': DAY + ' 10:00:00'},
         'siteVoList': [{'uuid': 'court', 'siteName': '合成场地'}],
         'resvStatus': 'RESV_EFFECT', 'resvExtendStatus': 1,
         'resvCheckStatus': 'RESV_APPROVE_PASS', 'sceneUseType': 'SPORT_GROUP',
         'orderStatus': 4, 'paidAmount': 5, 'totalAmount': 123456,
         'orderRefundStatus': 0, 'cancelBeforeStartTime': 10,
         'resvMemberVos': [{'userId': 'selfid'}]}


def site_block(site=None, raw_patch=None):
    site = site or s.field_row(SITE, 'scene', 'SPORT_GROUP', DAY, 'room')
    raw = s.result([site], s.HOME, date=DAY, scene={'scene_id': 'scene', 'name': '合成项目'},
                   room={'room_id': 'room', 'name': '房间'}, complete=True)
    raw.update(raw_patch or {})
    block = precheck.build('sports', 'availability', raw, [site],
                           {'scene_id': 'scene', 'date': DAY}, CHECKED)
    return site, block


def test_session_capacity_is_not_venue_capacity():
    site, block = site_block()
    session = site['sessions'][0]
    assert session['capacity'] == 6 and session['capacity_scope'] == 'SESSION_ALLOW_USER_NUM'
    assert session['precheck']['availability']['capacity'] == 6
    assert session['precheck']['availability']['capacity_scope'] == 'SESSION_ALLOW_USER_NUM'
    assert session['precheck']['availability']['capacity_is_slot_availability'] is True
    assert block['summary']['availability']['capacity'] is None
    assert block['summary']['availability']['capacity_scope'] == 'SITE_SESSION_LIST_NOT_VENUE_CAPACITY'
    assert block['summary']['availability']['session_count'] == 1
    assert block['summary']['availability']['available_session_count'] == 1
    assert block['summary']['preparation_state'] == 'NEEDS_ELIGIBILITY_CHECK'


def test_fee_fen_display_never_multiplies_by_one_hundred():
    site, block = site_block()
    session = site['sessions'][0]
    assert session['unit_fee_fen'] == 123456 and session['unit_fee_display'] == '1234.56'
    assert session['fee_unit'] == 'FEN'
    fee = session['precheck']['fees']['items'][0]
    assert fee['amount_fen'] == 123456 and fee['display'] == '1234.56'
    assert fee['conversion'] == 'EXACT_INTEGER_DIVIDE_BY_100'
    assert '123456.00' not in json.dumps(session, ensure_ascii=False)
    assert precheck.money(5, raw_field='x')['display'] == '0.05'
    order = s.normalize_order(ORDER)
    assert order['paid_amount_display'] == '0.05'
    assert order['total_amount_display'] == '1234.56'
    assert '123456.00' not in json.dumps(order, ensure_ascii=False)


def test_availability_keeps_scene_room_site_and_session_hierarchy():
    site, block = site_block()
    resource = site['precheck']['resource']
    assert resource['scene_id'] == 'scene' and resource['room_id'] == 'room'
    assert resource['resource_id'] == 'court'
    session = site['sessions'][0]['precheck']['resource']
    assert session['site_id'] == 'court' and session['resource_id'] == 'slot'
    assert (blocks := block['summary']['resource'])
    assert blocks['scene_id'] == 'scene' and blocks['room_id'] == 'room'


def test_same_name_room_in_two_projects_returns_candidates_without_reading_pages():
    class Reader:
        def __init__(self):
            self.calls = []

        def call(self, path, params=None):
            self.calls.append((path, params or {}))
            if path == s.SCENE:
                return {'data': [{'uuid': 'scene-a', 'sceneName': '羽毛球', 'relatedType': 'DEV'},
                                 {'uuid': 'scene-b', 'sceneName': '羽毛球', 'relatedType': 'DEV'}]}
            raise AssertionError(path)

    reader = Reader()
    out = s.availability(reader, {'name': '羽毛球', 'date': DAY})
    assert out['reason'] == 'SELECT_EXACT_SCENE'
    assert {candidate['scene_id'] for candidate in out['candidates']} == {'scene-a', 'scene-b'}
    assert [path for path, _ in reader.calls] == [s.SCENE]


def test_same_name_room_in_one_scene_is_not_picked_by_name():
    class Reader:
        def __init__(self):
            self.calls = []

        def call(self, path, params=None):
            self.calls.append((path, params or {}))
            if path == s.SCENE:
                return {'data': [{'uuid': 'scene', 'sceneName': '羽毛球', 'relatedType': 'DEV'}]}
            if path == s.LOCATION:
                if params['siteType'] == 'ROOM':
                    return {'data': [
                        {'uuid': 'room-a', 'siteType': 'ROOM', 'siteName': '一号场'},
                        {'uuid': 'room-b', 'siteType': 'ROOM', 'siteName': '一号场'}]}
                return {'data': [{'uuid': params['siteType'].lower(), 'siteType': params['siteType'],
                                  'siteName': '合成地点'}]}
            raise AssertionError(path)

    reader = Reader()
    out = s.availability(reader, {'scene_id': 'scene', 'date': DAY})
    assert out['status'] == 'NEEDS_USER_ACTION' and out['reason'] == 'SELECT_EXACT_ROOM'
    assert {candidate['room_id'] for candidate in out['candidates']} == {'room-a', 'room-b'}
    assert not any(path == s.PAGE for path, _ in reader.calls)


def test_room_precheck_rule_items_keep_source_field_unit_and_check_time():
    site, _ = site_block()
    rules = {item['raw_field']: item for item in site['precheck']['rules']['items']}
    assert rules['cancelBeforeStart']['source'] == s.HOME
    assert rules['cancelBeforeStart']['checked_at'] == CHECKED
    assert rules['cancelBeforeStart']['unit'] == 'MINUTE'
    assert rules['cancelBeforeStart']['raw_field'] == 'cancelBeforeStart'


def test_ambiguous_rule_unit_is_not_interpreted():
    site = s.field_row(SITE, 'scene', 'SPORT_GROUP', DAY, 'room')
    _, block = site_block(site)
    rules = {item['raw_field']: item for item in site['precheck']['rules']['items']}
    assert rules['cancelBeforeStart']['value'] == 10
    assert rules['cancelBeforeStart']['unit'] == 'MINUTE'
    assert rules['cancelBeforeStart']['state'] == 'DECLARED'
    assert rules['cancelTime']['value'] is None
    assert rules['cancelTime']['unit'] == 'UNKNOWN'
    assert rules['cancelTime']['state'] == 'UNIT_AMBIGUOUS'
    assert 'CANCELLATION_RULE' not in block['summary']['unknowns']


def test_same_name_scene_returns_candidates_before_any_availability_page():
    class Reader:
        def __init__(self):
            self.calls = []

        def call(self, path, params=None):
            self.calls.append(path)
            if path == s.SCENE:
                return {'data': [
                    {'uuid': 'scene-a', 'sceneName': '乒乓球', 'relatedType': 'DEV'},
                    {'uuid': 'scene-b', 'sceneName': '乒乓球', 'relatedType': 'DEV'}]}
            raise AssertionError(path)

    reader = Reader()
    out = s.availability(reader, {'name': '乒乓球', 'date': DAY})
    assert out['status'] == 'NEEDS_USER_ACTION' and out['reason'] == 'SELECT_EXACT_SCENE'
    assert [candidate['scene_id'] for candidate in out['candidates']] == ['scene-a', 'scene-b']
    assert reader.calls == [s.SCENE]
    block = precheck.build('sports', 'availability', out, [], {'name': '乒乓球'}, CHECKED)
    assert block['summary']['selection']['state'] == 'AMBIGUOUS'
    assert block['summary']['selection']['by'] == 'NAME'
    assert block['summary']['preparation_state'] == 'NEEDS_USER_ACTION'


class RecordReader:
    def __init__(self, pages, detail=None):
        self.pages = pages
        self.detail = detail

    def call(self, path, params=None):
        if path == s.RECORDS:
            return self.pages[params['pageNum']]
        if path == s.DETAIL:
            return self.detail or {'data': {'resvUuid': params['resvUuid'],
                                            'data': {'moduleData': {}, 'resvExtendStatus': 1,
                                                     'resvRecStatus': 'RESV_EFFECT'}}}
        raise AssertionError(path)


def page(rows, total):
    return {'count': total, 'data': rows}


def test_short_own_order_page_keeps_rows_and_retry_page():
    reader = RecordReader({1: page([{'resvUuid': 'a'}], 4)})
    rows, scope = s.order_page(reader, 1, 2, strict=False)
    assert [row['resvUuid'] for row in rows] == ['a']
    assert scope['complete'] is False and scope['next_page'] == 1
    assert scope['warnings'] == ['UPSTREAM_PAGE_COUNT_MISMATCH']


def test_repeated_own_order_page_stops_with_partial_rows():
    reader = RecordReader({1: page([{'resvUuid': 'a'}, {'resvUuid': 'b'}], 4),
                           2: page([{'resvUuid': 'a'}, {'resvUuid': 'b'}], 4)})
    collection = s.collect_pages(lambda number: s.order_page(reader, number, 2, strict=False),
                                 'resvUuid')
    assert [row['resvUuid'] for row in collection['rows']] == ['a', 'b']
    assert collection['complete'] is False
    assert collection['next_page'] == 2
    assert collection['warnings'] == ['REPEATED_OWN_RECORD_PAGE']


def test_missing_last_own_order_page_keeps_partial_and_next_page():
    reader = RecordReader({1: page([{'resvUuid': 'a'}, {'resvUuid': 'b'}], 4),
                           2: page([], 4)})
    collection = s.collect_pages(lambda number: s.order_page(reader, number, 2, strict=False),
                                 'resvUuid')
    assert [row['resvUuid'] for row in collection['rows']] == ['a', 'b']
    assert collection['complete'] is False
    assert collection['next_page'] == 2
    assert collection['warnings'] == ['UPSTREAM_PAGE_COUNT_MISMATCH']


def test_order_detail_keeps_a_found_record_when_the_search_is_partial():
    reader = RecordReader({1: page([ORDER], 51), 2: page([], 51)})
    found = s.own_order_read(reader, 'rec')
    assert found['state'] == 'FOUND'
    assert found['collection']['complete'] is False
    assert found['collection']['next_page'] == 1
    with pytest.raises(CampusError, match='ORDER_SEARCH_INCOMPLETE'):
        s.own_order(reader, 'rec')


def test_unknown_refund_or_approval_is_never_a_success_state():
    row = s.normalize_order({**ORDER, 'orderRefundStatus': 999,
                             'resvCheckStatus': 'RESV_NEW_SCHOOL_STATE'})
    assert row['status'] == 'UNKNOWN' and row['state_confirmed'] is False
    assert row['refund_status'] == 'UNKNOWN' and row['approval_status'] == 'RESV_NEW_SCHOOL_STATE'
    assert row['status_axes']['refund']['mapping_state'] == 'UNKNOWN'
    assert row['status_axes']['refund']['source_status_digest']
    assert row['status_axes']['approval']['mapping_state'] == 'UNKNOWN'
    assert row['payment_receipt_verified'] is False
    block = precheck.build('sports', 'orders',
                           s.result([row], s.PERSONAL, complete=True), [row], {}, CHECKED)
    assert 'REFUND_STATUS' in block['summary']['unknowns']
    assert 'APPROVAL_STATUS' in block['summary']['unknowns']


def test_known_status_axes_remain_separate_and_confirmed():
    row = s.normalize_order({**ORDER, 'orderRefundStatus': 4})
    assert row['status'] == 'SCHEDULED' and row['state_confirmed'] is True
    assert row['payment_status'] == 'PAID'
    assert row['refund_status'] == 'REFUNDING'
    assert row['status_axes']['payment']['status'] == 'PAID'
    assert row['status_axes']['refund']['status'] == 'REFUNDING'
    assert row['payment_receipt_verified'] is False
