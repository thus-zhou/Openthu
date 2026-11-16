'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {SessionStore, identity} = require('../src/thu_assistant/session-store');
const {CampusCookies, allowedUrls} = require('../src/thu_assistant/cookies');
const {browserSync} = require('../src/thu_assistant/session-policy');
const {resolveCryptoConfig, childEnvironment, looksSecureEnvelope} = require('../src/thu_assistant/crypto-bridge');

const origin = 'https://learn.tsinghua.edu.cn';
const cookie = (jar, text) => jar.absorb(origin + '/course/list', text);

function fakeCrypto() {
  let calls = 0;
  return {
    async health() { return {backend: 'test-encoder', key_id: 'key_test', key_present: true}; },
    async seal(buffer, context) {
      calls += 1;
      const body = JSON.stringify({revision: context.revision, record: context.recordId, data: buffer.toString('base64')});
      return {envelope: Buffer.concat([Buffer.from('TEST-SECURE-1\0'), Buffer.from(body)]),
        context: {purpose: 'SESSION', account_domain: 'CAMPUS', service_domain: 'LOCAL',
          revision: context.revision, record_id: context.recordId}};
    },
    async unseal(buffer, context) {
      calls += 1;
      const body = JSON.parse(buffer.subarray(Buffer.from('TEST-SECURE-1\0').length).toString('utf8'));
      if (context.expectedRevision !== undefined && body.revision !== context.expectedRevision) {
        throw Object.assign(new Error('revision'), {code: 'SECURE_REVISION_CONFLICT'});
      }
      return {plaintext: Buffer.from(body.data, 'base64'),
        context: {purpose: 'SESSION', account_domain: 'CAMPUS', service_domain: 'LOCAL',
          revision: body.revision, record_id: body.record}};
    },
    calls: () => calls,
  };
}

function fixture(t, {crypto} = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'thu-session-v3-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const held = new Set();
  const locks = {
    acquirePidLock: async file => { assert(!held.has(file)); held.add(file); return {ok: true}; },
    releasePidLock: file => held.delete(file),
  };
  const store = new SessionStore({
    dir, account: {username: 'synthetic'}, crypto,
    platform: 'linux', locks,
    atomicWrite: (file, data) => { const tmp = file + '.tmp'; fs.writeFileSync(tmp, data); fs.renameSync(tmp, file); },
  });
  return {dir, store, held};
}

test('secure store commits and reloads the same revision without plaintext on disk', async t => {
  const crypto = fakeCrypto();
  const {dir, store, held} = fixture(t, {crypto});
  const base = await store.load();
  const jar = new CampusCookies();
  cookie(jar, 'sid=secure; Path=/; Secure; Max-Age=600');
  const saved = await store.commit(base, jar, {});
  assert.equal(saved.version, 3);
  assert.equal(saved.revision, 1);
  assert.equal(saved.migration.saved, true);
  assert.equal(saved.migration.verified, true);
  const raw = fs.readFileSync(store.secureFile);
  assert.equal(raw.subarray(0, 14).toString('ascii'), 'TEST-SECURE-1\0');
  assert(!raw.includes(Buffer.from('secure; Path')));
  const loaded = await store.load();
  assert.equal(loaded.revision, 1);
  assert.equal(new CampusCookies(loaded.cookies).valueFor(origin + '/', 'sid'), 'secure');
  assert.equal(held.size, 0);
});

test('legacy DPAPI state is read, backed up and only then written in the new format', async t => {
  const {dir, store} = fixture(t, {crypto: fakeCrypto()});
  const legacy = {version: 2, account: identity({username: 'synthetic'}), revision: 4, cookies: [], apps: {}};
  fs.writeFileSync(path.join(dir, 'campus-v2.dpapi'), Buffer.from(JSON.stringify(legacy)).toString('base64'));
  store.dpapi = (mode, buffer) => buffer;
  store.legacyDpapi = store.dpapi;
  store.allowLegacy = true;
  const base = await store.load();
  assert.equal(base.revision, 4);
  assert.equal(base.migration.source, 'LEGACY_DPAPI');
  assert.equal(base.migration.saved, false);
  const jar = new CampusCookies();
  cookie(jar, 'sid=migrated; Path=/');
  const saved = await store.commit(base, jar, {});
  assert.equal(saved.version, 3);
  assert.equal(saved.migration.saved, true);
  assert.equal(saved.migration.legacyRetained, true);
  assert.equal(fs.readFileSync(path.join(dir, 'campus-v2.dpapi'), 'utf8'),
    Buffer.from(JSON.stringify(legacy)).toString('base64'));
  assert.equal(fs.readFileSync(path.join(dir, 'campus-v2.dpapi.before-v3'), 'utf8'),
    Buffer.from(JSON.stringify(legacy)).toString('base64'));
});

