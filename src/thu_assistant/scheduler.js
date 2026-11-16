'use strict';
const {setTimeout: sleep} = require('node:timers/promises');
const failure = code => Object.assign(new Error(code), {code});
const errorRecord = error => ({code: /^[A-Z_]+$/.test(error?.code || '') ? error.code :
  error?.name === 'TimeoutError' ? 'NETWORK_TIMEOUT' : 'CAMPUS_ACCESS_FAILED',
  ...(Number.isFinite(error?.retryAfterMs) ? {retry_after_ms:error.retryAfterMs} : {}),
  ...(error?.modelEvidence ? {model_evidence:error.modelEvidence} : {})});
async function withPidLock(locks,file,action,{signal,progress=()=>{},waitMs=30000,busyCode='AUTH_BUSY'}={}) {
  const deadline=Date.now()+waitMs;let announced=false;
  for (;;) {
    signal?.throwIfAborted();
    if(announced&&Date.now()>=deadline)throw Object.assign(failure(busyCode),{retryAfterMs:1000});
    const held=await locks.acquirePidLock(file);
    if(held.ok) {try{
      signal?.throwIfAborted();
      if(announced&&Date.now()>deadline)throw Object.assign(failure(busyCode),{retryAfterMs:1000});
      return await action();
    }finally{locks.releasePidLock(file);}}
    if(!announced){progress({status:busyCode==='AUTH_BUSY'?'WAITING_FOR_AUTH':'WAITING_FOR_SESSION'});announced=true;}
    const remaining=deadline-Date.now();
    if(remaining<=0)throw Object.assign(failure(busyCode),{retryAfterMs:1000});
    await sleep(Math.min(250,remaining),undefined,{signal});
  }
}
class AuthGate {
  constructor(locks,file){this.locks=locks;this.file=file;this.pending=null;this.blocked=null;}
  reset(){this.blocked=null;}
  async run(action,options={}){
    if(this.blocked)throw failure(this.blocked);
    if(this.pending)return this.pending;
    this.pending=withPidLock(this.locks,this.file,action,options).catch(error=>{
      if(['TWO_FACTOR_REQUIRED','BAD_CREDENTIALS'].includes(error.code))this.blocked=error.code;
      throw error;
    });
    try{return await this.pending;}finally{this.pending=null;}
  }
}
class SerialQueue {
  constructor(handler,emit){this.handler=handler;this.emit=emit;this.queue=[];this.tasks=new Map();this.active=null;this.closed=false;}
  accept(message){
    const id=message?.request_id;
    if(typeof id!=='string'||!id||id.length>100)return;
    if(message.type==='cancel'){this.cancel(id);return;}
    if(message.type!=='request'||this.closed||this.tasks.has(id))return;
    if(this.tasks.size>=64){this.emit({request_id:id,ok:false,error:{code:'QUEUE_FULL'}});return;}
    const timeout=Math.min(240000,Math.max(1,Number(message.timeout_ms)||240000));
    const task={id,command:message.command,controller:new AbortController(),started:false};
    task.timer=setTimeout(()=>this.cancel(id,task.started?'CAMPUS_GATEWAY_TIMEOUT':'QUEUE_TIMEOUT'),timeout);
    this.tasks.set(id,task);this.queue.push(task);this.progress(task,{status:'QUEUED'});this.pump();
  }
  progress(task,event){this.emit({request_id:task.id,type:'progress',...event});}
  finish(task,response){
    if(!this.tasks.delete(task.id))return;
    clearTimeout(task.timer);this.emit({request_id:task.id,...response});
  }
  cancel(id,code='CANCELLED'){
    const task=this.tasks.get(id);if(!task)return;
    task.controller.abort(failure(code));
    if(!task.started){this.queue=this.queue.filter(t=>t!==task);this.finish(task,{ok:false,error:{code}});}
  }
  async pump(){
    if(this.active)return;
    const task=this.queue.shift();if(!task)return;
    this.active=task;task.started=true;this.progress(task,{status:'RUNNING'});
    try{
      const result=await this.handler(task.command,{signal:task.controller.signal,progress:event=>this.progress(task,event)});
      task.controller.signal.throwIfAborted();this.finish(task,{ok:true,result});
    }catch(error){
      const selected=task.controller.signal.aborted?Object.assign(new Error(),task.controller.signal.reason,
        error.modelEvidence?{modelEvidence:error.modelEvidence}:{}):error;
      this.finish(task,{ok:false,error:errorRecord(selected)});
    }
    finally{this.active=null;this.pump();}
  }
  close(){this.closed=true;for(const id of this.tasks.keys())this.cancel(id,'GATEWAY_EXITED');}
}
module.exports={withPidLock,AuthGate,SerialQueue,errorRecord};
