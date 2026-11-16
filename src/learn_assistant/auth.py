"""可见浏览器登录与安全会话存储。

登录打开隔离的可见 Playwright 浏览器，等待用户或其授权智能体通过界面完成清华认证。
本模块不接收或保存密码，也不复用用户现有 Chrome profile；智能体可另用已授权的
浏览器/桌面控制工具操作可见登录窗口。

会话持久化仅在 Windows DPAPI 可用时启用。非 Windows 或 DPAPI 调用失败时，材料只留在
当前进程内，并返回 ``persisted=False``；绝不退回到明文 JSON/Cookie 文件。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .errors import AuthenticationRequired, ProviderNotConfigured, ValidationError
from .models import BrowserCookie, BrowserSession
from thu_assistant.secure_store.envelope import StorageContext, parse_envelope
from thu_assistant.secure_store.errors import SecureStoreError
from thu_assistant.secure_store.providers import load_provider

SECURE_SESSION_NAME = "session.secure"
SECURE_MAGIC = b"THU-SECURE-1\0"


def default_session_dir() -> Path:
    """Return the one Learn session directory for this platform.

    Windows keeps the historical ``%LOCALAPPDATA%\\LearnAssistant`` location.
    Other platforms use the unified state directory; a missing key never
    changes the path or creates a plaintext fallback.
    """
    explicit = os.environ.get("OPENTHU_LEARN_SESSION_DIR") or os.environ.get("THU_LEARN_SESSION_DIR")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base or Path.home()) / "LearnAssistant"
    state = (
        os.environ.get("OPENTHU_STATE_DIR")
        or os.environ.get("THU_ASSISTANT_STATE_DIR")
        or str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "openthu")
    )
    return Path(state).expanduser() / "LearnAssistant"


DEFAULT_LOGIN_URL = "https://id.tsinghua.edu.cn/do/off/ui/auth/login/form/bb5df85216504820be7bba2b0ae1535b/0"
DEFAULT_SESSION_PATH = default_session_dir() / "session.dpapi"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def extract_csrf_token(html: str) -> str:
    """提取网络学堂首页链接或隐藏字段中的 CSRF token。"""

    patterns = (
        r"[?&]_csrf=([^\"'&\s]+)",
        r"name=[\"']_csrf[\"'][^>]*value=[\"']([^\"']+)[\"']",
        r"value=[\"']([^\"']+)[\"'][^>]*name=[\"']_csrf[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _cookie_from_mapping(item: Any) -> BrowserCookie | None:
    if not isinstance(item, dict) or not item.get("name"):
        return None
    value = item.get("value")
    if value is None:
        return None
    expires = item.get("expires")
    try:
        expires = float(expires) if expires not in (None, -1) else None
    except (TypeError, ValueError):
        expires = None
    return BrowserCookie(
        name=str(item["name"]),
        value=str(value),
        domain=str(item.get("domain") or ""),
        path=str(item.get("path") or "/"),
        secure=bool(item.get("secure", True)),
        expires=expires,
    )


def browser_session_from_cookies(
    cookies: Iterable[dict[str, Any]],
    *,
    user_agent: str = "",
    csrf_token: str = "",
    source: str = "browser",
) -> BrowserSession:
    parsed = tuple(cookie for item in cookies if (cookie := _cookie_from_mapping(item)))
    if not parsed:
        raise ValidationError("可见浏览器没有返回任何会话 cookie，登录可能尚未完成。")
    return BrowserSession(
        cookies=parsed,
        user_agent=user_agent,
        csrf_token=csrf_token,
        captured_at=_now_iso(),
        source=source,
    )


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_SECURE_PROVIDERS: dict[tuple[str, str | None], object] = {}
_SECURE_PROVIDER_FAILURE: str | None = None


def _secure_key_path() -> str | None:
    return os.environ.get("OPENTHU_SECURE_KEY_FILE") or os.environ.get("THU_SECURE_KEY_FILE")


def _secure_provider():
    """Load the configured secure provider once per key location.

    The wrapper is deliberately explicit: a configured key never falls back to
    DPAPI or plaintext when the key is unreadable or belongs to another key.
    """
    global _SECURE_PROVIDER_FAILURE
    path = _secure_key_path()
    if not path:
        return None
    backend = os.environ.get("OPENTHU_SECURE_BACKEND") or os.environ.get("THU_SECURE_BACKEND") or None
    key = (path, backend)
    if key in _SECURE_PROVIDERS:
        return _SECURE_PROVIDERS[key]
    try:
        provider = load_provider(path, backend)
    except SecureStoreError as error:
        _SECURE_PROVIDER_FAILURE = error.code
        return None
    except Exception:
        _SECURE_PROVIDER_FAILURE = "SECURE_KEY_UNREADABLE"
        return None
    _SECURE_PROVIDERS[key] = provider
    return provider


def _secure_protect(
    raw: bytes,
    *,
    purpose: str = "SESSION",
    service_domain: str = "LEARN",
    account: str | None = None,
    revision: int = 0,
    record_id: str | None = "learn-session",
) -> bytes | None:
    provider = _secure_provider()
    if provider is None:
        return None
    try:
        account_ref = provider.account_reference(
            account or "",
            purpose=purpose,
            account_domain="CAMPUS",
            service_domain=service_domain,
        )
        context = StorageContext(
            purpose=purpose,
            account_domain="CAMPUS",
            service_domain=service_domain,
            account_ref=account_ref,
            revision=revision,
            record_id=record_id,
        )
        return provider.seal(raw, context)
    except SecureStoreError:
        return None
    except Exception:
        return None


def _secure_unprotect(raw: bytes) -> bytes | None:
    provider = _secure_provider()
    if provider is None or not raw.startswith(SECURE_MAGIC):
        return None
    try:
        return provider.unseal(raw, None).plaintext
    except (SecureStoreError, ValueError, TypeError):
        return None


def _secure_unprotect_session(raw: bytes) -> bytes | None:
    """Read only the Learn session scope, not any envelope under this key."""
    provider = _secure_provider()
    if provider is None or not raw.startswith(SECURE_MAGIC):
        return None
    try:
        envelope = parse_envelope(raw)
        if (
            envelope.context.purpose != "SESSION"
            or envelope.context.account_domain != "CAMPUS"
            or envelope.context.service_domain != "LEARN"
            or envelope.context.record_id != "learn-session"
        ):
            return None
        return provider.unseal(raw, envelope.context).plaintext
    except (SecureStoreError, ValueError, TypeError):
        return None


def _dpapi_protect(raw: bytes) -> bytes | None:
    """Compatibility wrapper: secure store first, Windows DPAPI second.

    ``ActionStore`` and the todo cache bind this function object at import
    time.  Keeping the same name and signature means those captured references
    receive the new backend without editing their modules.
    """
    protected = _secure_protect(raw, purpose="ACTION", service_domain="LOCAL", record_id=None)
    if protected is not None:
        return protected
    if _secure_key_path():
        return None  # A configured secure key must fail closed, not downgrade.
    if os.name != "nt":
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), "LearnAssistant", None, None, None, 0, ctypes.byref(out_blob)):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(raw: bytes) -> bytes | None:
    """Compatibility wrapper that also reads new secure-store envelopes."""
    secure = _secure_unprotect(raw)
    if secure is not None:
        return secure
    if os.name != "nt":
        return None
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = _DATA_BLOB(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        return None
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class SecureSessionStore:
    """Save Learn cookies with the configured secure store, else Windows DPAPI.

    The legacy ``LA-DPAPI-1`` file is read in place and never rewritten.  A new
    secure envelope is written beside it only after encryption succeeds and is
    verified by decrypting the bytes just written.  A configured but unusable
    key fails closed; it never falls back to plaintext or to a different key.
    """

    MAGIC = b"LA-DPAPI-1\0"

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        legacy = Path(path).expanduser() if path else default_session_dir() / "session.dpapi"
        self.legacy_path = legacy
        self.secure_path = (
            legacy.with_name(SECURE_SESSION_NAME)
            if legacy.name == "session.dpapi"
            else legacy.with_name(legacy.name + ".secure")
        )
        self.last_format: str | None = None
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        """The file currently holding the newest readable state.

        ``mcp_stdio`` watches this path to reload a changed provider session.
        """
        return self.secure_path if self.secure_path.is_file() else self.legacy_path

    @property
    def available(self) -> bool:
        return bool(_secure_key_path()) or os.name == "nt"

    @property
    def protection(self) -> str:
        if _secure_key_path():
            return "secure-store"
        return "Windows DPAPI" if os.name == "nt" else "unavailable"

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _payload(session: BrowserSession) -> bytes:
        payload = {
            "version": 1,
            "cookies": [asdict(cookie) for cookie in session.cookies],
            "user_agent": session.user_agent,
            "csrf_token": session.csrf_token,
            "captured_at": session.captured_at,
            "source": session.source,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _parse(raw: bytes) -> BrowserSession | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("cookies"), list):
                return None
            cookies = tuple(
                BrowserCookie(**item)
                for item in payload["cookies"]
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None
            )
            if not cookies:
                return None
            return BrowserSession(
                cookies=cookies,
                user_agent=str(payload.get("user_agent") or ""),
                csrf_token=str(payload.get("csrf_token") or ""),
                captured_at=str(payload.get("captured_at") or ""),
                source=str(payload.get("source") or "dpapi"),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(self, session: BrowserSession) -> bool:
        raw = self._payload(session)
        protected = _secure_protect(
            raw, purpose="SESSION", service_domain="LEARN", record_id="learn-session"
        )
        if protected is not None:
            try:
                if self.secure_path.exists():
                    backup = self.secure_path.with_name(self.secure_path.name + ".previous")
                    if not backup.exists():
                        backup.write_bytes(self.secure_path.read_bytes())
                self._write(self.secure_path, protected)
            except OSError:
                self.last_error = "STATE_WRITE_FAILED"
                return False
            if _secure_unprotect_session(protected) != raw:
                self.last_error = "STATE_ENCRYPT_FAILED"
                return False
            self.last_format = "secure-store"
            self.last_error = None
            return True
        if _secure_key_path():
            self.last_error = _SECURE_PROVIDER_FAILURE or "SECURE_KEY_NOT_CONFIGURED"
            return False
        if os.name != "nt":
            self.last_error = "SECURE_KEY_NOT_CONFIGURED"
            return False
        legacy = _dpapi_protect(raw)
        if legacy is None:
            self.last_error = "STATE_ENCRYPT_FAILED"
            return False
        try:
            if self.legacy_path.exists():
                backup = self.legacy_path.with_name(self.legacy_path.name + ".before-secure")
                if not backup.exists():
                    backup.write_bytes(self.legacy_path.read_bytes())
            self._write(self.legacy_path, self.MAGIC + legacy)
        except OSError:
            self.last_error = "STATE_WRITE_FAILED"
            return False
        self.last_format = "legacy-dpapi"
        self.last_error = None
        return True

    def load(self) -> BrowserSession | None:
        if self.secure_path.is_file():
            if not _secure_key_path():
                self.last_error = "SECURE_KEY_NOT_CONFIGURED"
                return None
            try:
                raw = self.secure_path.read_bytes()
            except OSError:
                self.last_error = "STATE_READ_FAILED"
                return None
            plain = _secure_unprotect_session(raw)
            if plain is None:
                self.last_error = _SECURE_PROVIDER_FAILURE or "STATE_DECRYPT_FAILED"
                return None
            session = self._parse(plain)
            self.last_format = "secure-store" if session is not None else None
            self.last_error = None if session is not None else "STATE_CORRUPT"
            return session
        if not self.legacy_path.is_file():
            self.last_format = None
            self.last_error = None
            return None
        if os.name != "nt":
            # A legacy ciphertext on another platform is not proof that the
            # user logged out.  Report the platform boundary instead.
            self.last_error = "SECURE_LEGACY_UNAVAILABLE"
            return None
        try:
            raw = self.legacy_path.read_bytes()
            if not raw.startswith(self.MAGIC):
                self.last_error = "STATE_CORRUPT"
                return None
            plain = _dpapi_unprotect(raw[len(self.MAGIC) :])
            if plain is None:
                self.last_error = "STATE_DECRYPT_FAILED"
                return None
            session = self._parse(plain)
            self.last_format = "legacy-dpapi" if session is not None else None
            self.last_error = None if session is not None else "STATE_CORRUPT"
            return session
        except (OSError, ValueError, TypeError):
            self.last_error = "STATE_READ_FAILED"
            return None

    def clear(self) -> bool:
        removed = False
        for path in (self.secure_path, self.legacy_path):
            if path.exists():
                path.unlink()
                removed = True
        self.last_format = None
        return removed


def _is_authenticated_learning_page(url: str, html: str) -> bool:
    """Require the actual learning host and a logged-in page marker together."""
    try:
        location = urlsplit(url)
    except ValueError:
        return False
    if location.scheme.casefold() != "https" or location.hostname != "learn.tsinghua.edu.cn":
        return False
    if "login" in location.path.casefold():
        return False
    return any(marker in html.casefold() for marker in ("mycourse", "semestercourselist", "退出登录", "注销"))


def _is_navigation_transient(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in (
        "unable to retrieve content because the page is navigating",
        "execution context was destroyed",
        "cannot find context with specified id",
        "interrupted by another navigation",
        "net::err_aborted",
    ))


class PlaywrightLogin:
    """打开隔离可见浏览器，捕获用户或授权智能体完成认证后的会话。"""

    def __init__(self, *, browser_channel: str | None = "chrome") -> None:
        self.browser_channel = browser_channel

    def capture(self, *, start_url: str = DEFAULT_LOGIN_URL, timeout_seconds: int = 300) -> BrowserSession:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError as exc:  # pragma: no cover - depends on local optional install
            raise ProviderNotConfigured(
                "插件登录需要 Playwright。请先安装并准备 Playwright 浏览器，或通过可用浏览器工具完成网页登录。"
            ) from exc

        if timeout_seconds <= 0:
            raise ValidationError("timeout_seconds 必须为正数。")

        with sync_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {"headless": False}
            if self.browser_channel:
                launch_kwargs["channel"] = self.browser_channel
            try:
                browser = playwright.chromium.launch(**launch_kwargs)
            except Exception:
                # 允许 Playwright 自带 Chromium 已安装但系统 Chrome channel 不可用。
                launch_kwargs.pop("channel", None)
                browser = playwright.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context()
                page = context.new_page()
                print("已打开可见登录浏览器；用户或授权智能体可通过界面完成清华 SSO、密码、验证码和双因素认证。", file=sys.stderr)
                print("程序会等待浏览器回到网络学堂；不会读取或保存你的密码。", file=sys.stderr)
                deadline = time.monotonic() + timeout_seconds
                try:
                    page.goto(start_url, wait_until="domcontentloaded", timeout=min(timeout_seconds * 1000, 30000))
                except PlaywrightTimeoutError:
                    # A slow load can still leave a usable visible login page.
                    # Keep the same bounded user-authentication wait running.
                    pass
                except Exception as exc:
                    if not _is_navigation_transient(exc):
                        raise
                authenticated = False
                while time.monotonic() < deadline:
                    try:
                        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=min(1000, remaining_ms))
                        except PlaywrightTimeoutError:
                            pass
                        current_url = page.url
                        page_html = page.content()
                        if page.url == current_url and _is_authenticated_learning_page(current_url, page_html):
                            candidate_agent = page.evaluate("() => navigator.userAgent")
                            # SSO can navigate again between the success probe
                            # and session capture. Keep this final read inside
                            # the retry loop, and verify the final page as well.
                            candidate_html = page.content()
                            if page.url == current_url and _is_authenticated_learning_page(current_url, candidate_html):
                                candidate_cookies = context.cookies()
                                if page.url == current_url:
                                    cookies = candidate_cookies
                                    html = candidate_html
                                    user_agent = candidate_agent
                                    authenticated = True
                                    break
                    except Exception as exc:
                        if not _is_navigation_transient(exc):
                            raise
                    remaining_ms = int((deadline - time.monotonic()) * 1000)
                    if remaining_ms > 0:
                        page.wait_for_timeout(min(1000, remaining_ms))
                if not authenticated:
                    raise AuthenticationRequired("等待可见浏览器完成网络学堂 SSO/二次认证超时。")
            finally:
                browser.close()
        session = browser_session_from_cookies(
            cookies,
            user_agent=str(user_agent or ""),
            csrf_token=extract_csrf_token(html),
            source="playwright",
        )
        return session
