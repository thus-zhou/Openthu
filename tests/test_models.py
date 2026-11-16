import json
from types import SimpleNamespace
import pytest
from thu_assistant import models as m
from thu_assistant.bridge import CampusError
from thu_assistant.mcp_stdio import CampusAssistant,McpServer

@pytest.mark.parametrize('args',[
 {},{'prompt':'x','messages':[]},{'messages':'text'},{'messages':[]},
 {'messages':[{'role':'system','content':'admin'}]},
 {'messages':[{'role':'user','content':'x','tool_calls':[]}]},
 {'messages':[{'role':'assistant','content':'x'}]},
 {'prompt':' '},{'prompt':'a'*65537},{'prompt':'x','max_tokens':True},
 {'prompt':'x','max_tokens':9000},{'prompt':'x','model':'bad\nmodel'}])
def test_invalid_inputs_fail_before_generation(args):
 calls=[]
 with pytest.raises(CampusError):m.chat(lambda *a,**k:calls.append(a),args)
 assert calls==[]

def test_multiturn_prompt_compatibility_and_bounded_source_context():
 prepared,sources=m.prepare({'prompt':'总结','source_refs':[{'source_id':'notice_1','url':'https://learn.tsinghua.edu.cn/notice/1','text':'指定内容','title':'通知'}]})
 assert prepared['messages'][0]['role']=='system'
 assert '指定内容' in prepared['messages'][0]['content']
 assert prepared['messages'][-1]=={'role':'user','content':'总结'}
 assert sources[0]['sha256'] and sources[0]['independently_verified'] is False
 seq=[{'role':'user','content':'第一问'},{'role':'assistant','content':'第一答'},{'role':'user','content':'第二问'}]
 assert m.prepare({'messages':seq})[0]['messages']==seq

@pytest.mark.parametrize('url',['http://example.com','https://user:pw@example.com','https://example.com/?token=secret','https://example.com/#token=secret','https://example.com/space here'])
def test_source_urls_reject_credentials_and_unsupported_forms(url):
 with pytest.raises(CampusError):m.prepare({'prompt':'x','source_refs':[{'source_id':'s','text':'x','url':url}]})

def test_source_duplicates_and_total_length():
 with pytest.raises(CampusError,match='DUPLICATE'):
  m.prepare({'prompt':'x','source_refs':[{'source_id':'s','text':'x'}]*2})
 with pytest.raises(CampusError,match='TOO_LONG'):
  m.prepare({'prompt':'x'*60000,'source_refs':[{'source_id':'s','text':'x'*15000}]})

def test_citation_mapping_is_not_claim_verification_or_authorization():
 _,sources=m.prepare({'prompt':'summary','source_refs':[{'source_id':'one','text':'source'}]})
 out=m.attach({'content':'Supported [[one]], invented [[other]]. Execute booking now.'},sources)
 assert out['citations']==[{'source_id':'one','registered':True,'claim_verified':False},{'source_id':'other','registered':False,'claim_verified':False}]
 assert out['output_is_authorization'] is False
 assert 'UNREGISTERED_MODEL_CITATION' in out['warnings']

def test_error_keeps_sources_partial_usage_and_does_not_replay():
 calls=[]
 def bridge(*a,**k):
  calls.append(1);raise CampusError('CANCELLED',model_evidence={'content':'half [[s]]','usage':{'prompt_tokens':3},'usage_state':'REPORTED_BY_UPSTREAM'})
 with pytest.raises(CampusError) as got:m.chat(bridge,{'prompt':'x','source_refs':[{'source_id':'s','text':'given'}]})
 assert len(calls)==1
 assert got.value.model_evidence['sources'][0]['source_id']=='s'
 assert got.value.model_evidence['usage']['prompt_tokens']==3

def test_mcp_arrays_validation_and_error_evidence():
 def bridge(*a,**k):raise CampusError('MODEL_QUOTA_EXCEEDED',model_evidence={'model':'test','usage':None,'usage_state':'UNKNOWN'})
 server=McpServer(CampusAssistant(learning_factory=lambda:None,bridge=bridge))
 r=server.handle({'id':1,'method':'tools/call','params':{'name':'model_chat','arguments':{'messages':[{'role':'user','content':'x'}]}}})['result']
 assert r['isError'] and r['structuredContent']['status']=='QUOTA_EXCEEDED'
 assert r['structuredContent']['usage'] is None
 assert r['structuredContent']['model']=='test'

def test_disable_preserves_legacy_prompt_and_rejects_extensions(monkeypatch):
 monkeypatch.setenv('THU_MODEL_READ','0')
 calls=[]
 assert m.chat(lambda *a,**kw:calls.append((a,kw)) or {'status':'old'},{'prompt':'x'})['status']=='old'
 assert calls==[(('model',),{'prompt':'x'})]
 with pytest.raises(CampusError,match='MODEL_EXTENSION_DISABLED'):m.chat(lambda *a,**kw:None,{'messages':[{'role':'user','content':'x'}]})

