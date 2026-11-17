'use strict';
// Catalog reads never probe/generate. Transport/parser/thinking translation are
// supplied by the existing madmodel-proxy deployment.
const crypto=require('node:crypto');
const fs=require('node:fs');
const nodePath=require('node:path');
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
// --- image input ------------------------------------------------------
// A catalogue flag is not transport evidence. These helpers accept only the
// protocol profile and single-use staging files prepared by model_media.py;
// nothing here can read a user folder, a URL or an authenticated request.
const IMAGE_SHAPES=new Set(['OPENAI_CONTENT_PARTS_IMAGE_URL']);
const IMAGE_EVIDENCE=new Set(['SYNTHETIC_TRANSPORT','LIVE_READ']);
const IMAGE_TYPES=new Set(['image/jpeg','image/png','image/webp']);
const STAGE_TOKEN=/^[a-f0-9]{32}$/;
const HEX64=/^[a-f0-9]{64}$/;
function imageSpec(batch,meta) {
 if(!batch||typeof batch!=='object'||Array.isArray(batch))throw fail('MODEL_IMAGE_PROTOCOL_UNVERIFIED');
 if(!IMAGE_SHAPES.has(batch.shape)||!IMAGE_EVIDENCE.has(batch.evidence_level)||
    typeof batch.protocol_id!=='string'||!batch.protocol_id.length)
  throw fail('MODEL_IMAGE_PROTOCOL_UNVERIFIED');
 if(!meta||meta.supportImage!==true)throw fail('MODEL_IMAGE_UNSUPPORTED');
 if(batch.model_id!==meta.id)throw fail('MODEL_IMAGE_PROTOCOL_UNVERIFIED');
 if(typeof batch.staging_root!=='string'||!nodePath.isAbsolute(batch.staging_root))
  throw fail('MODEL_IMAGE_TRANSPORT_UNAVAILABLE');
 for(const key of ['max_images','max_image_bytes','max_total_bytes']){
  if(!Number.isSafeInteger(batch[key])||batch[key]<=0)throw fail('MODEL_IMAGE_PROTOCOL_UNVERIFIED');
 }
 // Frozen ceilings (contracts/v0.2/README.md, MediaLimits): a batch may ask for
 // less, never for more. Python enforces the same numbers from one source.
 if(batch.max_images>4||batch.max_image_bytes>8*1024*1024||batch.max_total_bytes>16*1024*1024)
  throw fail('MODEL_IMAGE_PROTOCOL_UNVERIFIED');
 if(!Array.isArray(batch.images)||!batch.images.length||batch.images.length>batch.max_images)
  throw fail('MODEL_IMAGE_SELECTION_INVALID');
 const seen=new Set();let total=0;
 for(const item of batch.images){
  if(!item||typeof item!=='object'||Array.isArray(item))throw fail('MODEL_IMAGE_SELECTION_INVALID');
  if(!STAGE_TOKEN.test(item.token||'')||!IMAGE_TYPES.has(item.mime_type)||
     !HEX64.test(item.sha256||'')||!Number.isSafeInteger(item.byte_size)||item.byte_size<=0)
   throw fail('MODEL_IMAGE_SELECTION_INVALID');
  if(item.byte_size>batch.max_image_bytes)throw fail('MODEL_IMAGE_LIMIT_EXCEEDED');
  if(seen.has(item.media_id))throw fail('MODEL_IMAGE_SELECTION_INVALID');
  seen.add(item.media_id);total+=item.byte_size;
 }
 if(total>batch.max_total_bytes)throw fail('MODEL_IMAGE_LIMIT_EXCEEDED');
 return {batch,images:batch.images,total_bytes:total};
}
function readStaged(spec,signal) {
 const root=nodePath.resolve(spec.batch.staging_root),parts=[];
 for(const item of spec.images){
  signal?.throwIfAborted();
  const file=nodePath.join(root,item.token+'.bin');
  if(nodePath.dirname(file)!==root)throw fail('MODEL_IMAGE_STAGING_MISMATCH');
  let fd;
  try{fd=fs.openSync(file,fs.constants.O_RDONLY|(fs.constants.O_NOFOLLOW||0));}
  catch{throw fail('MODEL_IMAGE_STAGING_MISMATCH');}
  try{
   const stat=fs.fstatSync(fd);
   if(!stat.isFile()||stat.size!==item.byte_size)throw fail('MODEL_IMAGE_STAGING_MISMATCH');
   const data=Buffer.alloc(item.byte_size);
   let offset=0;
   while(offset<data.length){
    const read=fs.readSync(fd,data,offset,data.length-offset,null);
    if(read<=0)throw fail('MODEL_IMAGE_STAGING_MISMATCH');
    offset+=read;
   }
   if(fs.readSync(fd,Buffer.alloc(1),0,1,null)>0)throw fail('MODEL_IMAGE_STAGING_MISMATCH');
   if(crypto.createHash('sha256').update(data).digest('hex')!==item.sha256)
    throw fail('MODEL_IMAGE_STAGING_MISMATCH');
   parts.push({type:'image_url',image_url:{url:'data:'+item.mime_type+';base64,'+data.toString('base64')}});
  }finally{
   try{fs.closeSync(fd);}catch{}
   // The staging file is single use; a failed request cannot leave it behind.
   try{fs.unlinkSync(file);}catch{}
  }
 }
 return parts;
}
// Text stays first inside the final user turn and images follow the selection
// order; earlier turns keep their shape exactly.
function imageMessages(command,parts) {
 const messages=command.messages?JSON.parse(JSON.stringify(command.messages)):
  [{role:'user',content:String(command.prompt||'')}];
 const last=messages[messages.length-1];
 if(!last||last.role!=='user'||typeof last.content!=='string'||!last.content.trim())
  throw fail('MODEL_IMAGE_MESSAGES_INVALID');
 last.content=[{type:'text',text:last.content},...parts];
 return messages;
}
async function generate({command,directory,configuredModel,getToken,createClient,applyThinking,signal,progress=()=>{},redact=x=>x}) {
 const models=normalized(directory.models);
 if(command.model&&!models.some(x=>x.id===command.model))throw fail('MODEL_NOT_IN_CATALOG');
 const model=command.model||(models.some(x=>x.id===configuredModel)?configuredModel:models[0].id);
 const meta=models.find(x=>x.id===model);
 let output='',usage=null,finish_reason=null,started=false,refreshed=false,dispatched=false;
 let imageInput=null,imageParts=null,imageSends=0;
 const rawBatch=command.image_batch;
 if(rawBatch&&typeof rawBatch==='object'&&Array.isArray(rawBatch.images)){
  // Counts and identifiers only: a refused call still explains its scope
  // without carrying a token, a path or an image byte.
  imageInput={count:rawBatch.images.length,total_bytes:rawBatch.images.reduce(
    (total,item)=>total+(Number.isSafeInteger(item&&item.byte_size)?item.byte_size:0),0),
   media_ids:rawBatch.images.map(item=>item&&item.media_id).filter(x=>typeof x==='string'),
   protocol_id:typeof rawBatch.protocol_id==='string'?rawBatch.protocol_id:null,
   evidence_level:typeof rawBatch.evidence_level==='string'?rawBatch.evidence_level:null,
   sha256:[],inline_base64:false};
 }
 const evidence=()=>({model,content:redact(output),usage,usage_state:usage?'REPORTED_BY_UPSTREAM':'UNKNOWN',
  finish_reason,catalog_checked_at:directory.checked_at,catalog_freshness:directory.freshness,
  checked_at:now(),generation_started:dispatched,partial:!!output,automatic_replay:false,
  ...(imageInput?{image_input:{...imageInput,sends:imageSends}}:{})});
 const raise=code=>{throw Object.assign(fail(code),{modelEvidence:evidence()});};
 let spec=null;
 try{spec=command.image_batch?imageSpec(command.image_batch,meta):null;}
 catch(error){raise(error.code||'MODEL_IMAGE_PROTOCOL_UNVERIFIED');}
 if(spec) {
  imageInput={...imageInput,count:spec.images.length,total_bytes:spec.total_bytes,
   media_ids:spec.images.map(x=>x.media_id),sha256:spec.images.map(x=>x.sha256),
   protocol_id:spec.batch.protocol_id,evidence_level:spec.batch.evidence_level,
   inline_base64:false};
  try{imageParts=readStaged(spec,signal);}
  catch(error){raise(signal?.aborted?(signal.reason?.code||'CANCELLED'):(error.code||'MODEL_IMAGE_STAGING_MISMATCH'));}
 }
 const target=command.messages||[{role:'user',content:String(command.prompt||'')}];
 let payloadMessages=target;
 try{payloadMessages=spec?imageMessages(command,imageParts):target;}
 catch(error){raise(error.code||'MODEL_IMAGE_MESSAGES_INVALID');}
 const payload={model,messages:payloadMessages,
   max_tokens:command.max_tokens||1024,stream:true,stream_options:{include_usage:true},thinking:false};
 applyThinking(payload,meta);
 try {
  let token=await getToken();
  for(let attempt=0;attempt<2;attempt++){
   signal?.throwIfAborted();
   let sawOutput=false;
   dispatched=true;if(spec)imageSends++;progress({status:'MODEL_REQUEST_SENT'});
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

