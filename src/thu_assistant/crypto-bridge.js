'use strict';
// Bounded JSON-lines client for the private Python encryption service.
//
// The interpreter, module and environment are fixed by this process.  A
// request may select only a frozen action and its non-secret scope; it can
// never select a program, file path, key location or data directory.  Each
// request has an id and a byte limit, and a timeout or cancellation kills the
// child instead of replaying the request.
const {spawn} = require('node:child_process');
const crypto = require('node:crypto');

const MAGIC = Buffer.from('THU-SECURE-1\0', 'ascii');
const ACTIONS = new Set(['seal', 'unseal', 'inspect', 'health']);
const DEFAULT_TIMEOUT_MS = 15000;
const MAX_REQUEST_BYTES = 2 * 1024 * 1024;
const MAX_RESPONSE_BYTES = MAX_REQUEST_BYTES + 64 * 1024;
const FORBIDDEN_FIELDS = new Set([
  'argv', 'backend_path', 'data_dir', 'env', 'executable', 'key_file',
  'key_path', 'module', 'path', 'program', 'python',
]);
const ENV_ALLOW = [
  'PATH', 'PATHEXT', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC',
  'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
  'USERNAME', 'USERDOMAIN', 'PYTHONPATH', 'PYTHONUTF8', 'PYTHONNOUSERSITE',
  'PSModulePath',
];

const failure = (code, detail) => Object.assign(new Error(code), {code, detail});

