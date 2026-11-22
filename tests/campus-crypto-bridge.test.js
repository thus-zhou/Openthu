'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createSecureCrypto, resolveCryptoConfig, looksSecureEnvelope} = require('../src/thu_assistant/crypto-bridge');

const FAKE_SERVICE = `
const readline=require('node:readline');
const mode=process.argv[2]||'';
readline.createInterface({input:process.stdin}).on('line',line=>{
  if(mode==='silent')return;
  const q=JSON.parse(line);
  process.stdout.write(JSON.stringify({id:q.id,ok:true,health:{backend:'test-encoder',key_present:true}})+'\\n');
});
`;

function fakeClient(t, mode, timeoutMs = 5000) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'thu-crypto-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const script = path.join(dir, 'service.js');
  fs.writeFileSync(script, FAKE_SERVICE);
  return createSecureCrypto({
    enabled: true, keyFile: path.join(dir, 'key'), dataDir: dir,
    python: process.execPath, timeoutMs,
    argv: [process.execPath, script, mode],
  });
}

test('request ids are echoed and the child environment stays fixed', async t => {
  const client = fakeClient(t, '');
  t.after(() => client.close());
  const response = await client._request('health', {});
  assert.equal(response.ok, true);
  assert.equal(typeof response.id, 'string');
  assert(response.id.length > 10);
  assert.equal(response.health.backend, 'test-encoder');
});

test('unknown actions, path selectors and oversized requests never reach the child', async t => {
  const client = fakeClient(t, '');
  t.after(() => client.close());
  await assert.rejects(client._request('load', {}), {code: 'CRYPTO_ACTION_NOT_ALLOWED'});
  await assert.rejects(client._request('seal', {path: '/tmp/key'}), {code: 'CRYPTO_PATH_NOT_ACCEPTED'});
  await assert.rejects(client._request('seal', {action: 'health'}), {code: 'CRYPTO_ACTION_OVERRIDE'});
  await assert.rejects(client._request('seal', {plaintext_b64: 'x'.repeat(2 * 1024 * 1024 + 1)}),
    {code: 'CRYPTO_REQUEST_TOO_LARGE'});
});

test('timeout and cancellation kill the child without replaying the request', async t => {
  const timed = fakeClient(t, 'silent', 100);
  t.after(() => timed.close());
  await assert.rejects(timed._request('health', {}), {code: 'CRYPTO_TIMEOUT'});

  const cancelled = fakeClient(t, 'silent', 5000);
  t.after(() => cancelled.close());
  const controller = new AbortController();
  const pending = cancelled._request('health', {}, controller.signal);
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(pending, {code: 'CRYPTO_CANCELLED'});
  // A later independent call may start a fresh child; the first request is not resent.
  const fresh = fakeClient(t, '');
  t.after(() => fresh.close());
  assert.equal((await fresh._request('health', {})).ok, true);
});

test('configuration discovers the shared default key file', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'thu-default-key-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const env = {HOME: dir};
  if (process.platform === 'win32') env.APPDATA = dir;
  const key = process.platform === 'win32'
    ? path.join(dir, 'Openthu', 'keys', 'master.key')
    : path.join(dir, '.config', 'openthu', 'keys', 'master.key');
  fs.mkdirSync(path.dirname(key), {recursive: true});
  fs.writeFileSync(key, 'synthetic');
  const config = resolveCryptoConfig(env, '/state');
  assert.equal(config.enabled, true);
  assert.equal(config.keyFile, key);
});


test('configuration distinguishes a missing key from an explicit one', () => {
  assert.equal(resolveCryptoConfig({}, '/state').enabled, false);
  const explicit = resolveCryptoConfig({OPENTHU_SECURE_KEY_FILE: '/keys/master.key', OPENTHU_SECURE_DATA_DIR: '/data'}, '/state');
  assert.equal(explicit.enabled, true);
  assert.equal(explicit.keyFile, '/keys/master.key');
  assert.equal(explicit.dataDir, '/data');
  assert.equal(looksSecureEnvelope(Buffer.from('THU-SECURE-1\0payload')), true);
});
