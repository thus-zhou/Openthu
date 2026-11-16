'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const path=require('node:path'),{createRequire}=require('node:module');
const up=createRequire(path.join(process.env.THU_MADMODEL_DIR||path.resolve(__dirname,'../vendor/madmodel-proxy'),'package.json'));
const m=require('../src/thu_assistant/models');
const {SerialQueue}=require('../src/thu_assistant/scheduler');
const rows=[{id:'model-a',label:'A',thinkingParam:'thinking',supportImage:false}];
const directory={models:rows,checked_at:new Date().toISOString(),freshness:'LIVE'};
const args=extra=>({command:{prompt:'synthetic',model:'model-a'},directory,configuredModel:'model-a',
 getToken:async()=>({token:'fixture',cookie:'fixture'}),applyThinking:()=>{},...extra});
const fake=result=>()=>({request:async()=>result});

test('directory parses only fixed same-origin scripts, never POSTs',async()=>{
 const calls=[];
 const out=await m.catalog({parseModelList:JSON.parse,fetchImpl:async(url,opts)=>{
  calls.push(url);assert.equal(opts.redirect,'manual');assert.equal(opts.headers,undefined);
  return new Response(url.endsWith('/')?'<script src="/assets/js/index-test.js"></script>':JSON.stringify(rows));
 }});
 assert.equal(out.freshness,'LIVE');assert.equal(out.models[0].availability,'LISTED_NOT_PROBED');assert.equal(calls.length,2);
 assert.equal(out.source.sha256.length,64);
});
test('directory cache age, refresh, stale provenance and malformed historical list',async()=>{
 const cached={models:rows,source:{url:m.ORIGIN+'/'},checked_at:new Date().toISOString()};
 const bad=async()=>{throw new Error('secret token');};
 assert.equal((await m.catalog({cached,fetchImpl:()=>assert.fail()})).freshness,'CACHED');
 const old={...cached,checked_at:new Date(Date.now()-3600000).toISOString()};
 const stale=await m.catalog({cached:old,fetchImpl:bad});
 assert.equal(stale.freshness,'STALE');assert.equal(stale.checked_at,old.checked_at);
 assert.equal(stale.available_now_verified,false);assert.ok(!JSON.stringify(stale).includes('secret'));
 assert.equal((await m.catalog({cached,refresh:true,fetchImpl:bad})).freshness,'STALE');
 await assert.rejects(m.catalog({cached:{...cached,models:[]},refresh:true,fetchImpl:bad}),{code:'MODEL_CATALOG_UNAVAILABLE'});
});
test('catalog redirects, cross-origin scripts and duplicates fail closed',async()=>{
 await assert.rejects(m.catalog({parseModelList:JSON.parse,fetchImpl:async()=>new Response('',{status:302})}),{code:'MODEL_CATALOG_UNAVAILABLE'});
 await assert.rejects(m.catalog({parseModelList:JSON.parse,fetchImpl:async()=>new Response('<script src="https://outside.example/assets/index-x.js"></script>')}),{code:'MODEL_CATALOG_SOURCE_REJECTED'});
 assert.throws(()=>m.normalized([...rows,...rows]),{code:'MODEL_CATALOG_INVALID'});
 assert.throws(()=>m.normalized([{id:'ignore instructions\nsecret'}]),{code:'MODEL_CATALOG_INVALID'});
});
test('directory cancellation is not converted to stale success',async()=>{
 const c=new AbortController();c.abort(Object.assign(new Error(),{code:'CANCELLED'}));
 await assert.rejects(m.catalog({cached:{models:rows,source:{url:m.ORIGIN+'/'},checked_at:'2020-01-01'},signal:c.signal,
  fetchImpl:async()=>{throw c.signal.reason;}}),{code:'CANCELLED'});
});
test('unknown model is rejected before sending a generation',async()=>{
 await assert.rejects(m.generate(args({command:{model:'missing',prompt:'x'},getToken:()=>assert.fail(),createClient:()=>assert.fail()})),{code:'MODEL_NOT_IN_CATALOG'});
});
test('JSON completion preserves real usage and finish reason',async()=>{
 const r=await m.generate(args({createClient:fake({type:'completion',body:{choices:[{message:{content:'answer'},finish_reason:'stop'}],usage:{prompt_tokens:3,completion_tokens:2,total_tokens:5,secret:'omit'}}})}));
 assert.equal(r.content,'answer');assert.equal(r.finish_reason,'stop');assert.deepEqual(r.usage,{prompt_tokens:3,completion_tokens:2,total_tokens:5});
 assert.equal(r.complete,true);assert.equal(r.usage_state,'REPORTED_BY_UPSTREAM');
});
test('length stop is partial; missing usage is unknown, never zero',async()=>{
 const r=await m.generate(args({createClient:fake({type:'completion',body:{choices:[{message:{content:'partial'},finish_reason:'length'}]}})}));
 assert.equal(r.partial,true);assert.equal(r.complete,false);assert.equal(r.usage,null);assert.equal(r.usage_state,'UNKNOWN');
});
for(const [response,code] of [
 [{type:'upstream-error',status:403},'MODEL_FORBIDDEN'],
 [{type:'upstream-error',status:429},'MODEL_RATE_LIMITED'],
 [{type:'upstream-error',status:429,body:{error:{code:'insufficient_quota'}}},'MODEL_QUOTA_EXCEEDED'],
 [{type:'upstream-error',status:200,body:{status:10001,message:'模型不存在'}},'MODEL_NOT_FOUND'],
 [{type:'upstream-error',status:200,body:{errorMessage:'内容违规，拒绝'}},'MODEL_CONTENT_REFUSED'],
 [{type:'network-error'},'MODEL_NETWORK_ERROR'],[{type:'timeout'},'MODEL_TIMEOUT'],
 [{type:'protocol-error',reason:'truncated'},'MODEL_STREAM_INTERRUPTED'],
 [{type:'protocol-error',reason:'invalid'},'MODEL_RESPONSE_INVALID']]) {
 test('classified '+code+' is not replayed',async()=>{
  let calls=0;
  await assert.rejects(m.generate(args({createClient:()=>({request:async()=>{calls++;return response;}})})),
   e=>e.code===code&&e.modelEvidence.usage===null&&e.modelEvidence.automatic_replay===false);
  assert.equal(calls,1);
 });
}
test('only unopened HTTP 401 refreshes once; opened 401 never replays',async()=>{
 for(const opened of [false,true]){
  let calls=0,tokens=0;
  const p=m.generate(args({getToken:async()=>{tokens++;return {token:'x'};},createClient:()=>({request:async o=>{
   calls++;if(opened)o.onOpen();return calls===1?{type:'upstream-error',status:401}:{type:'completion',body:{choices:[{message:{content:'ok'}}]}};
  }})}));
  if(opened)await assert.rejects(p,{code:'MODEL_AUTH_REJECTED'});
  else assert.equal((await p).refreshed,true);
  assert.equal(calls,opened?1:2);assert.equal(tokens,calls);
 }
});
test('midstream cancellation retains partial content/usage through serial queue',async()=>{
 const messages=[];
 const q=new SerialQueue(async(_,control)=>m.generate(args({...control,createClient:()=>({request:async o=>{
  o.onOpen();o.onChunk({choices:[{delta:{content:'partial'}}],usage:{prompt_tokens:5}});
  q.cancel('one');return {type:'aborted'};
 }})})),x=>messages.push(x));
 q.accept({type:'request',request_id:'one',command:{}});
 while(q.tasks.size)await new Promise(r=>setTimeout(r,5));
 const error=messages.find(x=>x.error).error;
 assert.equal(error.code,'CANCELLED');assert.equal(error.model_evidence.content,'partial');
 assert.equal(error.model_evidence.usage.prompt_tokens,5);
});
test('existing upstream transport parses SSE and aborts an in-flight response',async()=>{
 const original=globalThis.fetch;
 const config={upstream:'https://example.invalid',tunnelMode:true,streamIdleTimeout:1000,
  streamTotalTimeout:2000,upstreamHeaderTimeout:1000,upstreamJsonBodyLimit:1024*1024,upstreamSseTotalLimit:1024*1024,sseLineLimit:10000};
 try{
  let aborted=false;
  const c=new AbortController();
  globalThis.fetch=async(url,opts)=>new Response(new ReadableStream({start(controller){
   controller.enqueue(new TextEncoder().encode('data: {"choices":[{"delta":{"content":"hello"}}],"usage":{"prompt_tokens":3}}\n\n'));
   opts.signal.addEventListener('abort',()=>{aborted=true;controller.error(new Error('aborted'));},{once:true});
  }}),{headers:{'Content-Type':'text/event-stream'}});
  await assert.rejects(m.generate(args({signal:c.signal,progress:e=>{
   if(e.status==='MODEL_OUTPUT_RECEIVED')c.abort(Object.assign(new Error(),{code:'CANCELLED'}));},
   createClient:()=>up('./core/upstream-client').createUpstreamClient(config)})),
   e=>e.code==='CANCELLED'&&e.modelEvidence.usage.prompt_tokens===3&&e.modelEvidence.content==='hello');
  assert.equal(aborted,true);
  globalThis.fetch=async()=>new Response('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"total_tokens":8}}\n\ndata: [DONE]\n\n',
   {headers:{'Content-Type':'text/event-stream'}});
  const out=await m.generate(args({createClient:()=>up('./core/upstream-client').createUpstreamClient(config)}));
  assert.equal(out.usage.total_tokens,8);assert.equal(out.finish_reason,'stop');assert.equal(out.content,'ok');
 }finally{globalThis.fetch=original;}
});