function resolveCryptoConfig(env = process.env, stateDir = '') {
  const keyFile = env.THU_SECURE_KEY_FILE || env.OPENTHU_SECURE_KEY_FILE || '';
  const dataDir = env.THU_SECURE_DATA_DIR || env.OPENTHU_SECURE_DATA_DIR || stateDir;
  const timeoutMs = Math.min(60000, Math.max(1000, Number(env.THU_SECURE_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS));
  return {
    enabled: Boolean(keyFile),
    keyFile,
    dataDir,
    backend: env.THU_SECURE_BACKEND || env.OPENTHU_SECURE_BACKEND || undefined,
    python: env.THU_PYTHON || env.OPENTHU_PYTHON || 'python',
    timeoutMs,
  };
}

function childEnvironment(config) {
  const env = {};
  for (const name of ENV_ALLOW) {
    if (typeof process.env[name] === 'string') env[name] = process.env[name];
  }
  env.THU_SECURE_KEY_FILE = config.keyFile;
  env.THU_SECURE_DATA_DIR = config.dataDir;
  env.PYTHONUTF8 = '1';
  env.PYTHONNOUSERSITE = '1';
  if (config.backend) env.THU_SECURE_BACKEND = config.backend;
  return env;
}

function looksSecureEnvelope(value) {
  return Buffer.isBuffer(value) && value.length >= MAGIC.length && value.subarray(0, MAGIC.length).equals(MAGIC);
}

class SecureCryptoClient {
  constructor(config) {
    if (!config || !config.enabled) throw failure('SECURE_KEY_NOT_CONFIGURED');
    this.config = config;
    this.child = null;
    this.buffer = '';
    this.pending = null;
    this.closed = false;
    this.queue = Promise.resolve();
  }

  _start() {
    if (this.closed) throw failure('CRYPTO_SERVICE_CLOSED');
    if (this.child && this.child.exitCode === null && !this.child.killed) return this.child;
    let child;
    const argv = Array.isArray(this.config.argv)
      ? this.config.argv
      : [this.config.python, '-m', 'thu_assistant.crypto_service'];
    try {
      child = spawn(argv[0], argv.slice(1), {
        env: childEnvironment(this.config), windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch {
      throw failure('CRYPTO_SERVICE_UNAVAILABLE');
    }
    this.child = child;
    this.buffer = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', part => this._data(child, part));
    child.stdin.on('error', () => this._exit(child, 'CRYPTO_SERVICE_EXITED'));
    // Private service diagnostics are never forwarded or logged.  Keep the
    // stream drained so a full pipe cannot stall the child.
    child.stderr.on('data', () => {});
    child.on('error', () => this._exit(child, 'CRYPTO_SERVICE_UNAVAILABLE'));
    child.on('exit', () => this._exit(child, 'CRYPTO_SERVICE_EXITED'));
    return child;
  }

  _data(child, part) {
    if (child !== this.child) return;
    this.buffer += part;
    if (Buffer.byteLength(this.buffer) > MAX_RESPONSE_BYTES) {
      this._failCurrent('CRYPTO_RESPONSE_TOO_LARGE');
      this._kill(child);
      return;
    }
    for (;;) {
      const end = this.buffer.indexOf('\n');
      if (end < 0) break;
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      if (!line) continue;
      let response;
      try { response = JSON.parse(line); } catch { this._failCurrent('CRYPTO_PROTOCOL_ERROR'); this._kill(child); return; }
      const pending = this.pending;
      if (!pending || response.id !== pending.id) continue;
      clearTimeout(pending.timer);
      this.pending = null;
      pending.resolve(response);
    }
  }

  _exit(child, code) {
    if (child !== this.child) return;
    this.child = null;
    this.buffer = '';
    if (this.pending) {
      const pending = this.pending;
      this.pending = null;
      clearTimeout(pending.timer);
      pending.reject(failure(code));
    }
  }

  _kill(child) {
    if (!child) return;
    try { child.kill(); } catch { /* already gone */ }
  }

  _failCurrent(code) {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    pending.reject(failure(code));
  }

  _request(action, fields, signal) {
    if (!ACTIONS.has(action)) return Promise.reject(failure('CRYPTO_ACTION_NOT_ALLOWED'));
    for (const key of Object.keys(fields || {})) {
      if (FORBIDDEN_FIELDS.has(key)) return Promise.reject(failure('CRYPTO_PATH_NOT_ACCEPTED'));
    }
    if (fields && fields.action !== undefined) return Promise.reject(failure('CRYPTO_ACTION_OVERRIDE'));
    if (signal?.aborted) return Promise.reject(failure('CRYPTO_CANCELLED'));
    const id = crypto.randomUUID();
    const request = {id, action, ...fields};
    let line;
    try { line = JSON.stringify(request) + '\n'; } catch { return Promise.reject(failure('CRYPTO_INVALID_REQUEST')); }
    if (Buffer.byteLength(line) > MAX_REQUEST_BYTES) return Promise.reject(failure('CRYPTO_REQUEST_TOO_LARGE'));
    const run = () => new Promise((resolve, reject) => {
      if (this.closed) { reject(failure('CRYPTO_SERVICE_CLOSED')); return; }
      let child;
      try { child = this._start(); } catch (error) { reject(error); return; }
      const timer = setTimeout(() => {
        this._failCurrent('CRYPTO_TIMEOUT');
        this._kill(child);
      }, this.config.timeoutMs || DEFAULT_TIMEOUT_MS);
      const onAbort = () => {
        this._failCurrent('CRYPTO_CANCELLED');
        this._kill(child);
      };
      signal?.addEventListener('abort', onAbort, {once: true});
      this.pending = {
        id, timer,
        resolve: value => { signal?.removeEventListener('abort', onAbort); resolve(value); },
        reject: error => { signal?.removeEventListener('abort', onAbort); reject(error); },
      };
      try {
        child.stdin.write(line);
      } catch {
        this._failCurrent('CRYPTO_SERVICE_EXITED');
        this._kill(child);
      }
    });
    const result = this.queue.then(run, run);
    this.queue = result.catch(() => {});
    return result;
  }

  async _call(action, fields, options = {}) {
    const response = await this._request(action, fields || {}, options.signal);
    if (!response.ok) {
      const code = typeof response.error?.code === 'string' && /^[A-Z_]+$/.test(response.error.code)
        ? response.error.code : 'CRYPTO_SERVICE_FAILED';
      const error = failure(code);
      error.category = response.error?.category;
      throw error;
    }
    return response;
  }

  async health() {
    return (await this._call('health')).health;
  }

  async seal(plaintext, context = {}) {
    if (!Buffer.isBuffer(plaintext)) plaintext = Buffer.from(String(plaintext), 'utf8');
    const response = await this._call('seal', {
      plaintext_b64: plaintext.toString('base64'),
      account: context.account,
      purpose: context.purpose,
      account_domain: context.accountDomain || context.account_domain,
      service_domain: context.serviceDomain || context.service_domain,
      revision: context.revision,
      record_id: context.recordId || context.record_id,
    }, context);
    return {
      envelope: Buffer.from(response.envelope_b64, 'base64'),
      header: response.header, context: response.context,
    };
  }

  async unseal(envelope, context = {}) {
    if (!Buffer.isBuffer(envelope)) envelope = Buffer.from(envelope);
    const response = await this._call('unseal', {
      envelope_b64: envelope.toString('base64'),
      account: context.account,
      purpose: context.purpose,
      account_domain: context.accountDomain || context.account_domain,
      service_domain: context.serviceDomain || context.service_domain,
      record_id: context.recordId || context.record_id,
      expected_revision: context.expectedRevision ?? context.expected_revision,
    }, context);
    return {
      plaintext: Buffer.from(response.plaintext_b64, 'base64'),
      header: response.header, context: response.context,
    };
  }

  async inspect(envelope) {
    if (!Buffer.isBuffer(envelope)) envelope = Buffer.from(envelope);
    const response = await this._call('inspect', {envelope_b64: envelope.toString('base64')});
    return {header: response.header, context: response.context};
  }

  close() {
    this.closed = true;
    const child = this.child;
    this.child = null;
    this._failCurrent('CRYPTO_SERVICE_CLOSED');
    this._kill(child);
  }
}

function createSecureCrypto(config) {
  return new SecureCryptoClient(config);
}

module.exports = {
  ACTIONS, DEFAULT_TIMEOUT_MS, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
  SecureCryptoClient, childEnvironment, createSecureCrypto, looksSecureEnvelope,
  resolveCryptoConfig,
};
