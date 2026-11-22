"""Compatibility CLI between the Windows access helper and Learn session store.

The helper reads JSON from stdin and returns one JSON object.  It never prints
cookie values on error and never writes a plaintext fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from dataclasses import asdict

provider = Path(os.environ.get('THU_LEARN_PROVIDER', str(Path(__file__).resolve().parents[2] / 'src')))
sys.path.insert(0, str(provider))


def main() -> int:
    from learn_assistant.auth import SecureSessionStore, browser_session_from_cookies

    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    store = SecureSessionStore()
    if mode == 'read':
        session = store.load()
        if session is None:
            print(json.dumps({'ok': False, 'errorCode': store.last_error or 'NO_SESSION'}))
            return 0
        print(json.dumps({'ok': True, 'format': store.last_format,
                          'cookies': [asdict(cookie) for cookie in session.cookies]}, ensure_ascii=False))
        return 0
    if mode == 'write':
        try:
            raw = json.load(sys.stdin)
        except (ValueError, TypeError):
            print(json.dumps({'ok': False, 'errorCode': 'INVALID_INPUT'}))
            return 0
        cookies = raw.get('cookies') if isinstance(raw, dict) else None
        if not isinstance(cookies, list) or not cookies:
            print(json.dumps({'ok': False, 'errorCode': 'INVALID_INPUT'}))
            return 0
        session = browser_session_from_cookies(cookies, source='thu-access')
        saved = store.save(session)
        print(json.dumps({'ok': bool(saved), 'format': store.last_format,
                          'errorCode': None if saved else (store.last_error or 'STATE_ENCRYPT_FAILED')}))
        return 0
    print(json.dumps({'ok': False, 'errorCode': 'UNKNOWN_COMMAND'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