test('account switch rejects a live store before and during commit', async t => {
  const {store} = fixture(t, {crypto: fakeCrypto()});
  const base = await store.load();
  store.currentAccount = () => ({username: 'switched'});
  await assert.rejects(store.load(), {code: 'STATE_ACCOUNT_MISMATCH'});
  await assert.rejects(store.commit(base, new CampusCookies(), {}), {code: 'STATE_ACCOUNT_MISMATCH'});
  assert(!fs.existsSync(store.secureFile));
});

test('two secure writers merge independent cookies and reject conflicting values', async t => {
  const crypto = fakeCrypto();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'thu-session-v3-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const locks = {acquirePidLock: async () => ({ok: true}), releasePidLock: () => {}};
  const make = () => new SessionStore({
    dir, account: {username: 'synthetic'}, crypto, platform: 'linux', locks,
    atomicWrite: (file, data) => { const tmp = file + '.tmp'; fs.writeFileSync(tmp, data); fs.renameSync(tmp, file); },
  });
  const a = make(), b = make();
  const baseA = await a.load(), baseB = await b.load();
  const ja = new CampusCookies(), jb = new CampusCookies();
  cookie(ja, 'a=1; Path=/'); cookie(jb, 'b=2; Path=/');
  await a.commit(baseA, ja, {});
  const merged = await b.commit(baseB, jb, {});
  assert.equal(merged.cookies.length, 2);
  const stale = await a.load();
  const j1 = new CampusCookies(stale.cookies), j2 = new CampusCookies(stale.cookies);
  cookie(j1, 'a=3; Path=/'); cookie(j2, 'a=4; Path=/');
  await a.commit(stale, j1, {});
  await assert.rejects(b.commit(stale, j2, {}), {code: 'STATE_CONFLICT'});
});

test('browser state preserves expiry and refuses another service scope', () => {
  const jar = new CampusCookies();
  cookie(jar, 'sid=root; Path=/; Secure; Max-Age=600');
  const urls = allowedUrls(origin);
  jar.setBrowser({name: 'expired', value: 'x', domain: 'learn.tsinghua.edu.cn', path: '/', expires: Date.now() / 1000 - 1}, urls);
  assert.equal(jar.valueFor(origin + '/', 'expired'), '');
  assert.throws(() => jar.setBrowser({name: 'other', value: 'y', domain: 'library.tsinghua.edu.cn', path: '/'}, urls),
    {code: 'BROWSER_COOKIE_SCOPE'});
  const boot = {version: 2, revision: 0, account: identity({username: 'synthetic'}), serviceOrigin: origin,
    url: origin + '/', cookies: jar.browserCookies(urls), cookieRecords: jar.export(), appOrigins: []};
  const apps = {};
  browserSync(jar, {cookies: [{name: 'expired', value: 'x', domain: '.learn.tsinghua.edu.cn', path: '/', expires: Date.now() / 1000 - 1}], origins: []},
    boot, boot.account, apps);
  assert.equal(jar.valueFor(origin + '/', 'expired'), '');
});

test('crypto child configuration never inherits account or path material from a request', () => {
  const config = resolveCryptoConfig({THU_SECURE_KEY_FILE: '/keys/master.key', THU_SECURE_DATA_DIR: '/state'}, '/state');
  assert.equal(config.enabled, true);
  assert.equal(config.dataDir, '/state');
  const env = childEnvironment(config);
  assert.equal(env.THU_SECURE_KEY_FILE, '/keys/master.key');
  assert.equal(env.THU_SECURE_DATA_DIR, '/state');
  assert.equal(env.THU_ASSISTANT_STATE_DIR, undefined);
  assert.equal(looksSecureEnvelope(Buffer.from('THU-SECURE-1\0rest')), true);
  assert.equal(looksSecureEnvelope(Buffer.from('other')), false);
});


test('browser sync ignores school cookies outside the current service scope', () => {
  const jar = new CampusCookies();
  const boot = {version: 2, revision: 0, account: identity({username: 'synthetic'}), serviceOrigin: origin,
    url: origin + '/', cookies: [], cookieRecords: [], appOrigins: []};
  const apps = {};
  const result = browserSync(jar, {cookies: [
    {name: 'sid', value: 'in-scope', domain: '.learn.tsinghua.edu.cn', path: '/'},
    {name: 'other', value: 'other-service', domain: 'library.tsinghua.edu.cn', path: '/'},
  ], origins: []}, boot, boot.account, apps);
  assert.equal(result.cookies.length, 1);
  assert.equal(result.cookies[0].name, 'sid');
  assert.equal(jar.valueFor(origin + '/', 'sid'), 'in-scope');
});
