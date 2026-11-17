"""5.1 renewal: eligibility read, one bound request and new-due readback.

Every test uses a synthetic Primo page and synthetic transport.  No renewal is
sent to the real site; the production contract registry stays empty and a
SYNTHETIC contract is executable only with the explicit test opt-in.
"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from thu_assistant import library as lib
from thu_assistant import library_contracts as c
from thu_assistant import renewal as renewal_pkg
from thu_assistant import source_state
from thu_assistant.renewal import adapter as ra_adapter
from thu_assistant.renewal import contract as ra_contract
from thu_assistant.renewal import transport as ra_transport
from thu_assistant.actions import ActionEngine, RequestGate, NotDispatched
from thu_assistant.bridge import CampusError
from synthetic_store import make_action_store

DAY='2099-12-31'
DUE=DAY+'T15:30+08:00'
AUTH='对本次合成续借的精确业务授权'
CONTRACT=ra_contract.SourceContract(contract_id='synthetic-renewal-v1',
    origin='https://discover.lib.tsinghua.edu.cn',path='/__synthetic__/renew',
    encoding='json',success_field='code',success_value=0,evidence_state='SYNTHETIC')
OTHER=ra_contract.SourceContract(contract_id='synthetic-renewal-v2',
    origin='https://discover.lib.tsinghua.edu.cn',path='/__synthetic__/different',
    encoding='json',success_field='code',success_value=0,evidence_state='SYNTHETIC')


def loan(due=DUE,renew='Y',hints=None):
    due_date,hour=(due[:10].replace('-',''),due[11:13]+due[14:16]) if 'T' in due else (due.replace('-',''),None)
    row={'loanid':'9001','title':'合成图书','duedate':due_date,'renew':renew,
         'renewstatuses':{'renewstatus':hints if hints is not None else ['可续借']}}
    if hour:
        row['duehour']=hour
    return row


class LoansPage:
    main_frame=object()
    def __init__(self,world):
        self.world=world
    def evaluate(self,script,arg):
        assert script is lib.LOAN_FETCH and isinstance(arg,int)
        rows=[deepcopy(x) for x in self.world.loans]
        return {'code':200,'data':{'data':{'loans':{'loan':rows,
                'showmore':['Y' if self.world.more else 'N']}}}}
    def locator(self,selector):
        if selector=='.el-table__body-wrapper':
            text='' if self.world.disagree else ' '.join(str(x.get('title')) for x in self.world.loans)
            return SimpleNamespace(count=lambda:1,inner_text=lambda:text)
        assert selector=='.el-table__empty-text'
        return SimpleNamespace(all_text_contents=lambda:[] if self.world.loans else ['暂无数据'])


@pytest.fixture
def world(monkeypatch,tmp_path):
    obj=SimpleNamespace(loans=[loan()],more=False,disagree=False,sent=0,sent_body=None,
                        lost=False,accepted=True,after=None,account='primo-account',
                        store=make_action_store(tmp_path),real_dispatch=ra_transport.dispatch_exact)
    obj.page=LoansPage(obj)
    obj.browsers=SimpleNamespace(sessions={'session':{'account_id':'account'}})
    class Reader:
        sid='session'
        def __init__(self,browsers): self.page=obj.page; self.account=obj.account
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def close(self): pass
    monkeypatch.setattr(ra_adapter,'Reader',Reader)
    def send(reader,contract,loan_id,due_at,gate,allow_synthetic=False):
        spec=ra_transport.specification(contract,loan_id,due_at)
        assert gate.consume(spec.url,'POST',spec.body)
        assert not gate.consume(spec.url,'POST',spec.body)
        obj.sent+=1;obj.sent_body=spec.body
        if obj.after:obj.after()
        if obj.lost:raise TimeoutError()
        return obj.accepted
    monkeypatch.setattr(ra_transport,'dispatch_exact',send)
    def grant(record):
        return {'account':record['account'],'digest':record['digest'],'plan_id':record['operation_id'],
                'adapter_version':c.VERSION,'reference':'synthetic-grant','source':'human',
                'expires_at':record['expires_at']}
    def make(contract=CONTRACT,allow_synthetic=True):
        return ActionEngine(store=obj.store,account_provider=lambda:'account',consent_verifier=grant,
            adapters={('library',c.RENEW_LOAN):ra_adapter.RenewalAction(obj.browsers,contract,
                                                                        allow_synthetic=allow_synthetic)})
    obj.make=make
    obj.engine=make()
    return obj


def prepare(world,record_id='9001',engine=None):
    return (engine or world.engine).prepare('library',c.RENEW_LOAN,{'record_id':record_id})


def test_production_renewal_registry_is_empty_and_unverified():
    assert renewal_pkg.registered()==()
    assert renewal_pkg.state()=='UNVERIFIED'
    assert renewal_pkg.resolve() is None


def test_renewal_contract_requires_tsinghua_https_and_a_known_evidence_state():
    with pytest.raises(CampusError,match='RENEWAL_CONTRACT_INVALID'):
        ra_contract.SourceContract(contract_id='synthetic',origin='https://example.com',path='/x',
                                   evidence_state='GUESSED')
    with pytest.raises(CampusError,match='RENEWAL_CONTRACT_INVALID'):
        ra_contract.SourceContract(contract_id='synthetic',origin='https://discover.lib.tsinghua.edu.cn',
                                   path='renew',evidence_state='SYNTHETIC')
    with pytest.raises(CampusError,match='NON_TSINGHUA_HTTPS_URL'):
        ra_contract.SourceContract(contract_id='synthetic',origin='https://example.com',path='/renew',
                                   evidence_state='SYNTHETIC')


def test_synthetic_evidence_is_refused_without_the_explicit_opt_in(world):
    engine=world.make(allow_synthetic=False)
    plan=prepare(world,engine=engine)
    assert 'RENEWAL_CONTRACT_EVIDENCE_UNVERIFIED' in plan['preview']['handoff']
    assert plan['preview']['contract_evidence']=='SYNTHETIC'
    assert engine.execute(plan['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL' and world.sent==0
    # The transport refuses the same contract even if the adapter gate is bypassed.
    gate=RequestGate([ra_transport.specification(CONTRACT,'9001',DUE)])
    with pytest.raises(NotDispatched):
        world.real_dispatch(SimpleNamespace(page=world.page),CONTRACT,'9001',DUE,gate,True)


def test_unverified_source_produces_an_official_handoff_and_never_posts(world):
    plan=world.make(contract=None).prepare('library',c.RENEW_LOAN,{'record_id':'9001'})
    preview=plan['preview']
    assert plan['status']=='PREPARED' and 'RENEWAL_SOURCE_CONTRACT_UNVERIFIED' in preview['handoff']
    assert preview['due_at']==DUE and preview['renewal_state']=='ELIGIBLE'
    assert preview['precheck_summary']['preparation_state']=='NEEDS_OFFICIAL_PAGE'
    assert preview['official_url']==ra_adapter.OFFICIAL
    assert preview['record_id']=='9001' and 'subject' not in preview
    assert preview['contract_evidence']=='NONE'
    assert '合成图书' not in json.dumps(plan,ensure_ascii=False)
    result=world.make(contract=None).execute(plan['plan_id'],AUTH)
    assert result['status']=='AWAITING_OFFICIAL' and world.sent==0
    assert world.make(contract=None).execute(plan['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    assert world.sent==0


def test_plan_binds_the_request_contract_and_a_swap_is_rejected(world):
    plan=prepare(world)
    stored=world.store.read(plan['plan_id'])
    assert stored['business']['contract_id']==CONTRACT.contract_id
    assert stored['business']['contract_digest']==CONTRACT.digest
    assert stored['business']['contract_evidence']=='SYNTHETIC'
    other=world.make(contract=OTHER)
    result=other.execute(plan['plan_id'],AUTH)
    assert result['status']=='FAILED' and result['reason']=='PRECONDITIONS_CHANGED_PREPARE_AGAIN'
    assert world.sent==0
    assert other.execute(plan['plan_id'],AUTH)['status']=='FAILED' and world.sent==0


def test_not_eligible_overdue_or_unknown_loans_are_rejected_before_dispatch(world):
    world.loans=[loan(renew='N')]
    with pytest.raises(CampusError,match='RENEWAL_NOT_ELIGIBLE'):prepare(world)
    world.loans=[loan(due='2020-01-01')]
    with pytest.raises(CampusError,match='RENEWAL_OVERDUE'):prepare(world)
    world.loans=[loan()]
    with pytest.raises(CampusError,match='RENEWAL_RECORD_NOT_FOUND'):prepare(world,'7777')
    world.loans=[loan(),{**loan(),'loanid':'9002'}]
    assert prepare(world)['status']=='PREPARED'
    assert world.sent==0


def test_unknown_renewal_eligibility_needs_the_official_page(world):
    world.loans=[loan(renew=None)]
    plan=prepare(world)
    assert 'RENEWAL_ELIGIBILITY_UNVERIFIED' in plan['preview']['handoff']
    assert plan['preview']['precheck_summary']['eligibility_state']=='UNKNOWN'
    assert world.engine.execute(plan['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL' and world.sent==0


@pytest.mark.parametrize('case,code',[('repeated','RENEWAL_LOAN_PAGE_REPEATED'),
                                      ('short','RENEWAL_LOAN_PAGINATION_UNVERIFIED'),
                                      ('disagree','RENEWAL_PAGE_DISAGREES_WITH_API')])
def test_incomplete_or_disagreeing_loan_pages_never_prepare(world,case,code):
    if case=='repeated':
        world.loans=[loan(),loan()]
    elif case=='short':
        world.more=True;world.loans=[loan(),{**loan(),'loanid':'9002'}]
    else:
        world.disagree=True
    with pytest.raises(CampusError,match=code):prepare(world)
    assert world.sent==0


def test_renewal_success_needs_the_same_record_and_a_later_due_time(world):
    plan=prepare(world)
    stored=world.store.read(plan['plan_id'])
    assert stored['business']['due_at']==DUE
    world.after=lambda:world.loans.__setitem__(0,loan(due='2100-01-31T15:30+08:00'))
    result=world.engine.execute(plan['plan_id'],AUTH)
    assert result['status']=='SUCCEEDED' and world.sent==1
    assert json.loads(world.sent_body)=={'due_at':DUE,'loan_id':'9001'}
    # The bound business field is never rewritten; the new due time is platform evidence.
    assert world.store.read(plan['plan_id'])['business']['due_at']==DUE
    assert result['preview']['due_at']==DUE
    assert result['platform']['previous_due_at']==DUE
    assert result['platform']['new_due_at']=='2100-01-31T15:30+08:00'
    assert result['platform']['completed_by']=='ADAPTER_DISPATCH_OR_OFFICIAL_PAGE'
    # The todo window spans the old and new due dates so the renewed loan is found.
    assert result['todo_query']=={'services':['library.loans'],'start_date':DAY,
                                  'end_date':'2100-01-31','include_completed':True}
    assert '合成图书' not in json.dumps(result,ensure_ascii=False)


def test_official_handoff_reconciliation_is_attributed_to_the_official_page(world):
    engine=world.make(contract=None)
    plan=engine.prepare('library',c.RENEW_LOAN,{'record_id':'9001'})
    assert engine.execute(plan['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    world.loans[0]=loan(due='2100-01-31T15:30+08:00')
    result=engine.result(plan['plan_id'])
    assert result['status']=='SUCCEEDED'
    assert result['platform']['completed_by']=='OFFICIAL_HANDOFF'
    assert result['preview']['due_at']==DUE


def test_lost_renewal_response_only_reconciles_and_never_resends(world):
    plan=prepare(world)
    world.lost=True
    first=world.engine.execute(plan['plan_id'],AUTH)
    assert first['status']=='UNKNOWN' and world.sent==1 and first['automatic_resend'] is False
    assert world.store.read(plan['plan_id'])['business']['due_at']==DUE
    # The same due time is not proof of renewal.
    assert world.engine.execute(plan['plan_id'],AUTH)['status']=='UNKNOWN' and world.sent==1
    world.loans[0]=loan(due='2100-01-31T15:30+08:00')
    final=world.engine.result(plan['plan_id'])
    assert final['status']=='SUCCEEDED' and world.sent==1
    assert world.store.read(plan['plan_id'])['business']['due_at']==DUE
    assert final['platform']['new_due_at']=='2100-01-31T15:30+08:00'


def test_failed_renewal_response_keeps_the_original_due_unknown(world):
    plan=prepare(world)
    world.accepted=False
    result=world.engine.execute(plan['plan_id'],AUTH)
    assert result['status']=='UNKNOWN' and world.sent==1
    assert result['preview']['due_at']==DUE and result.get('platform') is None
    assert world.store.read(plan['plan_id'])['business']['due_at']==DUE


def test_same_day_precision_change_or_equal_instant_is_not_a_renewal(world):
    world.loans=[loan(due='2099-12-31')]
    plan=prepare(world)
    assert world.engine.execute(plan['plan_id'],AUTH)['status']=='UNKNOWN'
    world.loans[0]=loan(due=DUE)                     # date-only -> same day + time
    assert world.engine.result(plan['plan_id'])['status']=='UNKNOWN'
    world.loans=[loan(due=DUE)]
    other=prepare(world)
    world.after=lambda:world.loans.__setitem__(0,loan(due='2099-12-31T07:30Z'))  # same instant
    assert world.engine.execute(other['plan_id'],AUTH)['status']=='UNKNOWN'


def test_due_comparison_normalizes_offsets_and_ignores_precision_only_changes():
    assert ra_adapter.due_later('2099-12-31T16:00Z','2099-12-31T15:30+08:00')
    assert not ra_adapter.due_later('2099-12-31T07:30Z','2099-12-31T15:30+08:00')
    assert not ra_adapter.due_later('2099-12-31T15:30+08:00','2099-12-31')
    assert ra_adapter.due_later('2100-01-01','2099-12-31')
    assert not ra_adapter.due_later('2099-12-31T23:00+08:00','2099-12-31')
    assert ra_adapter.due_later('2099-12-31T23:00+08:00','2099-12-31T22:59+08:00')


def test_primo_account_change_invalidates_the_plan(world):
    plan=prepare(world)
    world.account='other-primo-account'
    with pytest.raises(CampusError,match='RENEWAL_ACCOUNT_CHANGED_PREPARE_AGAIN'):
        world.engine.execute(plan['plan_id'],AUTH)
    assert world.sent==0


def test_disabled_renewal_switch_blocks_new_plans_but_keeps_readback(world,monkeypatch):
    plan=prepare(world)
    world.lost=True
    assert world.engine.execute(plan['plan_id'],AUTH)['status']=='UNKNOWN'
    monkeypatch.setenv('THU_LIBRARY_RENEW','0')
    with pytest.raises(CampusError,match='LIBRARY_RENEW_DISABLED'):prepare(world)
    world.loans[0]=loan(due='2100-01-31T15:30+08:00')
    # Reads and reconciliation stay available while new dispatch is closed.
    assert world.make().result(plan['plan_id'])['status']=='SUCCEEDED' and world.sent==1
    monkeypatch.delenv('THU_LIBRARY_RENEW')
    monkeypatch.setenv('THU_LIBRARY_WRITE','0')
    with pytest.raises(CampusError,match='LIBRARY_WRITES_DISABLED'):prepare(world)


def test_preview_wraps_an_unexpected_page_error(world,monkeypatch):
    def boom(*args,**kwargs):
        raise RuntimeError('page')
    monkeypatch.setattr(ra_adapter,'loan_proposal',boom)
    with pytest.raises(CampusError,match='RENEWAL_PREVIEW_FAILED'):prepare(world)


def test_every_raised_renewal_code_is_registered():
    from pathlib import Path
    import re
    root=Path(__file__).resolve().parents[1]
    frozen=json.loads((root/'contracts/v0.2/error-classification.json').read_text('utf-8'))['codes']
    text='\n'.join((root/name).read_text('utf-8') for name in
                   ('src/thu_assistant/renewal/adapter.py','src/thu_assistant/renewal/transport.py',
                    'src/thu_assistant/renewal/contract.py','src/thu_assistant/library_contracts.py'))
    codes=set()
    for pattern in (r"CampusError\(\s*'([A-Z][A-Z0-9_]+)'", r"require\([^,\n]+,\s*'([A-Z][A-Z0-9_]+)'"):
        codes.update(re.findall(pattern,text))
    codes={code for code in codes if code.startswith(('RENEWAL_','LIBRARY_RENEW'))}
    assert len(codes)>=12, codes
    assert codes <= set(frozen)
    assert all(source_state.ERROR_CODES[code]==frozen[code] for code in codes)
    assert frozen['RENEWAL_LOGIN_FAILED']=='AUTH'
    assert frozen['RENEWAL_NOT_ELIGIBLE']=='INPUT'
    assert frozen['RENEWAL_LOAN_PAGE_REPEATED']=='COMPLETENESS'
    assert frozen['LIBRARY_RENEW_DISABLED']=='PERMISSION'


class WirePage:
    main_frame=object()
    def __init__(self,url,body,variant,observed=True):
        self.url,self.body,self.variant,self.observed=url,body,variant,observed
        self.continued=0;self.cleaned=False
    def evaluate(self,script,args):
        if script is ra_transport._PREPARE:
            if self.variant=='prepare':
                raise RuntimeError('ORIGIN')
            return hashlib.sha256(self.body.encode()).hexdigest()
        if self.observed:
            raw=self.body+'!' if self.variant=='body' else self.body
            request=SimpleNamespace(url=self.url,method='GET' if self.variant=='method' else 'POST',
                                    frame=object() if self.variant=='frame' else self.main_frame,
                                    post_data=raw)
            self.guard(SimpleNamespace(request=request,abort=self._abort,
                                       continue_=lambda:setattr(self,'continued',self.continued+1)))
        return {'ok':True,'payload':{'code':0}}
    def _abort(self):
        raise RuntimeError('net::ERR_ABORTED')
    def route(self,pattern,guard):
        assert pattern.fullmatch(self.url);self.guard=guard
    def unroute(self,*args):self.cleaned=True


@pytest.mark.parametrize('variant',['good','body','method','frame','prepare'])
def test_exact_renewal_wire_binds_the_transmitted_body_and_frame(variant):
    body=json.dumps(CONTRACT.body('9001',DUE),ensure_ascii=False,separators=(',',':'))
    spec=ra_transport.specification(CONTRACT,'9001',DUE)
    gate=RequestGate([spec])
    page=WirePage(spec.url,body,variant)
    reader=SimpleNamespace(page=page)
    if variant=='good':
        assert ra_transport.dispatch_exact(reader,CONTRACT,'9001',DUE,gate,True) is True
    else:
        with pytest.raises(NotDispatched):
            ra_transport.dispatch_exact(reader,CONTRACT,'9001',DUE,gate,True)
    assert gate.sent==page.continued==int(variant=='good')
    assert page.cleaned==(variant!='prepare')


def test_unobserved_request_is_not_reported_as_not_dispatched():
    body=json.dumps(CONTRACT.body('9001',DUE),ensure_ascii=False,separators=(',',':'))
    spec=ra_transport.specification(CONTRACT,'9001',DUE)
    page=WirePage(spec.url,body,'good',observed=False)
    with pytest.raises(RuntimeError,match='DISPATCH_RESULT_UNOBSERVED'):
        ra_transport.dispatch_exact(SimpleNamespace(page=page),CONTRACT,'9001',DUE,
                                    RequestGate([spec]),True)


def test_form_encoded_contract_builds_a_form_specification():
    form=ra_contract.SourceContract(contract_id='synthetic-form',
        origin='https://discover.lib.tsinghua.edu.cn',path='/__synthetic__/renew',
        encoding='form',evidence_state='SYNTHETIC')
    spec=ra_transport.specification(form,'9001',DUE)
    assert spec.encoding=='form' and 'loan_id=9001' in spec.body and 'due_at=' in spec.body


def test_concurrent_renewal_execute_sends_at_most_once(world):
    from concurrent.futures import ThreadPoolExecutor
    plan=prepare(world)
    world.lost=True
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:world.engine.execute(plan['plan_id'],AUTH),range(4)))
    assert world.sent==1
    assert not any(row['status']=='SUCCEEDED' for row in results)
    assert world.engine.result(plan['plan_id'])['status']=='UNKNOWN' and world.sent==1


def test_preflight_only_requires_the_declared_renewal_checks(world):
    plan=prepare(world)
    adapter=world.engine.adapters[('library',c.RENEW_LOAN)]
    fresh=adapter.preflight(world.store.read(plan['plan_id']))
    assert set(fresh['checks'])=={'eligibility','rules'}
    assert all(value is True for value in fresh['checks'].values())


def test_library_write_switch_blocks_execute_of_a_prepared_renewal(world,monkeypatch):
    plan=prepare(world)
    monkeypatch.setenv('THU_LIBRARY_WRITE','0')
    with pytest.raises(CampusError,match='LIBRARY_WRITES_DISABLED'):
        world.engine.execute(plan['plan_id'],AUTH)
    assert world.sent==0


def test_loan_list_limit_and_unverified_due_fail_closed(world):
    class Many:
        main_frame=object()
        def __init__(self):self.page=0
        def evaluate(self,script,arg):
            self.page+=1
            rows=[{'loanid':str(self.page*10+i),'title':'合成图书','duedate':'20991231','renew':'Y'}
                  for i in range(10)]
            return {'code':200,'data':{'data':{'loans':{'loan':rows,'showmore':['Y']}}}}
        def locator(self,selector):
            return SimpleNamespace(count=lambda:1,inner_text=lambda:'合成图书',
                                   all_text_contents=lambda:[])
    saved=world.page
    world.page=Many()
    with pytest.raises(CampusError,match='RENEWAL_LOAN_LIST_LIMIT'):prepare(world)
    world.page=saved
    world.loans=[{'loanid':'9001','title':'合成图书','renew':'Y','renewstatuses':{'renewstatus':[]}}]
    with pytest.raises(CampusError,match='LIBRARY_LOAN_DATE_INVALID'):prepare(world)


def test_duplicate_loan_projection_is_rejected(world,monkeypatch):
    rows,_=lib.parse_loans({'code':200,'data':{'data':{'loans':{'loan':[loan(),loan()],
                                                              'showmore':['N']}}}})
    monkeypatch.setattr(ra_adapter,'read_loans',lambda reader:rows)
    with pytest.raises(CampusError,match='RENEWAL_RECORD_NOT_UNIQUE'):prepare(world)


def test_reader_maps_login_and_page_failures_to_stable_codes():
    from thu_assistant.renewal import adapter as module
    class Broken:
        def open(self,**kwargs):raise RuntimeError('browser')
        def close(self,sid):pass
    with pytest.raises(CampusError,match='RENEWAL_LOGIN_FAILED'):
        with module.Reader(Broken()):pass
    class WrongPage:
        def open(self,**kwargs):return {'session_id':'session'}
        def close(self,sid):pass
        sessions={'session':{'page':SimpleNamespace(url='https://example.edu/x')}}
    with pytest.raises(CampusError,match='AUTH_REQUIRED'):
        with module.Reader(WrongPage()):pass
