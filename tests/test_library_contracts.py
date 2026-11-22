"""4.3 library read contracts: precheck axes, partial pages and rule units."""
from types import SimpleNamespace

import pytest

from thu_assistant import library as lib
from thu_assistant import precheck
from thu_assistant import sports as sp
from thu_assistant.bridge import CampusError
from thu_assistant.queries import QueryEngine, SNAPSHOT_VERSION, describe, read_scope_for

CHECKED = '2026-11-17T12:00:00+08:00'
SPORTS_DAY = '2099-02-03'


def wrap(value):
    return {'code': 200, 'data': value}


def detail_call(op, **params):
    if op == 'detail':
        return wrap({'pnx': {'control': {'recordid': ['rec1']}, 'display': {'title': ['同名书目']},
                            'search': {'addsrcrecordid': ['123']},
                            'links': {'linktorsrc': ['$$Uhttps://books.example/item?id=1$$Eview']}},
                     'delivery': {'deliveryCategory': ['Alma-P', 'Alma-E']}})
    if op == 'holdings':
        return wrap({'total_record_count': 1, 'holding': [
            {'suppress_from_publishing': 'false', 'holding_id': '992',
             'call_number': 'TV1', 'library': {'desc': '分馆'},
             'location': {'desc': '阅览室'}}]})
    assert op == 'copies' and params['holdingid'] == '992'
    return wrap({'total_record_count': 2, 'item': [
        {'holding_data': {'holding_id': '992'}, 'bib_data': {'mms_id': '123'},
         'item_data': {'pid': '987', 'policy': {'desc': '普通外借'}}},
        {'holding_data': {'holding_id': '992'}, 'bib_data': {'mms_id': '123'},
         'item_data': {'pid': '988', 'work_order_type': {'value': 'IN_LIBRARY'},
                       'policy': {'desc': '仅限馆内'}}}]})


def test_catalog_detail_keeps_copy_visibility_borrow_eligibility_and_electronic_access_apart():
    result = lib.catalog_detail(detail_call, {'record_id': 'rec1'})
    book = result['items'][0]
    blocks = precheck.build('library', 'catalog_detail', result, result['items'],
                            {'record_id': 'rec1'}, CHECKED)
    visible, not_borrowable = book['holdings'][0]['copies']
    assert visible['borrowable'] is None and visible['borrowable_state'] == 'UNVERIFIED'
    assert visible['borrow_eligibility'] == 'UNVERIFIED'
    assert not_borrowable['borrowable'] is False
    assert not_borrowable['precheck']['availability']['state'] == 'NOT_BORROWABLE'
    assert not_borrowable['precheck']['preparation_state'] == 'NOT_PREPARABLE'
    assert visible['precheck']['availability']['state'] == 'COPY_VISIBLE'
    assert visible['precheck']['eligibility']['state'] == 'UNVERIFIED'
    assert blocks['summary']['electronic_access']['state'] == 'LISTED'
    assert blocks['summary']['electronic_access']['off_campus_access'] == 'UNVERIFIED'
    assert blocks['summary']['electronic_access']['cookie_forwarded'] is False
    assert 'ACCOUNT_BORROW_ELIGIBILITY' in blocks['summary']['unknowns']


def test_loans_keep_date_precision_and_renewal_is_never_an_execution_result():
    payload = wrap({'data': {'loans': {'loan': [
        {'loanid': '1', 'title': '书', 'duedate': '20991231', 'renew': 'Y',
         'renewstatuses': {'renewstatus': ['可续借']}},
        {'loanid': '2', 'title': '资料', 'duedate': '20991231', 'duehour': '1530', 'renew': 'N'}],
        'showmore': ['N']}}})
    rows, more = lib.parse_loans(payload)
    assert not more
    date_only, minute = rows
    assert date_only['due_at'] == '2099-12-31' and date_only['due_precision'] == 'date'
    assert minute['due_at'] == '2099-12-31T15:30+08:00'
    result = lib.result(rows, lib.PRIMO + 'loans', pagination={'page_size': 10})
    blocks = precheck.build('library', 'loans', result, rows, {}, CHECKED)
    assert date_only['renewal_state'] == 'ELIGIBLE'
    assert date_only['renewal']['state'] == 'HINT_ONLY'
    assert date_only['renewal']['execution_attempted'] is False
    assert date_only['due_at'] == '2099-12-31'
    assert date_only['precheck']['eligibility']['state'] == 'ELIGIBLE'
    assert date_only['precheck']['eligibility']['renewal']['does_not_change_due_date'] is True
    assert minute['renewal_state'] == 'NOT_ELIGIBLE'
    assert blocks['summary']['unknowns'] == ['RENEWAL_EXECUTION', 'OVERDUE_FEE_STATE']


