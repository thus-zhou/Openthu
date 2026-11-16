'use strict';
const crypto = require('node:crypto');
const KEY = Buffer.from('wrdvpnisthebest!');
const SECRET_KEY = /password|passwd|i_pass|cookie|authorization|csrf|xsrf|ticket|sessionid|access.?token|refresh.?token|oauth.?token|nonce|signature|vericode|samlrequest|samlresponse|relaystate|(?:^|[_-])(?:token|secret|otp|sign|api.?key)(?:$|[_-])/i;
const MUTATION = /^(?:logout|logoff|delete\w*|del\w*|remove\w*|save\w*|insert\w*|update\w*|submit\w*|cancel\w*|confirm\w*|pay\w*|renew\w*|reserve|book|apply|addFavorite|setProfile)$/i;
const fail = code => Object.assign(new Error(code), { code });

function validateUrl(input) {
  let u;
  try { u = new URL(input); } catch { throw fail('INVALID_URL'); }
  if (u.protocol !== 'https:' || u.port && u.port !== '443' || u.username || u.password
    || !(u.hostname === 'tsinghua.edu.cn' || u.hostname.endsWith('.tsinghua.edu.cn')))
    throw fail('NON_TSINGHUA_HTTPS_URL');
  if (u.hostname === 'webvpn.tsinghua.edu.cn' && /^\/https\//.test(u.pathname)) {
    const segment = u.pathname.split('/')[2];
    if (!/^[a-f0-9]+$/i.test(segment) || !segment.startsWith(KEY.toString('hex')) || segment.length % 2)
      throw fail('INVALID_WEBVPN_TARGET');
    try {
      const d = crypto.createDecipheriv('aes-128-cfb', KEY, KEY);
      const host = d.update(Buffer.from(segment.slice(32), 'hex')).toString('utf8');
      validateUrl('https://' + host + '/');
    } catch { throw fail('NON_TSINGHUA_WEBVPN_TARGET'); }
  }
  return u;
}

function assertReadUrl(input) {
  const u = validateUrl(input);
  let path;
  try { path = decodeURIComponent(u.pathname); } catch { throw fail('INVALID_URL_ENCODING'); }
  if (path.split(/[\/;.!]/).some(p => MUTATION.test(p))) throw fail('WRITE_URL_REQUIRES_ACTION_PLAN');
  for (const [key, value] of u.searchParams) {
    if (/^(?:action|method|operation|op|do)$/i.test(key) && MUTATION.test(value))
      throw fail('WRITE_URL_REQUIRES_ACTION_PLAN');
  }
  return u;
}

function toTunnel(input) {
  const u = validateUrl(input);
  if (u.hostname === 'webvpn.tsinghua.edu.cn') return u.href;
  const c = crypto.createCipheriv('aes-128-cfb', KEY, KEY);
  const hash = KEY.toString('hex') + c.update(u.hostname).toString('hex');
  return 'https://webvpn.tsinghua.edu.cn/https/' + hash + u.pathname + u.search;
}

function safeUrl(input) {
  try {
    const u = new URL(input);
    u.username = ''; u.password = '';
    u.pathname = u.pathname.replace(/;[^/]*session[^/]*(?=\/|$)/gi, '').replace(/;jsessionid=[^/]*/gi, '');
    for (const key of [...u.searchParams.keys()]) if (SECRET_KEY.test(key)) u.searchParams.delete(key);
    // Fragments often contain OAuth tokens or SPA state; do not publish them.
    u.hash = '';
    return u.href;
  } catch { return ''; }
}

function redact(value, secrets = []) {
  let text = String(value ?? '');
  text = text.replace(/(?:Bearer\s+)[A-Za-z0-9._~+\/-]+/gi, 'Bearer [REDACTED]')
    .replace(/([?&](?:amp;)?(?:ticket|_csrf|oauth_token|access_token|refresh_token|token|sessionid)=)[^\s"'<>&#]*/gi, '$1[REDACTED]')
    .replace(/;jsessionid=[^/\s"'<>?]*/gi, ';[REDACTED]');
  for (const secret of secrets.filter(s => typeof s === 'string' && s.length >= 6).sort((a,b) => b.length-a.length))
    text = text.split(secret).join('[REDACTED]');
  return text;
}

function sanitizeJson(value, secrets = [], depth = 0) {
  if (depth > 100) throw fail('JSON_DEPTH_LIMIT');
  if (typeof value === 'string') return redact(value, secrets);
  if (Array.isArray(value)) return value.map(v => sanitizeJson(v, secrets, depth+1));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key,v]) =>
    [key, SECRET_KEY.test(key) ? '[REDACTED]' : sanitizeJson(v, secrets, depth+1)]));
  return value;
}

module.exports = { validateUrl, assertReadUrl, toTunnel, safeUrl, redact, sanitizeJson, SECRET_KEY };
