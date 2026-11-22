'use strict';

// Reuse the user's deployed protocol implementation. Never copy its password.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const { execFileSync } = require('node:child_process');
const deployed = process.env.THU_MADMODEL_DIR || path.resolve(__dirname, '../../vendor/madmodel-proxy');
const upstreamRequire = createRequire(path.join(deployed, 'package.json'));
const auth = upstreamRequire('./madmodel-auth');
const credentials = upstreamRequire('./platform/credentials');
const locks = upstreamRequire('./platform/process-lock');
const paths = upstreamRequire('./platform/paths');
const { atomicWrite } = upstreamRequire('./platform/file-store');
const stateDir = process.env.THU_ACCESS_STATE_DIR || process.env.OPENTHU_STATE_DIR ||
  (process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'THUAccess')
    : path.join(os.homedir(), '.local', 'state', 'openthu', 'THUAccess'));
const sessionFile = path.join(stateDir, 'session.dpapi');
const summaryFile = path.join(__dirname, 'last-verification.json');
const origins = new Set(['https://id.tsinghua.edu.cn', 'https://webvpn.tsinghua.edu.cn',
  'https://learn.tsinghua.edu.cn', 'https://info.tsinghua.edu.cn']);
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const failure = code => Object.assign(new Error(code), { code });
// Upstream diagnostics may include ticket-bearing URLs or HTML. Keep only our
// own structured events at this boundary, including during error handling.
console.warn = () => {};
console.error = () => {};

function dpapi(mode, input) {
  if (process.platform !== 'win32') throw failure('WINDOWS_DPAPI_REQUIRED');
  const script = ['Add-Type -AssemblyName System.Security',
    '$raw=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())',
    `$out=[Security.Cryptography.ProtectedData]::${mode === 'protect' ? 'Protect' : 'Unprotect'}($raw,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)`,
    '[Console]::Out.Write([Convert]::ToBase64String($out))'].join(';');
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows';
  const binary = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const env = {...process.env};
  for (const key of Object.keys(env)) {
    if (key.toLowerCase() === 'psmodulepath') delete env[key];
  }
  env.PSModulePath = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'Modules');
  return Buffer.from(execFileSync(binary, ['-NoProfile', '-NonInteractive', '-Command', script], {
    input: Buffer.from(input).toString('base64'), windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'], timeout: 15000, env,
  }).toString().trim(), 'base64');
}

function accountIdentity() {
  try {
    const account = credentials.readAccount();
    if (!account?.username) return '';
    return crypto.createHash('sha256').update(String(account.username)).digest('hex');
  } catch {
    return '';
  }
}

function sessionEnvironment() {
  const env = {...process.env};
  const identity = accountIdentity();
  if (identity) env.THU_LEARN_ACCOUNT = identity;
  else delete env.THU_LEARN_ACCOUNT;
  return env;
}

function sessionHelper(mode, payload) {
  try {
    const output = execFileSync(process.env.THU_PYTHON || 'python', [path.join(__dirname, 'session-store.py'), mode], {
      input: payload === undefined ? '' : JSON.stringify(payload), encoding: 'utf8', windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], timeout: 20000, env: sessionEnvironment(),
    });
    const parsed = JSON.parse(output);
    return parsed && typeof parsed === 'object' ? parsed : {ok: false, errorCode: 'HELPER_INVALID_RESPONSE'};
  } catch {
    return {ok: false, errorCode: 'HELPER_UNAVAILABLE'};
  }
}

function saveSession(jar) {
  const selected = Object.fromEntries(Object.entries(jar.cookies).filter(([origin]) => origins.has(origin)));
  const cookies = Object.entries(selected).flatMap(([origin, rows]) => rows.map(c => ({
    ...c, domain: new URL(origin).hostname,
  })));
  const saved = sessionHelper('write', {cookies});
  if (saved?.ok) return;
  if (process.env.THU_SECURE_KEY_FILE || process.env.OPENTHU_SECURE_KEY_FILE) {
    throw failure(saved?.errorCode || 'STATE_ENCRYPT_FAILED');
  }
  // Windows compatibility fallback.  A configured secure key never reaches
  // this branch because session-store.py fails closed instead of downgrading.
  atomicWrite(sessionFile, dpapi('protect', Buffer.from(JSON.stringify({ version: 1, cookies: selected }))).toString('base64'));
}

