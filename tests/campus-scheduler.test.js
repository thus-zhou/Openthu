'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),{setTimeout:sleep}=require('node:timers/promises');
const {SerialQueue,AuthGate,withPidLock,errorRecord}=require('../src/thu_assistant/scheduler');

test('serial queue matches IDs and never overlaps handlers',async()=>{
 let active=0,max=0;const responses=[];
 const q=new SerialQueue(async c=>{max=Math.max(max,++active);await sleep(10);active--;return c;},r=>responses.push(r));
 for(let n=0;n<8;n++)q.accept({type:'request',request_id:String(n),command:n});
 while(q.tasks.size)await sleep(10);
 assert.equal(max,1);assert.deepEqual(responses.filter(r=>r.ok).map(r=>[r.request_id,r.result]),Array.from({length:8},(_,n)=>[String(n),n]));
});
test('queue cancellation and deadline remove work before execution',async()=>{
 const ran=[],responses=[];
 const q=new SerialQueue(async c=>{ran.push(c);await sleep(80);return c;},r=>responses.push(r));
 q.accept({type:'request',request_id:'active',command:1});
 q.accept({type:'request',request_id:'cancel',command:2});
 q.accept({type:'request',request_id:'expire',command:3,timeout_ms:10});
 q.accept({type:'cancel',request_id:'cancel'});
 while(q.tasks.size)await sleep(10);
 assert.deepEqual(ran,[1]);assert.equal(responses.find(r=>r.request_id==='cancel'&&r.error).error.code,'CANCELLED');
 assert.equal(responses.find(r=>r.request_id==='expire'&&r.error).error.code,'QUEUE_TIMEOUT');
});
test('active cancellation finishes cleanup before next task; close drains queued IDs',async()=>{
 const events=[],responses=[];
 const q=new SerialQueue(async(c,{signal})=>{try{await sleep(80,undefined,{signal});}finally{events.push(c);}return c;},r=>responses.push(r));
 q.accept({type:'request',request_id:'first',command:1});
 q.accept({type:'request',request_id:'second',command:2});
 q.close();
 while(q.tasks.size)await sleep(5);
 assert.deepEqual(events,[1]);assert.equal(responses.filter(r=>r.error?.code==='GATEWAY_EXITED').length,2);
});
test('auth single flight shares refresh and latches MFA until explicit reset',async()=>{
 let acquired=0,released=0,refreshes=0;
 const gate=new AuthGate({acquirePidLock:async()=>{acquired++;return {ok:true};},releasePidLock:()=>released++},'synthetic');
 const action=async()=>{refreshes++;await sleep(20);return 42;};
 assert.deepEqual(await Promise.all([gate.run(action),gate.run(action)]),[42,42]);
 assert.equal(refreshes,1);assert.equal(acquired,released);
 await assert.rejects(gate.run(async()=>{throw Object.assign(new Error('do not expose payload'),{code:'TWO_FACTOR_REQUIRED'});}),{code:'TWO_FACTOR_REQUIRED'});
 await assert.rejects(gate.run(action),{code:'TWO_FACTOR_REQUIRED'});assert.equal(refreshes,1);
 gate.reset();assert.equal(await gate.run(action),42);
});
test('busy lock times out with retry guidance and never releases another owner',async()=>{
 let released=0;const events=[];
 await assert.rejects(withPidLock({acquirePidLock:async()=>({ok:false}),releasePidLock:()=>released++},'synthetic',()=>assert.fail(),
  {waitMs:25,progress:e=>events.push(e.status)}),e=>e.code==='AUTH_BUSY'&&e.retryAfterMs===1000);
 assert.equal(released,0);assert.deepEqual(events,['WAITING_FOR_AUTH']);
});
test('waiting lock can be cancelled; unexpected error text never enters IPC',async()=>{
 const abort=new AbortController();setTimeout(()=>abort.abort(),15);
 await assert.rejects(withPidLock({acquirePidLock:async()=>({ok:false})},'synthetic',()=>assert.fail(),{signal:abort.signal}),{name:'AbortError'});
 assert.deepEqual(errorRecord(new Error('password=synthetic')), {code:'CAMPUS_ACCESS_FAILED'});
});
