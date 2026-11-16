'use strict';
// Catalog reads never probe/generate. Transport/parser/thinking translation are
// supplied by the existing madmodel-proxy deployment.
const crypto=require('node:crypto');
const ORIGIN='https://madmodel.cs.tsinghua.edu.cn';
const ID=/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const fail=code=>Object.assign(new Error(code),{code});
const now=()=>new Date().toISOString();
const TTL=15*60*1000;
function normalized(rows) {
 if(!Array.isArray(rows)||!rows.length||rows.length>100)throw fail('MODEL_CATALOG_INVALID');
 const seen=new Set();
 return rows.map(r=>{
  if(!r||!ID.test(r.id)||seen.has(r.id))throw fail('MODEL_CATALOG_INVALID');
  seen.add(r.id);
  return {id:r.id,label:typeof r.label==='string'?r.label.slice(0,200):r.id,
   supportImage:typeof r.supportImage==='boolean'?r.supportImage:null,
   thinkingParam:['thinking','enable_thinking',null].includes(r.thinkingParam)?r.thinkingParam:null,
   thinkingField:['content','reasoning','reasoning_content'].includes(r.thinkingField)?r.thinkingField:null,
   effortOptions:Array.isArray(r.effortOptions)?r.effortOptions.filter(x=>['low','medium','high','xhigh','max'].includes(x)):[],
   availability:'LISTED_NOT_PROBED'};
 });
}
async function limited(response,limit) {
 if(!response.ok)throw fail(response.status===429?'MODEL_CATALOG_RATE_LIMITED':'MODEL_CATALOG_UNAVAILABLE');
 const reader=response.body.getReader(),parts=[];let bytes=0;
 try{
  for(;;){const {done,value}=await reader.read();if(done)break;bytes+=value.length;
   if(bytes>limit)throw fail('MODEL_CATALOG_TOO_LARGE');parts.push(value);}
  return Buffer.concat(parts).toString('utf8');
 }finally{try{await reader.cancel();}catch{}}
}
async function catalog({cached,refresh=false,parseModelList,fetchImpl=fetch,signal}) {
 const checked=Date.parse(cached?.checked_at);
 const valid=Number.isFinite(checked)&&checked<=Date.now()&&cached?.source?.url===ORIGIN+'/';
 const wrap=(value,freshness,error)=>({...structuredClone(value),freshness,status:freshness==='STALE'?'STALE':'OK',
  available_now_verified:false,request_checked_at:now(),...(error?{warnings:[error]}:{warnings:[]})});
 if(valid&&!refresh&&Date.now()-checked<TTL) {
  try{normalized(cached.models);return wrap(cached,'CACHED');}catch{}
 }
 try {
  const local=signal?AbortSignal.any([signal,AbortSignal.timeout(20000)]):AbortSignal.timeout(20000);
  const html=await limited(await fetchImpl(ORIGIN+'/',{redirect:'manual',signal:local}),1024*1024);
  const match=/src=["']([^"']*index-[^"']*\.js[^"']*)["']/.exec(html);
  if(!match)throw fail('MODEL_CATALOG_INVALID');
  const url=new URL(match[1],ORIGIN);
  if(url.origin!==ORIGIN||url.username||url.password||url.search||url.hash||!url.pathname.startsWith('/assets/'))
   throw fail('MODEL_CATALOG_SOURCE_REJECTED');
  const source=await limited(await fetchImpl(url.href,{redirect:'manual',signal:local}),8*1024*1024);
  const models=normalized(parseModelList(source));
  return wrap({models,checked_at:now(),source:{url:ORIGIN+'/',bundle_url:url.href,
   sha256:crypto.createHash('sha256').update(source).digest('hex'),kind:'OFFICIAL_FRONTEND_CATALOG'}},'LIVE');
 }catch(error){
  if(signal?.aborted)throw signal.reason;
  if(valid){try{normalized(cached.models);return wrap(cached,'STALE',error.code||'MODEL_CATALOG_UNAVAILABLE');}catch{}}
  throw fail(error.code||'MODEL_CATALOG_UNAVAILABLE');
 }
}
function usageOf(raw) {
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return null;
 const out={};
 for(const k of ['prompt_tokens','completion_tokens','total_tokens']){
  if(Number.isSafeInteger(raw[k])&&raw[k]>=0)out[k]=raw[k];
 }
 return Object.keys(out).length?out:null;
}
function errorCode(r) {
 if(r.type==='aborted')return 'CANCELLED';
 if(r.type==='timeout')return 'MODEL_TIMEOUT';
 if(r.type==='network-error')return 'MODEL_NETWORK_ERROR';
 if(r.type==='protocol-error')return r.reason==='truncated'?'MODEL_STREAM_INTERRUPTED':'MODEL_RESPONSE_INVALID';
 const body=r.body||{},err=body.error;
 const msg=JSON.stringify([body.message,body.errorMessage,typeof err==='object'?err.message:err,typeof err==='object'?err.code:'']);
 if(r.status===401)return 'MODEL_AUTH_REJECTED';
 if(/quota|余额|额度|配额|insufficient|欠费/i.test(msg))return 'MODEL_QUOTA_EXCEEDED';
 if(/模型不存在|model.*(?:not.?found|does not exist)|model_not_found/i.test(msg))return 'MODEL_NOT_FOUND';
 if(/content.?filter|safety|内容.*(?:拒绝|违规)|敏感/i.test(msg))return 'MODEL_CONTENT_REFUSED';
 if(r.status===429)return 'MODEL_RATE_LIMITED';
 if(r.status===403)return 'MODEL_FORBIDDEN';
 if(r.status>=300&&r.status<400)return 'MODEL_SESSION_REDIRECT';
 return 'MODEL_UPSTREAM_ERROR';
}
async function generate({command,directory,configuredModel,getToken,createClient,applyThinking,signal,progress=()=>{},redact=x=>x}) {
 const models=normalized(directory.models);
 if(command.model&&!models.some(x=>x.id===command.model))throw fail('MODEL_NOT_IN_CATALOG');
 const model=command.model||(models.some(x=>x.id===configuredModel)?configuredModel:models[0].id);
 const meta=models.find(x=>x.id===model);
 let output='',usage=null,finish_reason=null,started=false,refreshed=false,dispatched=false;
 const evidence=()=>({model,content:redact(output),usage,usage_state:usage?'REPORTED_BY_UPSTREAM':'UNKNOWN',
  finish_reason,catalog_checked_at:directory.checked_at,catalog_freshness:directory.freshness,
  checked_at:now(),generation_started:dispatched,partial:!!output,automatic_replay:false});
 const raise=code=>{throw Object.assign(fail(code),{modelEvidence:evidence()});};
 const payload={model,messages:command.messages||[{role:'user',content:String(command.prompt||'')}],
   max_tokens:command.max_tokens||1024,stream:true,stream_options:{include_usage:true},thinking:false};
 applyThinking(payload,meta);
 try{
  let token=await getToken();
  for(let attempt=0;attempt<2;attempt++){
   signal?.throwIfAborted();
   let sawOutput=false;
   dispatched=true;progress({status:'MODEL_REQUEST_SENT'});
   const r=await createClient().request({token:token.token,cookie:token.cookie,signal,payload,
    onOpen:()=>{started=true;progress({status:'MODEL_STREAM_OPEN'});},
    onChunk:chunk=>{
     const choice=chunk?.choices?.[0],delta=choice?.delta;
     if(chunk?.usage)usage=usageOf(chunk.usage)||usage;
     if(choice?.finish_reason)finish_reason=String(choice.finish_reason).slice(0,80);
     if(typeof delta?.content==='string')output+=delta.content;
     if(output.length>1024*1024)throw fail('MODEL_OUTPUT_TOO_LARGE');
     if(!sawOutput&&(delta?.content||delta?.reasoning_content||delta?.reasoning)){
      sawOutput=true;started=true;progress({status:'MODEL_OUTPUT_RECEIVED'});}
    }});
   if(attempt===0&&!started&&r.type==='upstream-error'&&r.status===401){
    token=await getToken(token.token);refreshed=true;continue;
   }
   if(r.type==='completion'){
    const choice=r.body?.choices?.[0];
    if(typeof choice?.message?.content==='string')output=choice.message.content;
    if(choice?.message?.refusal)raise('MODEL_CONTENT_REFUSED');
    finish_reason=typeof choice?.finish_reason==='string'?choice.finish_reason:null;
    usage=usageOf(r.body?.usage)||usage;
   }else usage=usageOf(r.usage)||usage;
   if(signal?.aborted)raise(signal.reason?.code||'CANCELLED');
   if(!['stream','completion'].includes(r.type))raise(errorCode(r));
   if(finish_reason==='content_filter')raise('MODEL_CONTENT_REFUSED');
   if(!output.trim())raise('MODEL_EMPTY_RESPONSE');
   if(output.length>1024*1024)raise('MODEL_OUTPUT_TOO_LARGE');
   return {...evidence(),status:'GENERATION_VERIFIED',authentication_state:'AUTHENTICATED',
    partial:finish_reason==='length',complete:finish_reason!=='length',refreshed,
    warnings:directory.freshness==='STALE'?['MODEL_SELECTED_FROM_STALE_CATALOG']:[]};
  }
 }catch(error){
  if(error.modelEvidence)throw error;
  raise(signal?.aborted?(signal.reason?.code||'CANCELLED'):(error.code||'MODEL_NETWORK_ERROR'));
 }
}
module.exports={ORIGIN,ID,catalog,normalized,usageOf,errorCode,generate};

