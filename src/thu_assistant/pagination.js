'use strict';
// Upstream page collector.  v0.2 (module 3.2) adds explicit cancellation and
// collection metadata: a cancelled continuation keeps the first segment but is
// never reported complete, and the collector exposes how many pages it read.
const crypto=require('node:crypto');
const COLLECTOR_VERSION='page-collect-v1';
function parseCount(value){
 if(!(typeof value==='number'||typeof value==='string'&&/^\d+$/.test(value))||!Number.isSafeInteger(Number(value))||Number(value)<0)
  throw Object.assign(new Error('PORTAL_COUNT_CHANGED'),{code:'PORTAL_COUNT_CHANGED'});
 return Number(value);
}
async function collectPages(total,load,{limit=30,pageSize=50,cancel=null,binding=null}={}){
 const items=[],pages=[],warnings=[],missing_pages=[],page_errors=[],seen=new Set();let expectedPages=Math.ceil(total/pageSize),cancelled=false;
 const stopped=(code,page)=>{warnings.push(code);cancelled=true;
  for(let p=page;p<=Math.min(expectedPages,limit);p++)missing_pages.push(p);};
 for(let page=1;page<=Math.min(limit,expectedPages);page++){
  if(cancel&&cancel()){stopped('READ_CANCELLED',page);break;}
  let rows;
  try{rows=await load(page);if(!Array.isArray(rows))throw Object.assign(new Error(),{code:'PAGE_SCHEMA_CHANGED'});}
  catch(error){
   if(error.code==='CANCELLED'){
    if(!items.length)throw error; // nothing was read; the caller keeps the explicit cancel
    stopped('READ_CANCELLED',page);break;
   }
   if(['GATEWAY_EXITED','CAMPUS_GATEWAY_TIMEOUT'].includes(error.code))throw error;
   warnings.push('UPSTREAM_PAGE_FAILED');page_errors.push({page,code:/^[A-Z_]+$/.test(error.code||'')?error.code:'UPSTREAM_PAGE_FAILED'});missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;
  }
  if(page===1&&rows.length){pageSize=rows.length;expectedPages=Math.ceil(total/pageSize);}
  const digest=crypto.createHash('sha256').update(JSON.stringify(rows.map(r=>r.yyfwid||r).sort())).digest('hex');
  if(seen.has(digest)){warnings.push('REPEATED_UPSTREAM_PAGE');missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;}
  seen.add(digest);pages.push({page,count:rows.length});items.push(...rows);
  if(!rows.length&&items.length<total){warnings.push('MISSING_UPSTREAM_PAGE');missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;}
 }
 if(expectedPages>limit){warnings.push('UPSTREAM_PAGE_LIMIT');missing_pages.push(...Array.from({length:Math.min(expectedPages-limit,1000)},(_,i)=>limit+i+1));}
 if(items.length!==total&&!cancelled)warnings.push('UPSTREAM_COUNT_MISMATCH');
 if(expectedPages-limit>1000)warnings.push('MISSING_PAGE_LIST_TRUNCATED');
 const uniqueWarnings=[...new Set(warnings)],uniqueMissing=[...new Set(missing_pages)];
 const snapshotComplete=pages.length>=expectedPages&&!cancelled;
 return {items,pages,warnings:uniqueWarnings,missing_pages:uniqueMissing,page_errors,expectedPages,
   pages_read:pages.length,upstream_total:total,snapshot_complete:snapshotComplete,cancelled,
   collector_version:COLLECTOR_VERSION,binding:binding,
   complete:items.length===total&&uniqueWarnings.length===0&&!cancelled};
}
module.exports={collectPages,parseCount,COLLECTOR_VERSION};