def test_database_guide_never_claims_institutional_or_off_campus_access():
    result = lib.database_links(lambda op, **params: wrap(
        {'mmsid': '123', 'databasename': '测试库', 'offcampus': '机构认证'}),
        {'database_id': '123'})
    entry = result['items'][0]
    assert entry['access_state'] == 'OFFICIAL_GUIDE_ONLY'
    assert entry['electronic_access']['institutional_authorization'] == 'UNVERIFIED'
    assert entry['electronic_access']['off_campus_access'] == 'UNVERIFIED'
    assert entry['electronic_access']['cookie_forwarded'] is False
    blocks = precheck.build('library', 'database_links', result, result['items'],
                            {'database_id': '123'}, CHECKED)
    assert blocks['summary']['electronic_access']['state'] == 'OFFICIAL_GUIDE_ONLY'
    assert 'INSTITUTIONAL_AUTHORIZATION' in blocks['summary']['unknowns']


@pytest.mark.parametrize('payload,code', [
    ({'code': 503}, 'MAINTENANCE'),
    ({'code': 500, 'message': '系统维护中'}, 'MAINTENANCE'),
    ({'code': 429}, 'RATE_LIMITED'),
])
def test_library_maintenance_and_rate_limit_are_not_query_results(payload, code):
    with pytest.raises(CampusError, match=code):
        lib.code_check(payload)


class Response:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_rate_limit_exposes_retry_after_without_following_it(monkeypatch):
    reader = lib.PublicReader()
    monkeypatch.setattr(reader.session, 'request',
                        lambda *args, **kwargs: Response(429, {'Retry-After': '7'}))
    try:
        with pytest.raises(CampusError) as error:
            reader.call('db_search', searchKey='synthetic')
        assert error.value.code == 'RATE_LIMITED'
        assert error.value.retry_after_ms == 7000
    finally:
        reader.session.close()


def test_library_503_is_maintenance_not_an_empty_list(monkeypatch):
    reader = lib.PublicReader()
    monkeypatch.setattr(reader.session, 'request', lambda *args, **kwargs: Response(503))
    try:
        with pytest.raises(CampusError, match='MAINTENANCE'):
            reader.call('db_search', searchKey='synthetic')
    finally:
        reader.session.close()


def test_query_envelope_carries_precheck_and_cursor_pages_are_snapshots(monkeypatch):
    account = {'account_id': 'one'}
    owner = SimpleNamespace(bridge=lambda *args: account, browsers=None)
    engine = QueryEngine(owner)
    monkeypatch.setattr(lib, 'query', lambda *args: lib.result(
        [{'loan_id': '1', 'title': '书', 'renewal_state': 'UNKNOWN'},
         {'loan_id': '2', 'title': '资料', 'renewal_state': 'UNKNOWN'}],
        lib.PRIMO + 'loans', pagination={'page_size': 10, 'complete': True}))
    first = engine.query('library', 'loans', {}, page_size=1)
    assert first['retrieval']['freshness'] == 'LIVE_AT_CHECKED_AT'
    assert first['retrieval']['current_source_read'] is True
    assert first['retrieval']['reused_snapshot'] is False
    assert first['precheck']['version'] == precheck.VERSION
    second = engine.query('library', 'loans', {}, cursor=first['next_cursor'], page_size=1)
    assert second['retrieval']['freshness'] == 'SNAPSHOT_ONLY_NOT_LIVE'
    assert second['retrieval']['current_source_read'] is False
    assert second['retrieval']['reused_snapshot'] is True
    assert second['checked_at'] == first['checked_at']
    assert second['precheck'] == first['precheck']
    assert second['items'][0]['loan_id'] == '2'


def test_disabling_library_writes_keeps_every_read_operation(monkeypatch):
    monkeypatch.setenv('THU_LIBRARY_WRITE', '0')
    monkeypatch.setenv('THU_SPORTS_WRITE', '0')
    library_ops = {row['operation'] for row in describe('library')['operations']}
    sports_ops = {row['operation'] for row in describe('sports')['operations']}
    assert {'catalog_search', 'catalog_detail', 'loans', 'database_links',
            'seat_availability', 'room_availability', 'reservations'} <= library_ops
    assert {'scenes', 'availability', 'orders', 'order_detail'} <= sports_ops
    assert not any(row['enabled'] for row in describe('library')['actions'])
    assert not any(row['enabled'] for row in describe('sports')['actions'])


