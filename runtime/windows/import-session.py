"""Accept a verified Learn session via stdin and save it with existing DPAPI code.

No password, cookie, CSRF value or student identifier is printed or saved raw.
"""
import json
import os
from pathlib import Path
import sys
import uuid
from datetime import datetime, timezone

provider = Path(os.environ.get('THU_LEARN_PROVIDER', str(Path(__file__).resolve().parents[2] / 'src')))
sys.path.insert(0, str(provider))

def main():
    from learn_assistant.auth import SecureSessionStore, browser_session_from_cookies
    from learn_assistant.providers.web import WebProvider
    raw = json.load(sys.stdin)
    cookies = raw.get('cookies', [])
    if not cookies or any(c.get('domain') not in ('learn.tsinghua.edu.cn','.learn.tsinghua.edu.cn','.tsinghua.edu.cn') for c in cookies):
        raise ValueError('scope')
    session = browser_session_from_cookies(cookies, csrf_token=raw['csrf'], source='authorized-official-sso-protocol')
    live = WebProvider(browser_session=session, load_persisted=False)
    if not live.get_login_status().authenticated:
        raise ValueError('authentication')
    courses = live.list_courses()
    store = SecureSessionStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    temp = store.path.with_name(store.path.name + '.' + uuid.uuid4().hex + '.thu-access.tmp')
    try:
        if not SecureSessionStore(temp).save(session):
            raise ValueError('dpapi')
        # Preserve the prior encrypted session once; never export plaintext.
        if store.path.exists():
            backup = store.path.with_name(store.path.name + '.before-thu-access')
            if not backup.exists():
                backup.write_bytes(store.path.read_bytes())
        os.replace(temp, store.path)
    finally:
        temp.unlink(missing_ok=True)
    print(json.dumps({'persisted': True, 'protection': 'Windows DPAPI',
                      'freshProviderAuthenticated': True, 'courseCount': len(courses)}, ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"persisted":false,"errorCode":"SESSION_IMPORT_FAILED"}')
        raise SystemExit(1)