// The file is base64-wrapped ciphertext; no cookie/token is ever written raw.
function restoreSession() {
  const stored = sessionHelper('read');
  const secureKey = Boolean(process.env.THU_SECURE_KEY_FILE || process.env.OPENTHU_SECURE_KEY_FILE);
  if (stored?.ok && Array.isArray(stored.cookies)) {
    const jar = new auth.CookieJar();
    for (const cookie of stored.cookies) {
      const host = String(cookie.domain || '').replace(/^\./, '');
      const origin = 'https://' + host;
      if (!origins.has(origin) || typeof cookie.name !== 'string' || typeof cookie.value !== 'string') continue;
      if (typeof cookie.expires === 'number' && cookie.expires > 0 && cookie.expires * 1000 <= Date.now()) continue;
      (jar.cookies[origin] ||= []).push({name: cookie.name, value: cookie.value, path: cookie.path || '/'});
    }
    if (Object.keys(jar.cookies).length) return jar;
    if (secureKey) throw failure('STATE_DECRYPT_FAILED');
  }
  if (secureKey && !(stored && stored.ok === false && stored.errorCode === 'NO_SESSION')) {
    throw failure(stored?.errorCode || 'SECURE_KEY_UNAVAILABLE');
  }
  try {
    const encoded = fs.readFileSync(sessionFile, 'utf8');
    const jar = new auth.CookieJar();
    const record = JSON.parse(dpapi('unprotect', Buffer.from(encoded, 'base64')).toString('utf8'));
    if (record.version !== 1) return jar;
    for (const [origin, cookies] of Object.entries(record.cookies || {})) {
      if (origins.has(origin) && Array.isArray(cookies)) jar.cookies[origin] = cookies.filter(c =>
        typeof c.name === 'string' && typeof c.value === 'string' && typeof c.path === 'string' && c.path.startsWith('/'));
    }
    return jar;
  } catch { return new auth.CookieJar(); }
}

function seedTunnel(jar) {
  if (jar.headerFor(auth.TUNNEL_COOKIE_SCOPE)) return;
  const token = credentials.readToken();
  for (const pair of (token?.cookie || '').split(';').filter(Boolean)) {
    jar.absorb('https://webvpn.tsinghua.edu.cn/', pair.trim() + '; Path=/');
  }
}