def test_query_engine_precheck_keeps_the_actual_source_url(monkeypatch):
    slot = {'uuid': 'slot', 'beginDate': 20990203, 'endDate': 20990203,
            'beginTime': '09:00', 'endTime': '10:00', 'sceneUseType': 'SPORT_GROUP',
            'reserveStatus': {'reserveStatus': 'Y'},
            'userFeeDetails': {'chargingUnitPrice': 300, 'payableAmount': 300},
            'allowUserNum': 6, 'resvUserNum': 1}
    site = sp.field_row({'uuid': 'court', 'sceneUuid': 'scene', 'siteName': '场地',
                         'sessionVo': [slot],
                         'reserveRule': {'cancelBeforeStart': 10}}, 'scene',
                        'SPORT_GROUP', SPORTS_DAY, 'room')
    raw = sp.result([site], sp.HOME, date=SPORTS_DAY,
                    scene={'scene_id': 'scene', 'name': '项目'},
                    room={'room_id': 'room', 'name': '房间'}, complete=True)
    monkeypatch.setattr(sp, 'query', lambda *args: raw)
    engine = QueryEngine(SimpleNamespace(bridge=lambda *args: {'account_id': 'one'},
                                        browsers=None))
    out = engine.query('sports', 'availability', {'scene_id': 'scene', 'date': SPORTS_DAY})
    session = out['items'][0]['sessions'][0]
    assert session['precheck']['fees']['items'][0]['source'] == out['source']['url']
    assert session['precheck']['rules']['items'][0]['source'] == out['source']['url']
    assert out['items'][0]['precheck']['rules']['items'][0]['source'] == out['source']['url']


def test_selection_marks_id_bound_single_records_unique():
    item = {'record_id': 'rec1', 'title': 't'}
    search = precheck.build('library', 'catalog_search', {'complete': True}, [item],
                            {'query': 't'}, CHECKED)['summary']
    assert search['selection']['by'] == 'NONE'
    assert search['selection']['state'] == 'ALL'

    detail = {'items': [{'record_id': 'rec1', 'title': 't', 'holdings': []}],
              'complete': True}
    summary = precheck.build('library', 'catalog_detail', detail, detail['items'],
                             {'record_id': 'rec1'}, CHECKED)['summary']
    assert summary['selection']['state'] == 'UNIQUE'
    assert summary['selection']['by'] == 'ID'

    database = {'items': [{'database_id': '123', 'name': '库'}], 'complete': True}
    summary = precheck.build('library', 'database_links', database, database['items'],
                             {'database_id': '123'}, CHECKED)['summary']
    assert summary['selection']['state'] == 'UNIQUE'
    assert summary['selection']['by'] == 'ID'

    summary = precheck.build('library', 'seat_areas', {'complete': True}, [],
                             {'parent_id': '45'}, CHECKED)['summary']
    assert summary['selection']['by'] == 'ID'

    order = {'reservation_id': 'rec', 'scene_name': '项目',
             'start_at': SPORTS_DAY + 'T09:00:00+08:00',
             'end_at': SPORTS_DAY + 'T10:00:00+08:00', 'status_axes': {}}
    summary = precheck.build('sports', 'order_detail',
                             {'complete': True}, [order], {'record_id': 'rec'}, CHECKED)['summary']
    assert summary['selection']['state'] == 'UNIQUE'
    assert summary['selection']['by'] == 'ID'


def test_order_status_axes_missing_unknown_and_string_enums_are_consistent():
    base = {'resvUuid': 'rec', 'sceneUuid': 'scene', 'sceneName': 'x',
            'resvTime': {'startTime': SPORTS_DAY + ' 09:00:00',
                         'endTime': SPORTS_DAY + ' 10:00:00'},
            'siteVoList': [{'uuid': 'court'}], 'resvStatus': 'RESV_EFFECT',
            'resvExtendStatus': 1, 'resvCheckStatus': 'RESV_APPROVE_PASS',
            'orderStatus': 4, 'paidAmount': 0, 'totalAmount': 100}
    missing = sp.normalize_order(base)
    assert missing['status'] == 'UNKNOWN' and missing['state_confirmed'] is False
    assert missing['status_axes']['refund']['mapping_state'] == 'MISSING'
    summary = precheck.build('sports', 'orders', sp.result([missing], sp.PERSONAL),
                             [missing], {}, CHECKED)['summary']
    assert summary['status_axes_state'] == 'PARTIAL'
    assert 'REFUND_STATUS' in summary['unknowns']

    cancelled = sp.normalize_order({**base, 'resvExtendStatus': 3, 'orderRefundStatus': 999})
    assert cancelled['status'] == 'UNKNOWN' and cancelled['state_confirmed'] is False

    stringy = sp.normalize_order({**base, 'orderStatus': '4', 'orderRefundStatus': '0'})
    assert stringy['payment_status'] == 'PAID'
    assert stringy['status_axes']['payment']['status'] == 'PAID'
    assert stringy['refund_status'] == 'NONE'


