'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'thu-campus-download-'));
process.env.MADMODEL_STATE_DIR=path.join(temporary,'unused-auth');
const {downloadFile,pageResult}=require('../src/thu_assistant/gateway');
const originalFetch=globalThis.fetch;
process.once('exit',()=>{
  const resolved=fs.realpathSync(temporary);
  if(path.dirname(resolved)===fs.realpathSync(os.tmpdir())&&path.basename(resolved).startsWith('thu-campus-download-'))fs.rmSync(resolved,{recursive:true,force:true});
});
const jar={cookies:{},headerFor:()=>'',absorb:()=>{}};

test('long pages can be read without gaps or silently losing their tail',()=>{
  const response={statusCode:200,finalUrl:'https://lib.tsinghua.edu.cn/article',body:'<html><body>'+('abcdef '.repeat(10000))+'</body></html>'};
  const first=pageResult(response,{limit:40000}),last=pageResult(response,{offset:first.nextOffset,limit:40000});
  assert.equal(first.text+last.text,'abcdef '.repeat(10000).trim());
  assert.equal(last.nextOffset,null);assert.equal(first.totalCharacters,69999);
});
test('download records actual bytes and hash, redacts signed query, and refuses overwrite',async()=>{
  const payload=Buffer.from('synthetic school document\n');
  globalThis.fetch=async()=>new Response(payload,{headers:{'content-type':'text/plain','content-length':String(payload.length)}});
  const target=path.join(temporary,'document.txt');
  try{
    const result=await downloadFile({jar},null,'https://lib.tsinghua.edu.cn/file?ticket=synthetic',target);
    assert.equal(result.bytes,payload.length);assert.equal(result.sha256,crypto.createHash('sha256').update(payload).digest('hex'));
    assert.deepEqual(fs.readFileSync(target),payload);assert.ok(!result.url.includes('synthetic'));
    await assert.rejects(downloadFile({jar},null,'https://lib.tsinghua.edu.cn/file',target),/DOWNLOAD_TARGET_ALREADY_EXISTS/);
    assert.deepEqual(fs.readFileSync(target),payload);
  }finally{globalThis.fetch=originalFetch;}
});
test('HTML login page masquerading as a file never becomes a delivered PDF',async()=>{
  globalThis.fetch=async()=>new Response('<title>登录</title><input type="password">',{headers:{'content-type':'text/html'}});
  const target=path.join(temporary,'fake.pdf');
  try{
    await assert.rejects(downloadFile({jar},null,'https://lib.tsinghua.edu.cn/file.pdf',target),/DOWNLOAD_RETURNED_HTML_NOT_FILE/);
    assert.equal(fs.existsSync(target),false);
  }finally{globalThis.fetch=originalFetch;}
});
test('a JSON error with HTTP 200 cannot masquerade as a PDF',async()=>{
  globalThis.fetch=async()=>new Response('{"error":"synthetic-auth-error"}',{headers:{'content-type':'application/json'}});
  const target=path.join(temporary,'fake-json.pdf');
  try{
    await assert.rejects(downloadFile({jar},null,'https://lib.tsinghua.edu.cn/file.pdf',target),/DOWNLOAD_FILE_SIGNATURE_MISMATCH/);
    assert.equal(fs.existsSync(target),false);
  }finally{globalThis.fetch=originalFetch;}
});
test('oversized chunked downloads remove their temporary file and deliver nothing',async()=>{
  globalThis.fetch=async()=>new Response(Buffer.alloc(4096),{headers:{'content-type':'application/octet-stream'}});
  const target=path.join(temporary,'too-large.bin');
  try{
    await assert.rejects(downloadFile({jar},null,'https://lib.tsinghua.edu.cn/file',target,1024),/DOWNLOAD_BYTE_LIMIT/);
    assert.equal(fs.existsSync(target),false);
    assert.equal(fs.readdirSync(temporary).some(name=>name.startsWith('too-large.bin.')),false);
  }finally{globalThis.fetch=originalFetch;}
});
