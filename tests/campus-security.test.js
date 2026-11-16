'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {validateUrl,assertReadUrl,toTunnel,safeUrl,redact,sanitizeJson}=require('../src/thu_assistant/security');

test('school boundary accepts all school subdomains but rejects spoofing, other schemes and ports',()=>{
  for(const u of ['https://tsinghua.edu.cn/','https://seat.lib.tsinghua.edu.cn/a'])assert.ok(validateUrl(u));
  for(const u of ['http://lib.tsinghua.edu.cn/','https://tsinghua.edu.cn.evil.invalid/',
    'https://evil.invalid/?x=tsinghua.edu.cn','https://id.tsinghua.edu.cn@evil.invalid/',
    'https://a.tsinghua.edu.cn:8443/','https://127.0.0.1/','file:///C:/x'])assert.throws(()=>validateUrl(u));
});
test('arbitrary school tunnel generation roundtrips and rejects tunneled external hosts',()=>{
  const u=toTunnel('https://seat.lib.tsinghua.edu.cn/home?a=1');
  assert.ok(validateUrl(u));
  assert.ok(u.endsWith('/home?a=1'));
  assert.throws(()=>toTunnel('https://evil.invalid/'));
  const c=require('node:crypto'),key=Buffer.from('wrdvpnisthebest!'),cipher=c.createCipheriv('aes-128-cfb',key,key);
  const bad='https://webvpn.tsinghua.edu.cn/https/'+key.toString('hex')+cipher.update('evil.invalid').toString('hex')+'/';
  assert.throws(()=>validateUrl(bad));
});
test('generic read does not invoke explicit mutation endpoints, including encoded paths',()=>{
  for(const suffix of ['/b/save.do','/b/%73ubmit','/b/x?action=delete','/logout'])
    assert.throws(()=>assertReadUrl('https://thos.tsinghua.edu.cn'+suffix));
  assert.ok(assertReadUrl('https://thos.tsinghua.edu.cn/service/list?keyword=预约'));
});
test('URLs, structured data and text redact authentication secrets without losing ordinary query terms',()=>{
  const u=safeUrl('https://learn.tsinghua.edu.cn/home/;jsessionid=SYNTHETIC?ticket=X&_csrf=Y&keyword=book#token=Z');
  assert.ok(!u.includes('SYNTHETIC'));assert.ok(!u.includes('ticket'));assert.ok(!u.includes('_csrf'));assert.ok(u.includes('keyword=book'));
  assert.equal(redact('Bearer fake-token-123 &ticket=fake-secret',['private-password']),'Bearer [REDACTED] &ticket=[REDACTED]');
  const data=sanitizeJson({token:'fake',JSESSIONID:'fake',nested:{password:'fake',name:'library'}});
  assert.equal(data.token,'[REDACTED]');assert.equal(data.JSESSIONID,'[REDACTED]');assert.equal(data.nested.name,'library');
  const signed=safeUrl('https://www.sports.tsinghua.edu.cn/read?sign=synthetic&nonce=synthetic&appId=123');
  assert.ok(!signed.includes('synthetic'));assert.ok(signed.includes('appId=123'));
});
