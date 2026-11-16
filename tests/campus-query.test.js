'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {collectPages,parseCount}=require('../src/thu_assistant/pagination');
const {sanitizeJson}=require('../src/thu_assistant/security');
const {pageResult}=require('../src/thu_assistant/gateway');
test('JSON keeps more than 500 items, deep nodes and long strings while redacting secrets',()=>{
 const data={nested:{items:Array.from({length:701},(_,id)=>({id,token:'synthetic',text:'x'.repeat(25000)}))}};
 const clean=sanitizeJson(data);assert.equal(clean.nested.items.length,701);assert.equal(clean.nested.items[700].text.length,25000);assert.equal(clean.nested.items[700].token,'[REDACTED]');
 assert.throws(()=>pageResult({statusCode:200,finalUrl:'https://lib.tsinghua.edu.cn/',body:JSON.stringify(data)}),{code:'RESULT_TOO_LARGE_SELECT_NARROWER_SOURCE'});
});
test('upstream paging reaches the last page and counts exactly',async()=>{
 const r=await collectPages(5,async p=>p===1?[{yyfwid:'a'},{yyfwid:'b'}]:p===2?[{yyfwid:'c'},{yyfwid:'d'}]:[{yyfwid:'e'}]);
 assert.equal(r.items.length,5);assert.equal(r.pages.length,3);assert(r.complete);assert.deepEqual(r.missing_pages,[]);
});
test('missing/invalid counts cannot masquerade as an empty directory',()=>{
 for(const v of [null,undefined,false,'',{},-1,1.2,'NaN'])assert.throws(()=>parseCount(v),{code:'PORTAL_COUNT_CHANGED'});
 assert.equal(parseCount('0'),0);assert.equal(parseCount(306),306);
});
test('repeated/reordered pages stop instead of manufacturing completeness',async()=>{
 const r=await collectPages(6,async p=>p===1?[{yyfwid:'a'},{yyfwid:'b'}]:[{yyfwid:'b'},{yyfwid:'a'}]);
 assert(!r.complete);assert.equal(r.items.length,2);assert.deepEqual(r.missing_pages,[2,3]);assert(r.warnings.includes('REPEATED_UPSTREAM_PAGE'));
});
test('missing and failed pages retain partial data and list missing pages',async()=>{
 for(const fail of [false,true]){
  const r=await collectPages(6,async p=>{if(p===1)return [1,2];if(fail)throw Error('synthetic');return [];});
  assert(!r.complete);assert.equal(r.items.length,2);assert.deepEqual(r.missing_pages,[2,3]);
 }
});
test('page cap and cancellation are explicit',async()=>{
 const r=await collectPages(5,async p=>[p],{limit:2});assert(!r.complete);assert.deepEqual(r.missing_pages,[3,4,5]);
 await assert.rejects(collectPages(1,async()=>{throw Object.assign(Error(),{code:'CANCELLED'});}),{code:'CANCELLED'});
});
test('HTML login/maintenance and full-text snapshot are distinguished',()=>{
 const r=pageResult({statusCode:200,finalUrl:'https://id.tsinghua.edu.cn/',body:'<input type="password">'});assert.equal(r.status,'AUTH_REQUIRED');
 assert.equal(pageResult({statusCode:200,finalUrl:'https://learn.tsinghua.edu.cn/f/login_timeout',body:'会话已过期'}).status,'AUTH_REQUIRED');
 assert(pageResult({statusCode:503,finalUrl:'https://lib.tsinghua.edu.cn/',body:'<title>系统维护</title>'}).maintenance);
 const text='abc '.repeat(20000).trim();assert.equal(pageResult({statusCode:200,finalUrl:'https://lib.tsinghua.edu.cn/',body:'<body>'+text+'</body>'},{snapshot:true}).text,text);
});
