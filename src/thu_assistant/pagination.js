'use strict';
const crypto=require('node:crypto');
function parseCount(value){
 if(!(typeof value==='number'||typeof value==='string'&&/^\d+$/.test(value))||!Number.isSafeInteger(Number(value))||Number(value)<0)
  throw Object.assign(new Error('PORTAL_COUNT_CHANGED'),{code:'PORTAL_COUNT_CHANGED'});
 return Number(value);
}
async function collectPages(total,load,{limit=30,pageSize=50}={}){
 const items=[],pages=[],warnings=[],missing_pages=[],page_errors=[],seen=new Set();let expectedPages=Math.ceil(total/pageSize);
 for(let page=1;page<=Math.min(limit,expectedPages);page++){
  let rows;
  try{rows=await load(page);if(!Array.isArray(rows))throw Object.assign(new Error(),{code:'PAGE_SCHEMA_CHANGED'});}
  catch(error){
   if(['CANCELLED','GATEWAY_EXITED','CAMPUS_GATEWAY_TIMEOUT'].includes(error.code))throw error;
   warnings.push('UPSTREAM_PAGE_FAILED');page_errors.push({page,code:/^[A-Z_]+$/.test(error.code||'')?error.code:'UPSTREAM_PAGE_FAILED'});missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;
  }
  if(page===1&&rows.length){pageSize=rows.length;expectedPages=Math.ceil(total/pageSize);}
  const digest=crypto.createHash('sha256').update(JSON.stringify(rows.map(r=>r.yyfwid||r).sort())).digest('hex');
  if(seen.has(digest)){warnings.push('REPEATED_UPSTREAM_PAGE');missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;}
  seen.add(digest);pages.push({page,count:rows.length});items.push(...rows);
  if(!rows.length&&items.length<total){warnings.push('MISSING_UPSTREAM_PAGE');missing_pages.push(...Array.from({length:Math.min(expectedPages,limit)-page+1},(_,i)=>page+i));break;}
 }
 if(expectedPages>limit){warnings.push('UPSTREAM_PAGE_LIMIT');missing_pages.push(...Array.from({length:Math.min(expectedPages-limit,1000)},(_,i)=>limit+i+1));}
 if(items.length!==total)warnings.push('UPSTREAM_COUNT_MISMATCH');
 if(expectedPages-limit>1000)warnings.push('MISSING_PAGE_LIST_TRUNCATED');
 return {items,pages,warnings:[...new Set(warnings)],missing_pages:[...new Set(missing_pages)],page_errors,expectedPages,
   complete:items.length===total&&warnings.length===0};
}
module.exports={collectPages,parseCount};