def test_loan_renewal_rule_and_ambiguous_rules_are_partial():
    payload = wrap({'data': {'loans': {'loan': [
        {'loanid': '1', 'title': '书', 'duedate': '20991231', 'renew': 'Y',
         'renewstatuses': {'renewstatus': ['可续借']}}], 'showmore': ['N']}}})
    rows, _ = lib.parse_loans(payload)
    precheck.build('library', 'loans', lib.result(rows, lib.PRIMO + 'loans'), rows, {}, CHECKED)
    eligibility = rows[0]['precheck']['eligibility']
    rules = {item['raw_field']: item for item in rows[0]['precheck']['rules']['items']}
    assert eligibility['state'] == 'ELIGIBLE'
    assert rules['renew']['unit'] == 'FLAG' and rules['renew']['value'] is True
    assert rules['renew']['state'] == 'DECLARED'
    assert precheck._rules(precheck.SPORTS_RULE_UNITS, {'cancelTime': 30},
                           source=None, checked_at=CHECKED)['state'] == 'PARTIAL'


def test_retrieval_policy_distinguishes_live_reads_and_static_entries():
    catalog = precheck.retrieval('library', 'catalog_search', checked_at=CHECKED)
    assert catalog['policy'] == 'LIVE_READ_ONLY'
    assert catalog['current_source_read'] is True
    assert catalog['snapshot_version'] == SNAPSHOT_VERSION
    static = precheck.retrieval('library', 'database_links', checked_at=CHECKED)
    assert static['policy'] == 'STATIC_ENTRY_LIST'
    assert static['freshness'] == 'STATIC_ENTRY_LIST'
    assert static['current_source_read'] is False
    detail = precheck.retrieval('sports', 'order_detail', checked_at=CHECKED,
                                parameters={'record_id': 'x'})
    assert detail['policy'] == 'LIVE_ONLY'
    assert detail['current_source_read'] is True
    scenes = precheck.retrieval('sports', 'scenes', checked_at=CHECKED)
    assert scenes['policy'] == 'LIVE_READ_ONLY'


def test_read_scope_uses_adapter_pagination_windows():
    loans = read_scope_for('library', 'loans', {},
                           [{'loan_id': str(i)} for i in range(7)],
                           {'complete': True, 'pagination': {'pages_read': 3}}, CHECKED)
    assert loans['kind'] == 'FULL_LIST' and loans['pages_read'] == 3
    reservations = read_scope_for('library', 'reservations', {'system': 'seat', 'page': 2}, [],
                                  {'complete': False, 'upstream_total': 40,
                                   'pagination': {'upstream_page': 2,
                                                  'upstream_page_size': 20,
                                                  'upstream_total': 40}}, CHECKED)
    assert reservations['offset'] == 20 and reservations['limit'] == 20
    assert reservations['expected_pages'] == 2


def test_resource_uids_are_stable_across_search_and_detail():
    item = {'record_id': 'rec1', 'title': 't'}
    precheck.build('library', 'catalog_search', {'complete': True}, [item],
                   {'query': 't'}, CHECKED)
    assert item['precheck']['resource']['uid'] == 'library.catalog:RESOURCE:rec1'
    detail = {'items': [{'record_id': 'rec1', 'title': 't', 'holdings': []}], 'complete': True}
    summary = precheck.build('library', 'catalog_detail', detail, detail['items'],
                             {'record_id': 'rec1'}, CHECKED)['summary']
    assert summary['resource']['uid'] == item['precheck']['resource']['uid']
    loan = {'loan_id': '1', 'title': '书', 'renewal_state': 'UNKNOWN'}
    precheck.build('library', 'loans', lib.result([loan], lib.PRIMO + 'loans'), [loan],
                   {}, CHECKED)
    assert loan['precheck']['resource']['uid'] == 'library.loans:RENEWAL:1'
