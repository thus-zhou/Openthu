"""One owned Node subprocess. Sent requests are never replayed."""
from __future__ import annotations
import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid


class CampusError(RuntimeError):
    def __init__(self, code: str, retry_after_ms: int | None = None, model_evidence=None):
        self.code = code
        self.retry_after_ms = retry_after_ms
        self.model_evidence = model_evidence
        super().__init__(code)


_control = ContextVar('gateway_control', default=(None, None))


@contextmanager
def request_control(cancel_event=None, progress=None):
    token = _control.set((cancel_event, progress))
    try:
        yield
    finally:
        _control.reset(token)


@dataclass
class Pending:
    process: subprocess.Popen
    event: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    status: str = 'QUEUED'
    progress: object = None


class GatewayClient:
    def __init__(self, argv=None, env=None):
        self.argv = argv or [os.environ.get('THU_NODE', 'node'), str(Path(__file__).with_name('gateway.js')), '--stdio']
        self.env = env
        self.lock = threading.RLock()
        self.process = None
        self.pending = {}
        self.starts = 0
        self.closed = False

    def _start(self):
        if self.closed:
            raise CampusError('GATEWAY_EXITED')
        if self.process is None or self.process.poll() is not None:
            try:
                proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1, env=self.env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except OSError:
                raise CampusError('CAMPUS_GATEWAY_UNAVAILABLE') from None
            self.process = proc
            self.starts += 1
            threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        return self.process

    def _read(self, proc):
        try:
            for line in proc.stdout:
                response = json.loads(line)
                with self.lock:
                    item = self.pending.get(response.get('request_id'))
                    if item is None or item.process is not proc:
                        continue
                    if response.get('type') == 'progress':
                        item.status = response.get('status', item.status)
                        if item.progress:
                            try:
                                item.progress(item.status)
                            except Exception:
                                pass
                    else:
                        item.response = response
                        item.event.set()
        except (ValueError, OSError, TypeError, AttributeError):
            pass
        finally:
            self._retire(proc)

    def _retire(self, proc):
        with self.lock:
            if self.process is proc:
                self.process = None
            for item in self.pending.values():
                if item.process is proc and not item.event.is_set():
                    item.response = {'ok': False, 'error': {'code': 'GATEWAY_EXITED'}}
                    item.event.set()
            if proc.poll() is None:
                proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _send(self, proc, message):
        try:
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            proc.stdin.flush()
        except (OSError, ValueError):
            self._retire(proc)
            raise CampusError('GATEWAY_EXITED') from None

    def call(self, command, *, timeout=240, cancel_event=None, progress=None, **arguments):
        if cancel_event and cancel_event.is_set():
            raise CampusError('CANCELLED')
        request_id = uuid.uuid4().hex
        with self.lock:
            proc = self._start()
            item = Pending(proc, progress=progress)
            self.pending[request_id] = item
            try:
                self._send(proc, {'type': 'request', 'request_id': request_id,
                                 'timeout_ms': int(timeout * 1000), 'command': {'command': command, **arguments}})
            except Exception:
                self.pending.pop(request_id, None)
                raise
        deadline = time.monotonic() + timeout
        try:
            while not item.event.wait(0.05):
                cancelled = cancel_event is not None and cancel_event.is_set()
                if cancelled or time.monotonic() >= deadline:
                    code = 'CANCELLED' if cancelled else 'QUEUE_TIMEOUT' if item.status == 'QUEUED' else 'CAMPUS_GATEWAY_TIMEOUT'
                    with self.lock:
                        try:
                            self._send(proc, {'type': 'cancel', 'request_id': request_id})
                        except CampusError:
                            pass
                    if not item.event.wait(2):
                        self._retire(proc)
                    error=(item.response or {}).get('error',{})
                    raise CampusError(code,model_evidence=error.get('model_evidence'))
            response = item.response or {}
            if not response.get('ok'):
                error = response.get('error', {})
                raise CampusError(error.get('code', 'CAMPUS_ACCESS_FAILED'), error.get('retry_after_ms'),error.get('model_evidence'))
            return response['result']
        except (KeyboardInterrupt, SystemExit):
            self._retire(proc)
            raise
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        with self.lock:
            self.closed = True
            proc = self.process
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            self._retire(proc)


_client = GatewayClient()
atexit.register(_client.close)


def gateway(command: str, **arguments):
    cancel, progress = _control.get()
    return _client.call(command, cancel_event=cancel, progress=progress, **arguments)


def close_gateway():
    _client.close()
