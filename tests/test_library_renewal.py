"""5.1 renewal: eligibility read, one bound request and new-due readback.

Every test uses a synthetic Primo page and synthetic transport.  No renewal is
sent to the real site; the production contract registry stays empty.
"""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from thu_assistant import library as lib
from thu_assistant import library_contracts as c
from thu_assistant import renewal as renewal_pkg
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
    obj=SimpleNamespace(loans=[loan()],more=False,disagree=False,contract=CONTRACT,sent=0,sent_body=None,
                        lost=False,accepted=True,after=None,account='primo-account',
                        store=make_action_store(tmp_path))
    obj.page=LoansPage(obj)
    obj.browsers=SimpleNamespace(sessions={'session':{'account_id':'account'}})
    class Reader:
        sid='session'
        def __init__(self,browsers): self.page=obj.page; self.account=obj.account
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def close(self): pass
    monkeypatch.setattr(ra_adapter,'Reader',Reader)
    def send(reader,contract,loan_id,due_at,gate):
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
    def make(contract=CONTRACT):
        return ActionEngine(store=obj.store,account_provider=lambda:'account',consent_verifier=grant,
            adapters={('library',c.RENEW_LOAN):ra_adapter.RenewalAction(obj.browsers,contract)})
    obj.make=make
    obj.engine=make()
    return obj


def prepare(world,record_id='9001'):
    return world.engine.prepare('library',c.RENEW_LOAN,{'record_id':record_id})


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


def test_unverified_source_produces_an_official_handoff_and_never_posts(world):
    plan=world.make(contract=None).prepare('library',c.RENEW_LOAN,{'record_id':'9001'})
    preview=plan['preview']
    assert plan['status']=='PREPARED' and 'RENEWAL_SOURCE_CONTRACT_UNVERIFIED' in preview['handoff']
    assert preview['due_at']==DUE and preview['renewal_state']=='ELIGIBLE'
    assert preview['precheck']['preparation_state']=='NEEDS_OFFICIAL_PAGE'
    assert preview['official_url']==ra_adapter.OFFICIAL
    assert preview['record_id']=='9001' and 'subject' not in preview
    assert '合成图书' not in json.dumps(plan,ensure_ascii=False)
    result=world.make(contract=None).execute(plan['plan_id'],AUTH)
    assert result['status']=='AWAITING_OFFICIAL' and world.sent==0
    assert world.make(contract=None).execute(plan['plan_id'],AUTH)['status']=='AWAITING_OFFICIAL'
    assert world.sent==0


def test_not_eligible_overdue_or_unknown_loans_are_rejected_before_dispatch(world):
    world.loans=[loan(renew='N')]
    with pytest.raises(CampusError,match='RENEWAL_NOT_ELIGIBLE'):prepare(world)
    world.loans=[loan(due='2020-01-01')]
    with pytest.raises(CampusError,match='RENEWAL_OVERDUE'):prepare(world)
    world.loans=[loan()]
    with pytest.raises(CampusError,match='RENEWAL_RECORD_NOT_FOUND'):prepare(world,'7777')
    world.loans=[loan(hints=[])]
    plan=prepare(world)
    assert plan['status']=='PREPARED' and plan['preview']['precheck']['eligibility_state']=='ELIGIBLE'
    assert world.sent==0


def test_unknown_renewal_eligibility_needs_the_official_page(world):
    world.loans=[loan(renew=None)]
    plan=prepare(world)
    assert 'RENEWAL_ELIGIBILITY_UNVERIFIED' in plan['preview']['handoff']
    assert plan['preview']['precheck']['eligibility_state']=='UNKNOWN'
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
    assert result['todo_query']=={'services':['library.loans'],'start_date':DAY,'end_date':DAY,
                                  'include_completed':True}
    assert '合成图书' not in json.dumps(result,ensure_ascii=False)


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


class WirePage:
    main_frame=object()
    def __init__(self,url,body,variant):
        self.url,self.body,self.variant=url,body,variant
        self.continued=0
    def evaluate(self,script,args):
        if script is ra_transport._PREPARE:
            return hashlib.sha256(self.body.encode()).hexdigest()
        raw=self.body+'!' if self.variant=='body' else self.body
        request=SimpleNamespace(url=self.url,method='GET' if self.variant=='method' else 'POST',
                                frame=object() if self.variant=='frame' else self.main_frame,
                                post_data=raw)
        self.guard(SimpleNamespace(request=request,abort=lambda:None,
                                   continue_=lambda:setattr(self,'continued',self.continued+1)))
        return True
    def route(self,pattern,guard):
        assert pattern.fullmatch(self.url);self.guard=guard
    def unroute(self,*args):self.cleaned=True


@pytest.mark.parametrize('variant',['good','body','method','frame'])
def test_exact_renewal_wire_binds_the_transmitted_body_and_frame(variant):
    body=json.dumps(CONTRACT.body('9001',DUE),ensure_ascii=False,separators=(',',':'))
    spec=ra_transport.specification(CONTRACT,'9001',DUE)
    gate=RequestGate([spec])
    page=WirePage(spec.url,body,variant)
    reader=SimpleNamespace(page=page)
    if variant=='good':
        assert ra_transport.dispatch_exact(reader,CONTRACT,'9001',DUE,gate) is True
    else:
        with pytest.raises(NotDispatched):
            ra_transport.dispatch_exact(reader,CONTRACT,'9001',DUE,gate)
    assert gate.sent==page.continued==int(variant=='good') and page.cleaned


def test_concurrent_renewal_execute_sends_at_most_once(world):
    from concurrent.futures import ThreadPoolExecutor
    plan=prepare(world)
    world.lost=True
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:world.engine.execute(plan['plan_id'],AUTH),range(4)))
    assert world.sent==1
    assert not any(row['status']=='SUCCEEDED' for row in results)
    assert world.engine.result(plan['plan_id'])['status']=='UNKNOWN' and world.sent==1
