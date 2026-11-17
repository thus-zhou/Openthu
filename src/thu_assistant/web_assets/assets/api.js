'use strict';
/* Openthu local dashboard: same-origin read-only API client (module 7.1).
 *
 * The dashboard may only perform GET requests against the loopback API.  This
 * wrapper enforces that at the single call site: it never accepts a method
 * argument, never accepts an absolute URL, and never stores a cookie or a
 * token.  Session cookies are HttpOnly and handled by the browser.
 *
 * A timeout produces a local display error instead of hanging the page; the
 * server's own 30 s deadline stays the authority for facade reads.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.OpenthuApi = factory();
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const DEFAULT_TIMEOUT_MS = 35000;
  const API_PREFIX = '/api/v1/';

  function buildUrl(base, path, params) {
    if (typeof path !== 'string' || path.charAt(0) !== '/' || path.indexOf('://') >= 0) {
      throw new Error('INVALID_LOCAL_PATH');
    }
    if (path.indexOf(API_PREFIX) !== 0) throw new Error('INVALID_LOCAL_PATH');
    const query = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      const value = params[key];
      if (value === null || value === undefined || value === '') return;
      if (Array.isArray(value)) {
        value.forEach(function (item) { query.append(key, String(item)); });
      } else {
        query.append(key, String(value));
      }
    });
    const text = query.toString();
    return base + path + (text ? '?' + text : '');
  }

  function createApi(options) {
    const base = (options && options.base) || '';
    const fetchImpl = (options && options.fetchImpl) || (typeof fetch === 'function' ? fetch : null);
    const timeoutMs = Number.isInteger(options && options.timeoutMs)
      ? options.timeoutMs : DEFAULT_TIMEOUT_MS;
    if (typeof fetchImpl !== 'function') throw new Error('FETCH_UNAVAILABLE');

    async function get(path, params, settings) {
      const controller = new AbortController();
      const external = settings && settings.signal;
      let cancelled = false;
      const abort = function () { cancelled = true; controller.abort(); };
      if (external) {
        if (external.aborted) abort();
        else external.addEventListener('abort', abort, {once: true});
      }
      const timer = setTimeout(function () { controller.abort(); }, timeoutMs);
      try {
        const response = await fetchImpl(buildUrl(base, path, params), {
          method: 'GET',
          mode: 'same-origin',
          credentials: 'same-origin',
          cache: 'no-store',
          redirect: 'error',
          referrerPolicy: 'no-referrer',
          headers: {'Accept': 'application/json'},
          signal: controller.signal
        });
        let body = null;
        let parsed = false;
        try {
          const raw = await response.text();
          if (raw) {
            body = JSON.parse(raw);
            parsed = true;
          }
        } catch (error) {
          parsed = false;
        }
        if (!parsed || !body || typeof body !== 'object') {
          return {ok: false, aborted: false, networkError: 'LOCAL_API_INVALID_JSON',
                  httpStatus: response.status, body: null, requestId: ''};
        }
        const requestId = typeof body.request_id === 'string' ? body.request_id : '';
        return {
          ok: response.ok,
          aborted: false,
          networkError: '',
          httpStatus: response.status,
          body: body,
          requestId: requestId
        };
      } catch (error) {
        if (cancelled || (error && error.name === 'AbortError')) {
          return {ok: false, aborted: true, networkError: '', httpStatus: 0,
                  body: null, requestId: ''};
        }
        return {ok: false, aborted: false, networkError: 'LOCAL_API_NETWORK_UNAVAILABLE',
                httpStatus: 0, body: null, requestId: ''};
      } finally {
        clearTimeout(timer);
        if (external) external.removeEventListener('abort', abort);
      }
    }

    return {get: get, base: base, timeoutMs: timeoutMs};
  }

  return {createApi: createApi, buildUrl: buildUrl, DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
          API_PREFIX: API_PREFIX};
}));