const get = (url, jar) => auth.requestWithRedirects({ url, timeout: 20000 }, jar);
function csrf(body) {
  return /[?&]_csrf=([^"'&\s<>]+)/i.exec(body)?.[1]
    || /name=["']_csrf["'][^>]*value=["']([^"']+)/i.exec(body)?.[1] || '';
}
function learnHomeValid(response) {
  const u = new URL(response.finalUrl);
  return response.statusCode === 200 && u.hostname === 'learn.tsinghua.edu.cn'
    && u.pathname.startsWith('/f/wlxt/index/course/student/')
    && /退出登录|注销/.test(response.body) && !/type=["']password["']/i.test(response.body);
}
const noTwoFactor = () => { throw failure('TWO_FACTOR_REQUIRED'); };

async function learnSession(client, account, force) {
  const homeUrl = 'https://learn.tsinghua.edu.cn/f/wlxt/index/course/student/';
  let home = force ? null : await get(homeUrl, client.jar);
  if (!home || !learnHomeValid(home)) {
    const entry = await get('https://learn.tsinghua.edu.cn/f/login', client.jar);
    const found = [...entry.body.matchAll(/(?:href|action)\s*=\s*["']([^"']+)["']/gi)]
      .map(m => { try { return new URL(m[1].replace(/&amp;/g, '&'), entry.finalUrl); } catch { return null; } })
      .find(u => u?.origin === 'https://id.tsinghua.edu.cn' && /^\/do\/off\/ui\/auth\/login\/form\/[a-f0-9]+\/0$/.test(u.pathname));
    if (!found) throw failure('LEARN_SSO_ENTRY_CHANGED');
    const form = await get(found.href, client.jar);
    let target = form.finalUrl;
    if (/sm2publicKey/.test(form.body)) {
      const result = await client.authenticateIdentity(found.href,
        'https://id.tsinghua.edu.cn/do/off/ui/auth/login/check',
        account.username, account.password, account.fingerPrint, noTwoFactor, form);
      target = result.redirectUrl;
    } else if (!learnHomeValid(form)) {
      const anchor = /<a[^>]+href\s*=\s*["']([^"']+)["']/i.exec(form.body);
      if (anchor && /ticket=|登录成功|正在重定向/.test(form.body)) target = new URL(anchor[1].replace(/&amp;/g, '&'), form.finalUrl).href;
    }
    if (new URL(target).protocol === 'http:') target = client.toLbRedirectUrl(target);
    await get(target, client.jar);
    home = await get(homeUrl, client.jar);
  }
  if (!learnHomeValid(home)) throw failure('LEARN_SESSION_NOT_VERIFIED');
  const token = csrf(home.body);
  if (!token) throw failure('LEARN_CSRF_MISSING');
  return token;
}

async function readCourses(jar, token) {
  const requestJson = async endpoint => {
    const url = new URL(endpoint, 'https://learn.tsinghua.edu.cn');
    url.searchParams.set('_csrf', token);
    const response = await get(url.href, jar);
    if (response.statusCode !== 200 || new URL(response.finalUrl).hostname !== 'learn.tsinghua.edu.cn') throw failure('LEARN_API_AUTH_REQUIRED');
    try { return JSON.parse(response.body); } catch { throw failure('LEARN_API_NOT_JSON'); }
  };
  const semester = (await requestJson('/b/kc/zhjw_v_code_xnxq/getCurrentAndNextSemester'))?.result?.xnxq;
  if (typeof semester !== 'string' || !/^[\w-]+$/.test(semester)) throw failure('LEARN_SEMESTER_CHANGED');
  const payload = await requestJson(`/b/wlxt/kc/v_wlkc_xs_xkb_kcb_extend/student/loadCourseBySemesterId/${semester}/zh/`);
  if (!Array.isArray(payload.resultList)) throw failure('LEARN_COURSES_CHANGED');
  return { semester, count: payload.resultList.length,
    names: payload.resultList.map(c => String(c.kcm || c.zywkcm || c.ywkcm || '')) };
}

async function checkModel() {
  const config = upstreamRequire('./config');
  const { createUpstreamClient } = upstreamRequire('./core/upstream-client');
  const t = credentials.readToken();
  if (!t?.token || t.expiresAt <= Date.now()) throw failure('MADMODEL_TOKEN_EXPIRED');
  const upstream = createUpstreamClient({ ...config,
    upstream: auth.MADMODEL_VPN_PREFIX + '/v1/chat/completions', tunnelMode: true,
    upstreamHeaderTimeout: 60000, streamIdleTimeout: 60000, streamTotalTimeout: 90000 });
  let content = '';
  const result = await upstream.request({ token: t.token, cookie: t.cookie,
    signal: AbortSignal.timeout(90000),
    payload: { model: config.model, messages: [{ role: 'user', content: '只回复四个字：连接成功' }],
      stream: true, max_tokens: 64, chat_template_kwargs: { thinking: false } },
    onChunk: chunk => { content += chunk?.choices?.[0]?.delta?.content || ''; },
  });
  if (result.type === 'completion') content = result.body?.choices?.[0]?.message?.content || '';
  if (!['completion', 'stream'].includes(result.type) || !content.trim()) {
    const kind = result.type.toUpperCase().replace(/-/g, '_');
    throw failure('MADMODEL_' + kind);
  }
  return { status: 'GENERATION_VERIFIED', model: config.model, reply: content.trim().slice(0, 80) };
}

function importLearnSession(jar, token) {
  const cookies = (jar.cookies['https://learn.tsinghua.edu.cn'] || []).map(c => ({
    name: c.name, value: c.value, domain: 'learn.tsinghua.edu.cn', path: c.path, secure: true,
  }));
  if (!cookies.length) throw failure('LEARN_COOKIE_MISSING');
  try {
    return JSON.parse(execFileSync(process.env.THU_PYTHON || 'python', [path.join(__dirname, 'import-session.py')], {
      input: JSON.stringify({ cookies, csrf: token }), encoding: 'utf8', windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], timeout: 45000,
    }));
  } catch { throw failure('LEARN_PLUGIN_IMPORT_FAILED'); }
}

async function main() {
  const command = process.argv[2] || 'verify';
  if (!['verify', 'courses', 'refresh'].includes(command)) throw failure('UNKNOWN_COMMAND');
  const held = await locks.acquirePidLock(paths.AUTH_LOCK);
  if (!held.ok) throw failure('AUTH_BUSY');
  const originalFetch = globalThis.fetch;
  let passwordSubmissions = 0;
  globalThis.fetch = (input, options) => {
    const u = new URL(String(input));
    if (u.origin === 'https://id.tsinghua.edu.cn' && u.pathname === '/do/off/ui/auth/login/check'
      && options?.method === 'POST') passwordSubmissions++;
    return originalFetch(input, options);
  };
  try {
    const account = credentials.readAccount();
    if (!account) throw failure('DEPLOYED_CREDENTIALS_UNAVAILABLE');
    const jar = restoreSession();
    seedTunnel(jar);
    const client = new auth.MadmodelAuthClient(jar);
    const result = { checkedAt: new Date().toISOString(), mode: 'READ_ONLY', sites: {} };
    emit({ stage: 'verifying_info' });
    await client.ensureWebVpnSession(account.username, account.password, account.fingerPrint, noTwoFactor);
    const info = await get(auth.INFO_PREFIX + '/f/info/gxfw_fg/common/index', jar);
    if (info.statusCode !== 200 || !info.body.includes('我的工作台') || !info.body.includes('退出')
      || !info.body.includes('个人信息') || !new URL(info.finalUrl).pathname.endsWith('/f/info/gxfw_fg/common/index'))
      throw failure('INFO_PERSONAL_PAGE_NOT_VERIFIED');
    result.sites.info = { status: 'PERSONAL_WORKBENCH_VERIFIED', title: '我的工作台 - 清华大学信息门户' };
    emit({ stage: 'verifying_learn' });
    const learnCsrf = await learnSession(client, account, command === 'refresh');
    const courses = await readCourses(jar, learnCsrf);
    result.sites.learn = { status: 'COURSES_API_VERIFIED', semester: courses.semester,
      courseCount: courses.count, sampleCourses: courses.names.slice(0, 3) };
    // Save only after actual personalized/business reads succeeded.
    saveSession(jar);
    if (process.argv.includes('--import-plugin')) result.plugin = importLearnSession(jar, learnCsrf);
    const old = credentials.readToken();
    if (command === 'refresh' || !old || old.expiresAt <= Date.now()) {
      const fresh = await client.getMadModelToken({ ...account, twoFactorHandler: noTwoFactor });
      credentials.writeToken(fresh.token, fresh.expiresAt, fresh.cookie);
    }
    result.sites.madmodel = process.argv.includes('--model-check') ? await checkModel()
      : { status: 'CREDENTIALS_PRESENT_NOT_INFERENCE_TESTED' };
    result.identityPasswordSubmissions = passwordSubmissions;
    atomicWrite(summaryFile, JSON.stringify(result, null, 2));
    if (process.argv.includes('--model-check')) atomicWrite(path.join(__dirname, 'last-full-verification.json'), JSON.stringify(result, null, 2));
    if (command === 'courses') result.courses = courses.names;
    emit(result);
  } finally { locks.releasePidLock(paths.AUTH_LOCK); globalThis.fetch = originalFetch; }
}

if (require.main === module) main().catch(error => {
  const code = typeof error.code === 'string' && /^[A-Z_]+$/.test(error.code) ? error.code : 'ACCESS_FAILED';
  emit({ status: 'FAILED', errorCode: code,
    ...(code === 'TWO_FACTOR_REQUIRED' ? { action: '在原 madmodel-proxy 中完成一次官方二次认证，再重试。' } : {}) });
  process.exitCode = 1;
});

module.exports = { csrf, learnHomeValid, saveSession, restoreSession, dpapi, learnSession, importLearnSession, checkModel };
