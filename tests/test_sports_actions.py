"""5.2 sports transaction tests: snapshot binding, state closure, no replay.

Every test uses a synthetic reader and a synthetic transport. No campus order
is created, cancelled or paid.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import time
from types import SimpleNamespace

import pytest

from thu_assistant import precheck
from thu_assistant import sports as s
from thu_assistant import sports_actions as a
from thu_assistant import sports_transport as net
from thu_assistant.actions import ActionEngine, RequestGate, RequestSpec
from thu_assistant.bridge import CampusError
from thu_assistant.queries import describe
from synthetic_store import make_action_store

DAY = '2099-02-03'
ARGS = {'scene_id': 'scene', 'room_id': 'room', 'site_id': 'court', 'session_id': 'slot',
        'date': DAY, 'mode': 'SPORT_GROUP', 'members': 'self', 'payment_method': 'FREE'}
AUTH = '为这个合成测试批准指定的一笔业务'
SLOT = {'uuid': 'slot', 'beginDate': 20990203, 'endDate': 20990203,
        'beginTime': '09:00', 'endTime': '10:00', 'sceneUseType': 'SPORT_GROUP',
        'reserveStatus': {'reserveStatus': 'Y'},
        'userFeeDetails': {'chargingUnitPrice': 0, 'payableAmount': 0, 'payType': 3}}
FIELD = {'uuid': 'court', 'sceneUuid': 'scene', 'siteName': '合成场地', 'siteType': 'DEV',
         'sessionVo': [SLOT], 'formRuleVo': None, 'reserveRule': {'cancelBeforeStart': 10}}
ROW = {'resvUuid': 'rec', 'sceneUuid': 'scene', 'sceneName': '合成项目',
       'resvTime': {'startTime': DAY + ' 09:00:00', 'endTime': DAY + ' 10:00:00'},
       'siteVoList': [{'uuid': 'court', 'siteName': '合成场地'}],
       'resvStatus': 'RESV_UNEFFECT', 'resvExtendStatus': 1, 'resvProperty': 0,
       'resvCheckStatus': 'RESV_APPROVE_PASS', 'sceneUseType': 'SPORT_GROUP',
       'orderStatus': 4, 'paidAmount': 0, 'totalAmount': 0,
       'orderRefundStatus': 0, 'cancelBeforeStartTime': 10,
       'resvMemberVos': [{'userId': 'selfid', 'spotsNum': 1}]}
# The canonical effective order: SCHEDULED in the read layer and the contract
# compatibility sample (RESV_EFFECT + approval pass + PAID).
SCHEDULED_ROW = {**ROW, 'resvStatus': 'RESV_EFFECT'}


class Fake:
    """One synthetic official session; every call is recorded."""

    def __init__(self):
        self.calls = []
        self.scenes = [{'uuid': 'scene', 'sceneName': '合成项目', 'relatedType': 'DEV',
                        'sceneUseType': 'SPORT_GROUP'}]
        self.fields = [deepcopy(FIELD)]
        self.rows = []
        self.records_pages = None      # optional {page: raw payload}
        self.records_error = None      # optional error code for the order list
        self.records_handler = None    # optional callable(params) for special walks
        self.detail_pay_type = 'PAY_ONLINE'
        self.errors = {}               # optional {path: error code}
        self.captcha = '0'
        self.detail_amount = 0
        self.detail_override = None
        self.session = {'account_id': 'test-account'}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def call(self, path, params=None):
        params = params or {}
        self.calls.append((path, deepcopy(params)))
        if path in self.errors:
            raise CampusError(self.errors[path])
        if path == s.SCENE:
            return {'data': self.scenes}
        if path == s.LOCATION:
            return {'data': [{'uuid': params['siteType'].lower(), 'siteType': params['siteType'],
                              'siteName': '合成地点'}]}
        if path == s.PAGE:
            rows = [f for f in self.fields
                    if f['sessionVo'][0]['sceneUseType'] == params['sceneUseType']]
            return {'count': len(rows), 'data': rows if params['pageNum'] == 1 else []}
        if path == s.RECORDS:
            if self.records_handler is not None:
                return self.records_handler(params)
            if self.records_error:
                raise CampusError(self.records_error)
            if self.records_pages is not None:
                return self.records_pages[params['pageNum']]
            start = (params['pageNum'] - 1) * params['pageSize']
            return {'count': len(self.rows), 'data': self.rows[start:start + params['pageSize']]}
        if path == s.DETAIL:
            if self.detail_override is not None:
                return self.detail_override
            row = next(r for r in self.rows if r['resvUuid'] == params['resvUuid'])
            return {'data': {'resvUuid': params['resvUuid'],
                             'data': {'moduleData': {}, 'resvExtendStatus': row['resvExtendStatus'],
                                      'resvRecStatus': row['resvStatus']},
                             'orderInfo': {'payableAmount': self.detail_amount,
                                           'payType': self.detail_pay_type}}}
        if path == net.USER:
            return {'data': {'id': 'selfid'}}
        if path == net.CAPTCHA:
            return {'data': {'sysKey': 'RESV_CODE', 'sysValue': self.captcha}}
        if path == net.FORM_BRIEF + 'official-form':
            return {'data': {'deployUuid': 'synthetic-deployment'}}
        raise AssertionError(path)


def make_engine(tmp_path, monkeypatch, fake, operation='create_reservation', clock=time.time):
    monkeypatch.setattr(a, 'Reader', lambda browsers: fake)
    adapter = a.SportsAction(None, net.CREATE if operation == 'create_reservation' else net.CANCEL)

    def consent(record):
        return {'plan_id': record['operation_id'], 'account': record['account'],
                'digest': record['digest'], 'adapter_version': record['adapter_version'],
                'source': 'human', 'reference': 'synthetic-local-human',
                'expires_at': record['expires_at']}

    engine = ActionEngine(store=make_action_store(tmp_path),
                          adapters={('sports', operation): adapter},
                          account_provider=lambda: 'test-account',
                          consent_verifier=consent, clock=clock)
    return engine, adapter


def block_for(fake, checked='2026-11-17T12:00:00+08:00'):
    """Build the availability precheck blocks the way selected_slot does."""
    result = s.availability(fake, {'scene_id': 'scene', 'room_id': 'room',
                                   'mode': 'SPORT_GROUP', 'date': DAY})
    built = precheck.build('sports', 'availability', result, result['items'],
                           {'scene_id': 'scene', 'room_id': 'room', 'mode': 'SPORT_GROUP',
                            'date': DAY}, checked)
    site = result['items'][0]
    slot = site['sessions'][0]
    return built['summary'], site, slot


def record_now(fake, **patch):
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat(), **patch}]


def test_free_plan_dispatches_once_and_verifies_the_remote_record(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    sent = []
    bodies = []

    def send(reader, path, body, gate):
        assert path == net.CREATE
        bodies.append(body)
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)
        record_now(fake)
        raise TimeoutError('lost response')

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    first = engine.execute(plan['plan_id'], AUTH)
    assert first['status'] == 'SUCCEEDED'
    assert first['platform']['reservation_id'] == 'rec'
    assert first['platform']['amounts']['total_verified'] is True
    assert first['platform']['payment_receipt_verified'] is False
    # The lost response still leaves the dispatch fact recorded as SENT.
    assert first['effects'] == [{'step': 'official_request', 'status': 'SENT'}]
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'SUCCEEDED'
    assert sent == [1]
    # The request body is bound to the chosen project, room, site, session and
    # mode; no other project's site can be substituted.
    assert bodies[0]['sceneUuid'] == 'scene' and bodies[0]['siteUuid'] == 'court'
    assert bodies[0]['sceneUseType'] == 'SPORT_GROUP'
    assert bodies[0]['siteSessionReserve'][0]['sessionDetailUuid'] == 'slot'
    assert all(path not in (net.CREATE, net.CANCEL) for path, _ in fake.calls)


def test_definite_not_sent_is_failed_and_never_retried(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    calls = []

    def send(reader, path, body, gate):
        calls.append(1)
        raise a.NotDispatched()

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED' and result['reason'] == 'REQUEST_NOT_SENT'
    assert result['effects'] == [{'step': 'official_request', 'status': 'NOT_SENT'}]
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'FAILED'
    assert calls == [1]


def test_total_change_invalidates_the_old_plan(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    fake.fields[0]['sessionVo'][0]['userFeeDetails']['payableAmount'] = 100
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'


def test_price_change_invalidates_the_old_plan_without_dispatch(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    fake.fields[0]['sessionVo'][0]['userFeeDetails']['chargingUnitPrice'] = 300
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'
    assert engine.result(plan['plan_id'])['status'] == 'FAILED'


def test_session_closure_invalidates_the_old_plan(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    fake.fields[0]['sessionVo'][0]['reserveStatus'] = {'reserveStatus': 'N'}
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED' and result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'


def test_preview_binding_ignores_check_time_but_not_price_or_rules():
    fake = Fake()
    summary, site, slot = block_for(fake, '2026-11-17T12:00:00+08:00')
    first = a.slot_binding(summary, site, slot)
    summary2, site2, slot2 = block_for(fake, '2026-11-17T12:05:41+08:00')
    assert a.slot_binding(summary2, site2, slot2) == first

    fake.fields[0]['sessionVo'][0]['userFeeDetails']['chargingUnitPrice'] = 1
    summary3, site3, slot3 = block_for(fake)
    assert a.slot_binding(summary3, site3, slot3) != first

    fake.fields[0]['sessionVo'][0]['userFeeDetails']['chargingUnitPrice'] = 0
    fake.fields[0]['reserveRule'] = {'cancelBeforeStart': 30}
    summary4, site4, slot4 = block_for(fake)
    assert a.slot_binding(summary4, site4, slot4) != first


def test_same_named_site_from_another_project_is_rejected():
    fake = Fake()
    fake.scenes.append({'uuid': 'scene-b', 'sceneName': '合成项目', 'relatedType': 'DEV'})
    fake.fields[0]['sceneUuid'] = 'scene-b'
    with pytest.raises(CampusError, match='SPORTS_SCENE_MISMATCH'):
        a.creation_preview(fake, ARGS)


def test_mode_without_a_source_contract_is_refused():
    fake = Fake()
    with pytest.raises(CampusError, match='INVALID_ARGUMENTS'):
        a.creation_preview(fake, {**ARGS, 'mode': 'SPORT_UNKNOWN'})
    assert not fake.calls                     # rejected before any source read
    with pytest.raises(CampusError, match='INVALID_ARGUMENTS'):
        a.creation_preview(fake, {**ARGS, 'mode': 'SPORT_PERSON'})
    # The declared project mode is read, but no mode-specific page is requested.
    assert [path for path, _ in fake.calls] == [s.SCENE]


def test_person_mode_hands_off_ticket_quantity_and_never_dispatches(tmp_path, monkeypatch):
    fake = Fake()
    fake.scenes[0]['sceneUseType'] = 'SPORT_PERSON'
    fake.fields[0]['sessionVo'][0]['sceneUseType'] = 'SPORT_PERSON'
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', {**ARGS, 'mode': 'SPORT_PERSON'})
    assert 'CHOOSE_TICKET_AND_QUANTITY' in plan['preview']['handoff']
    assert '1 TICKET' in plan['preview']['fee']
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'AWAITING_OFFICIAL'
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'AWAITING_OFFICIAL'


def test_official_verification_stays_awaiting_official(tmp_path, monkeypatch):
    fake = Fake()
    fake.captcha = '1'
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert 'OFFICIAL_CAPTCHA' in plan['preview']['handoff']
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'AWAITING_OFFICIAL'
    assert result['official_url'].endswith('#/sports')
    assert engine.result(plan['plan_id'])['status'] == 'AWAITING_OFFICIAL'


def test_unknown_total_is_never_priced_from_the_unit_price(tmp_path, monkeypatch):
    fake = Fake()
    fake.fields[0]['sessionVo'][0]['userFeeDetails'] = {'chargingUnitPrice': 300, 'payType': 3}
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'create_reservation', {**ARGS, 'payment_method': 'PAY_ONLINE'})
    assert 'CONFIRM_TOTAL_AND_PAYMENT_METHOD' in plan['preview']['handoff']
    assert '总额须在官方结算页确认' in plan['preview']['fee']
    assert '3.00 元' in plan['preview']['fee']
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'AWAITING_OFFICIAL'


def test_expected_total_mismatch_blocks_prepare():
    fake = Fake()
    fake.fields[0]['sessionVo'][0]['userFeeDetails'] = {'chargingUnitPrice': 300,
                                                        'payableAmount': 300, 'payType': 3}
    with pytest.raises(CampusError, match='SPORTS_FEE_CHANGED'):
        a.creation_preview(fake, {**ARGS, 'payment_method': 'PAY_ONLINE',
                                  'expected_total_fen': 400})


def unresolved_create(engine, monkeypatch):
    """Dispatch one create whose response is lost and whose order is not visible."""
    sent = []

    def send(reader, path, body, gate):
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'UNKNOWN'
    return plan, sent


def test_pending_payment_and_paid_pending_approval_are_separate(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat(),
                  'orderStatus': 1, 'paymentDeadline': DAY + ' 08:30:00'}]
    pending = engine.result(plan['plan_id'])
    assert pending['status'] == 'PENDING_PAYMENT'
    assert pending['platform']['payment_status'] == 'PENDING_PAYMENT'
    assert pending['platform']['payment_deadline'].startswith(DAY + 'T08:30')
    assert pending['platform']['payment_deadline_state'] == 'PLATFORM_DECLARED'
    assert pending['platform']['approval_status'] == 'RESV_APPROVE_PASS'
    assert pending['platform']['payment_receipt_verified'] is False

    fake.rows[0].update(orderStatus=4, resvCheckStatus='RESV_WAIT_APPROVE')
    found = engine.result(plan['plan_id'])
    assert found['status'] == 'PENDING_APPROVAL'
    assert found['platform']['payment_status'] == 'PAID'
    assert found['platform']['approval_status'] == 'RESV_WAIT_APPROVE'


def test_repeated_execute_of_an_unresolved_plan_dispatches_only_once(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    sent = []

    def send(reader, path, body, gate):
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'UNKNOWN'
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'UNKNOWN'
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'
    assert sent == [1]
    record_now(fake)
    assert engine.result(plan['plan_id'])['status'] == 'SUCCEEDED'
    assert sent == [1]


def test_cancel_success_with_unconfirmed_refund_is_not_an_arrival(tmp_path, monkeypatch):
    fake = Fake()
    fake.rows = [deepcopy(ROW)]
    engine, _ = make_engine(tmp_path, monkeypatch, fake, 'cancel_reservation')

    def send(reader, path, body, gate):
        assert path == net.CANCEL
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CANCEL, 'POST', json.dumps(body))
        fake.rows[0].update(resvExtendStatus=2, orderRefundStatus=4)
        raise TimeoutError('lost response')

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'cancel_reservation', {'record_id': 'rec'})
    assert plan['preview']['refund_state'] == 'NONE'
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'SUCCEEDED'
    platform = result['platform']
    assert platform['refund_status'] == 'REFUNDING'
    assert platform['refund_accepted'] is True
    assert platform['refund_settled_by_platform'] is False
    assert platform['refund_arrived_verified'] is False


def test_cancel_preview_is_exact_fen_and_binds_payment_state():
    fake = Fake()
    fake.rows = [{**deepcopy(ROW), 'paidAmount': 12345, 'orderStatus': 4}]
    business, _ = a.cancel_preview(fake, {'record_id': 'rec'})
    assert business['fee'] == '已付 123.45 元；付款状态 PAID'
    assert '12345.00' not in json.dumps(business, ensure_ascii=False)
    assert business['original_status'] == 'SCHEDULED'


def test_cancellation_rule_change_invalidates_the_cancel_plan(tmp_path, monkeypatch):
    fake = Fake()
    fake.rows = [deepcopy(ROW)]
    engine, _ = make_engine(tmp_path, monkeypatch, fake, 'cancel_reservation')
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'cancel_reservation', {'record_id': 'rec'})
    fake.rows[0]['cancelBeforeStartTime'] = 30
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED' and result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'


def test_cancel_requires_a_complete_own_record_search():
    fake = Fake()
    fake.rows = [deepcopy(ROW)]
    fake.records_pages = {1: {'count': 2, 'data': fake.rows}}
    with pytest.raises(CampusError, match='SPORTS_ORDER_SEARCH_INCOMPLETE'):
        a.cancel_preview(fake, {'record_id': 'rec'})
    with pytest.raises(CampusError, match='SPORTS_ORDER_SEARCH_INCOMPLETE'):
        a.creation_preview(fake, ARGS)


def test_short_or_duplicate_order_page_never_decides_success(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat()}]
    # A short page claims two records but returns one: the list is partial.
    fake.records_pages = {1: {'count': 2, 'data': fake.rows}}
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'
    # Two similar records make the match ambiguous even on a complete page.
    fake.records_pages = None
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat()},
                 {**deepcopy(ROW), 'resvUuid': 'rec2',
                  'gmtCreate': datetime.now(s.TZ).isoformat()}]
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'


def test_detail_disagreement_keeps_the_result_unknown(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    row = {**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat()}
    fake.rows = [row]
    fake.detail_override = {'data': {'resvUuid': 'rec',
                                     'data': {'moduleData': {}, 'resvExtendStatus': 1,
                                              'resvRecStatus': 'RESV_EFFECT'}}}
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'
    fake.detail_override = {'data': {'resvUuid': 'rec', 'data': {}}}
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'


def test_delayed_order_after_plan_expiry_is_still_matched(tmp_path, monkeypatch):
    fake = Fake()
    base = time.time()
    engine, _ = make_engine(tmp_path, monkeypatch, fake, clock=lambda: base)
    sent = []

    def send(reader, path, body, gate):
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'UNKNOWN'
    engine.clock = lambda: base + 700          # the plan itself has expired
    record_now(fake)
    assert engine.result(plan['plan_id'])['status'] == 'SUCCEEDED'
    assert sent == [1]


def test_order_created_before_dispatch_is_not_attributed_to_the_plan(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    sent = []

    def send(reader, path, body, gate):
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'UNKNOWN'
    fake.rows = [{**deepcopy(ROW),
                  'gmtCreate': (datetime.now(s.TZ) - timedelta(hours=1)).isoformat()}]
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'
    assert sent == [1]


def test_disabling_new_transactions_keeps_reads_and_reconciliation(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    sent = []

    def send(reader, path, body, gate):
        assert gate.consume(net.ORIGIN + net.PREFIX + net.CREATE, 'POST', json.dumps(body))
        sent.append(1)
        record_now(fake)
        raise TimeoutError('lost response')

    monkeypatch.setattr(a, 'dispatch_exact', send)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'SUCCEEDED'
    monkeypatch.setenv('THU_SPORTS_WRITE', '0')
    assert engine.result(plan['plan_id'])['status'] == 'SUCCEEDED'
    with pytest.raises(CampusError, match='SPORTS_WRITES_DISABLED'):
        engine.prepare('sports', 'create_reservation', ARGS)
    described = describe('sports')
    assert 'orders' in [item['operation'] for item in described['operations']]
    assert not any(item['enabled'] for item in described['actions'])
    assert [path for path, _ in fake.calls].count(net.RECORDS) >= 1
    assert sent == [1]


def test_rate_limited_reconciliation_stays_unknown_without_replay(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, sent = unresolved_create(engine, monkeypatch)
    fake.records_error = 'RATE_LIMITED'
    shown = engine.result(plan['plan_id'])
    assert shown['status'] == 'UNKNOWN'
    assert shown['next_actions'] == ['CHECK_RESULT_ONLY_DO_NOT_RESEND']
    assert sent == [1]
    # The limiter only changes when the next readback may run.
    fake.records_error = None
    record_now(fake)
    assert engine.result(plan['plan_id'])['status'] == 'SUCCEEDED'
    assert sent == [1]


def test_official_url_never_carries_signing_parameters():
    assert net.redact_official_url(net.HOME + '?appId=1&nonce=n&sign=secret&timeStamp=1#/sports') \
        == net.HOME + '#/sports'
    assert net.redact_official_url('https://example.invalid/venue/index.html#/sports') == net.PERSONAL
    assert net.redact_official_url('https://user:pw@www.sports.tsinghua.edu.cn/venue/index.html#/sports') \
        == net.PERSONAL
    fake = Fake()
    business, _ = a.creation_preview(fake, ARGS)
    assert business['official_url'] == net.HOME + '#/sports'
    assert 'sign=' not in business['official_url']


def test_capability_table_stays_within_the_existing_two_operations():
    table = a.capabilities()
    assert table['operations'] == ['create_reservation', 'cancel_reservation']
    assert set(table['modes']) == {'SPORT_GROUP', 'SPORT_PERSON'}
    assert all(mode['ticket_quantity'] == 1 for mode in table['modes'].values())
    assert table['amount']['unit'] == 'FEN'
    assert table['amount']['multiplies_unit_price_never'] is True
    assert 'MULTI_TICKET_PURCHASE' in table['not_supported']
    # No new sports-specific tool/operation name is introduced.
    assert not any(name in json.dumps(table) for name in ('sports_pay', 'sports_refund', 'sports_ticket'))


# --- P1-1: the canonical effective order must reach the cancel loop ---------

def test_effective_order_gets_an_official_cancel_plan(tmp_path, monkeypatch):
    fake = Fake()
    fake.rows = [deepcopy(SCHEDULED_ROW)]
    engine, _ = make_engine(tmp_path, monkeypatch, fake, 'cancel_reservation')
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'cancel_reservation', {'record_id': 'rec'})
    assert 'OFFICIAL_CANCEL_CONTROL' in plan['preview']['handoff']
    assert plan['preview']['original_status'] == 'SCHEDULED'
    assert 'site_ids' not in plan['preview']
    assert plan['preview']['target'].startswith('合成项目')
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'AWAITING_OFFICIAL'


def test_cancel_plan_is_bound_to_the_site_list_without_publishing_it(tmp_path, monkeypatch):
    fake = Fake()
    fake.rows = [deepcopy(ROW)]
    engine, _ = make_engine(tmp_path, monkeypatch, fake, 'cancel_reservation')
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'cancel_reservation', {'record_id': 'rec'})
    fake.rows[0]['siteVoList'][0]['uuid'] = 'other-court'
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'FAILED'
    assert result['reason'] == 'PRECONDITIONS_CHANGED_PREPARE_AGAIN'


def test_effective_paid_order_adds_refund_confirmation_and_reads_back(tmp_path, monkeypatch):
    fake = Fake()
    fake.rows = [deepcopy(SCHEDULED_ROW)]
    fake.rows[0]['paidAmount'] = 200
    engine, _ = make_engine(tmp_path, monkeypatch, fake, 'cancel_reservation')
    monkeypatch.setattr(a, 'dispatch_exact', lambda *args: pytest.fail('must not dispatch'))
    plan = engine.prepare('sports', 'cancel_reservation', {'record_id': 'rec'})
    flags = set(plan['preview']['handoff'].split(','))
    assert {'OFFICIAL_CANCEL_CONTROL', 'CONFIRM_REFUND_CONSEQUENCES'} <= flags
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'AWAITING_OFFICIAL'
    # The user cancels on the official page; the readback proves the record.
    fake.rows[0].update(resvExtendStatus=2, orderRefundStatus=4)
    result = engine.result(plan['plan_id'])
    assert result['status'] == 'SUCCEEDED'
    assert result['platform']['refund_status'] == 'REFUNDING'
    assert result['platform']['refund_arrived_verified'] is False


def test_ended_or_cancelled_order_is_refused():
    ended = Fake()
    ended.rows = [{**SCHEDULED_ROW, 'resvStatus': 'RESV_END'}]
    with pytest.raises(CampusError, match='SPORTS_CANCELLATION_NOT_AVAILABLE'):
        a.cancel_preview(ended, {'record_id': 'rec'})
    cancelled = Fake()
    cancelled.rows = [{**ROW, 'resvExtendStatus': 2}]
    with pytest.raises(CampusError, match='SPORTS_CANCELLATION_NOT_AVAILABLE'):
        a.cancel_preview(cancelled, {'record_id': 'rec'})


def test_extended_bit_256_hands_off_instead_of_refusing():
    fake = Fake()
    fake.rows = [{**SCHEDULED_ROW, 'resvExtendStatus': 257}]
    business, _ = a.cancel_preview(fake, {'record_id': 'rec'})
    assert 'OFFICIAL_CANCEL_CONTROL' in business['handoff']
    assert 'bit 256' in a.capabilities()['assumptions'][0]


# --- P1-2 / P2-5 / P2-6: dispatch observation and the dispatch fact ---------

class WirePage:
    """Synthetic reservation page for dispatch_exact."""

    def __init__(self, outcome='FETCHED', observe=None, prepare_error=False):
        self.main_frame = object()
        self.outcome = outcome
        self.observe = observe            # None, 'match' or 'wrong'
        self.prepare_error = prepare_error
        self.signed = None
        self.body = None
        self.removed = False

    def route(self, pattern, guard):
        self.pattern = pattern
        self.guard = guard

    def unroute(self, *args):
        self.removed = True

    def evaluate(self, script, args=None):
        if args is not None:
            if self.prepare_error:
                raise RuntimeError('prepare failed')
            self.body = json.dumps(args['params'])
            self.signed = (net.ORIGIN + net.PREFIX + args['path']
                           + '?appId=1&nonce=n&sign=s&timeStamp=1')
            return {'url': self.signed, 'body': self.body}
        if self.observe == 'match':
            self.guard(SimpleNamespace(
                request=SimpleNamespace(url=self.signed, frame=self.main_frame,
                                        method='POST', post_data=self.body),
                continue_=lambda: None, abort=lambda: None))
        elif self.observe == 'wrong':
            self.guard(SimpleNamespace(
                request=SimpleNamespace(url=self.signed + 'x', frame=self.main_frame,
                                        method='POST', post_data=self.body),
                continue_=lambda: None, abort=lambda: None))
        return self.outcome


def create_gate(body):
    return RequestGate([RequestSpec(net.ORIGIN + net.PREFIX + net.CREATE, 'POST',
                                    json.dumps(body))])


def test_dispatch_exact_unobserved_fetch_is_never_not_sent():
    page = WirePage(outcome='FETCHED', observe=None)
    reader = SimpleNamespace(page=page)
    body = {'sceneUuid': 'scene'}
    gate = create_gate(body)
    with pytest.raises(CampusError, match='SPORTS_DISPATCH_UNOBSERVED'):
        a.dispatch_exact(reader, net.CREATE, body, gate)
    assert gate.sent == 0 and page.removed


def test_dispatch_exact_no_pending_script_is_not_sent():
    page = WirePage(outcome='NO_PENDING', observe=None)
    reader = SimpleNamespace(page=page)
    body = {'sceneUuid': 'scene'}
    with pytest.raises(a.NotDispatched):
        a.dispatch_exact(reader, net.CREATE, body, create_gate(body))
    assert page.removed


def test_dispatch_exact_observed_abort_is_not_sent():
    page = WirePage(outcome='FETCHED', observe='wrong')
    reader = SimpleNamespace(page=page)
    body = {'sceneUuid': 'scene'}
    with pytest.raises(a.NotDispatched):
        a.dispatch_exact(reader, net.CREATE, body, create_gate(body))


def test_dispatch_exact_marks_the_gate_when_it_is_consumed():
    page = WirePage(outcome='FETCHED', observe='match')
    reader = SimpleNamespace(page=page)
    body = {'sceneUuid': 'scene'}
    gate = create_gate(body)
    marks = []
    a.dispatch_exact(reader, net.CREATE, body, gate, on_sent=lambda: marks.append('sent'))
    assert gate.sent == 1 and marks == ['sent']


def test_dispatch_exact_prepare_failure_is_not_sent():
    page = WirePage(prepare_error=True)
    reader = SimpleNamespace(page=page)
    body = {'sceneUuid': 'scene'}
    with pytest.raises(a.NotDispatched):
        a.dispatch_exact(reader, net.CREATE, body, create_gate(body))


def test_dispatch_pre_read_failure_is_not_sent(monkeypatch):
    fake = Fake()
    fake.errors[s.SCENE] = 'RATE_LIMITED'
    monkeypatch.setattr(a, 'Reader', lambda browsers: fake)
    adapter = a.SportsAction(None, net.CREATE)
    record = {'account': 'test-account', 'business': dict(ARGS),
              'expires_at': time.time() + 600,
              'authorization': {'evidence': {'expires_at': time.time() + 600}}}
    gate = create_gate({'sceneUuid': 'scene'})
    with pytest.raises(a.NotDispatched):
        adapter.dispatch(record, gate)


def test_unobserved_request_becomes_unknown_and_the_ledger_says_so(tmp_path, monkeypatch):
    fake = Fake()
    fake.page = WirePage(outcome='FETCHED', observe=None)
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    result = engine.execute(plan['plan_id'], AUTH)
    assert result['status'] == 'UNKNOWN'
    assert result['reason'] == 'REMOTE_RESULT_NOT_PROVEN'
    assert result['effects'] == [{'step': 'official_request', 'status': 'UNKNOWN'}]
    assert result['next_actions'] == ['CHECK_RESULT_ONLY_DO_NOT_RESEND']


def test_lost_response_after_consume_keeps_sent(tmp_path, monkeypatch):
    fake = Fake()
    page = WirePage(outcome='FETCHED', observe=None)

    def evaluate(script, args=None):
        value = WirePage.evaluate(page, script, args)
        if args is None:
            page.guard(SimpleNamespace(
                request=SimpleNamespace(url=page.signed, frame=page.main_frame,
                                        method='POST', post_data=page.body),
                continue_=lambda: None, abort=lambda: None))
            raise TimeoutError('lost response')
        return value

    page.evaluate = evaluate
    fake.page = page
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    first = engine.execute(plan['plan_id'], AUTH)
    # The gate was consumed once; the timeout leaves UNKNOWN with SENT recorded.
    assert first['status'] == 'UNKNOWN'
    assert first['effects'] == [{'step': 'official_request', 'status': 'SENT'}]


# --- P2-1 / P2-2 / P2-4 / P2-13: evidence semantics -------------------------

def test_eligibility_is_not_claimed_as_verified(tmp_path, monkeypatch):
    table = a.capabilities()
    assert 'UNVERIFIED' in table['prechecks']['eligibility']
    assert a.eligibility_check({'eligibility': {'state': 'UNVERIFIED'}}) is True
    assert a.eligibility_check({'eligibility': {'state': 'INELIGIBLE'}}) is False
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    record_now(fake)
    platform = engine.result(plan['plan_id'])['platform']
    assert platform['eligibility'] == {'state': 'UNVERIFIED', 'verified': False,
                                       'authority': 'PLATFORM_RESPONSE'}


def paid_fake():
    fake = Fake()
    fake.fields[0]['sessionVo'][0]['userFeeDetails'] = {
        'chargingUnitPrice': 300, 'payableAmount': 300, 'payType': 3}
    fake.detail_amount = 300
    return fake


def test_free_plan_reports_its_implicit_zero_total(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    assert '免费计划按 0 元核对' in plan['preview']['fee']
    record_now(fake)
    amounts = engine.result(plan['plan_id'])['platform']['amounts']
    assert amounts['expected_total_fen'] == 0
    assert amounts['total_verified'] is True
    assert amounts['total_source_parsed'] is True


def test_paid_plan_without_a_plan_amount_says_it_was_not_compared(tmp_path, monkeypatch):
    fake = paid_fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation', {**ARGS, 'payment_method': 'PAY_ONLINE'})
    assert '未提供计划金额，未做比对' in plan['preview']['fee']
    assert engine.execute(plan['plan_id'], AUTH)['status'] == 'AWAITING_OFFICIAL'
    record_now(fake, resvStatus='RESV_EFFECT')
    result = engine.result(plan['plan_id'])
    assert result['status'] == 'SUCCEEDED'
    assert result['platform']['amounts']['expected_total_fen'] is None
    assert result['platform']['amounts']['total_verified'] is False


def test_explicit_plan_amount_is_compared_and_stated(tmp_path, monkeypatch):
    fake = paid_fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation',
                          {**ARGS, 'payment_method': 'PAY_ONLINE', 'expected_total_fen': 300})
    assert '已按计划金额核对' in plan['preview']['fee']
    engine.execute(plan['plan_id'], AUTH)
    record_now(fake, resvStatus='RESV_EFFECT')
    amounts = engine.result(plan['plan_id'])['platform']['amounts']
    assert amounts['expected_total_fen'] == 300 and amounts['total_verified'] is True


def test_numeric_pay_type_is_recorded_not_fatal(tmp_path, monkeypatch):
    fake = paid_fake()
    fake.detail_pay_type = 3
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation',
                          {**ARGS, 'payment_method': 'PAY_ONLINE', 'expected_total_fen': 300})
    engine.execute(plan['plan_id'], AUTH)
    record_now(fake, resvStatus='RESV_EFFECT')
    result = engine.result(plan['plan_id'])
    assert result['status'] == 'SUCCEEDED'
    payment = result['platform']['payment_method']
    assert payment['source_pay_type'] == 3
    assert payment['matches_plan'] is False
    assert payment['verified'] is False


def test_refresh_and_checked_at_are_consumer_compatible(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    record_now(fake)
    platform = engine.result(plan['plan_id'])['platform']
    assert platform['refresh']['service_id'] == 'sports'
    assert platform['refresh']['operation'] == 'order_detail'
    assert platform['checked_at'].endswith('+00:00')
    assert platform['checked_at_zone'] == 'UTC'


# --- P2-3 / P2-8 / P2-17 / P2-18: failure states and reads ------------------

@pytest.mark.parametrize('order_status', [8, 16])
def test_expired_or_cancelled_payment_fails_the_plan(tmp_path, monkeypatch, order_status):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    record_now(fake, orderStatus=order_status)
    assert engine.result(plan['plan_id'])['status'] == 'FAILED'


def test_unrelated_malformed_order_does_not_block_reconciliation(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat()},
                 {'resvUuid': 'old', 'sceneUuid': 'other', 'resvTime': None,
                  'siteVoList': []}]
    assert engine.result(plan['plan_id'])['status'] == 'SUCCEEDED'


def test_malformed_candidate_row_keeps_the_result_unknown(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': datetime.now(s.TZ).isoformat()},
                 {'resvUuid': 'bad', 'sceneUuid': 'scene', 'resvTime': None,
                  'siteVoList': [{'uuid': 'court'}]}]
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'


def test_malformed_gmtcreate_is_not_a_crash(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, _ = unresolved_create(engine, monkeypatch)
    fake.rows = [{**deepcopy(ROW), 'gmtCreate': 'not-a-timestamp'}]
    assert engine.result(plan['plan_id'])['status'] == 'UNKNOWN'


def test_one_transient_short_page_is_retried_before_prepare_fails(tmp_path, monkeypatch):
    fake = Fake()
    reads = {'page1': 0}

    def handler(params):
        if params['pageNum'] == 1:
            reads['page1'] += 1
            if reads['page1'] == 1:
                return {'count': 2, 'data': []}
        return {'count': 0, 'data': []}

    fake.records_handler = handler
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan = engine.prepare('sports', 'create_reservation', ARGS)
    assert plan['status'] == 'PREPARED'
    assert reads['page1'] == 2


def test_persistent_short_page_still_blocks_prepare():
    fake = Fake()
    fake.records_handler = lambda params: {'count': 2, 'data': []}
    with pytest.raises(CampusError, match='SPORTS_ORDER_SEARCH_INCOMPLETE'):
        a.creation_preview(fake, ARGS)


def test_read_switch_off_suspends_pending_reconciliation(tmp_path, monkeypatch):
    fake = Fake()
    engine, _ = make_engine(tmp_path, monkeypatch, fake)
    plan, sent = unresolved_create(engine, monkeypatch)
    record_now(fake)
    monkeypatch.setenv('THU_SPORTS_READ', '0')
    before = [path for path, _ in fake.calls].count(s.RECORDS)
    shown = engine.result(plan['plan_id'])
    assert shown['status'] == 'UNKNOWN'
    assert [path for path, _ in fake.calls].count(s.RECORDS) == before
    assert sent == [1]


# --- P2-10 / P2-11: error registry and capability isolation -----------------

def test_every_sports_error_code_used_by_the_adapter_is_registered():
    import pathlib
    import re
    from thu_assistant import source_state
    codes = set()
    for name in ('sports_actions.py', 'sports_transport.py'):
        text = (pathlib.Path(a.__file__).parent / name).read_text(encoding='utf-8')
        codes |= set(re.findall(r"'(SPORTS_[A-Z0-9_]+)'", text))
    assert codes
    assert codes <= set(source_state.ERROR_CODES), sorted(codes - set(source_state.ERROR_CODES))


def test_capabilities_return_is_isolated_from_the_module_contract():
    first = a.capabilities()
    first['modes']['SPORT_PERSON']['official_handoff'].append('INJECTED')
    first['operations'].append('injected')
    second = a.capabilities()
    assert 'INJECTED' not in second['modes']['SPORT_PERSON']['official_handoff']
    assert second['operations'] == ['create_reservation', 'cancel_reservation']
    assert 'INJECTED' not in a.MODE_CONTRACT['SPORT_PERSON']['official_handoff']
