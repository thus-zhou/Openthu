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
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .errors import AuthenticationRequired, ProviderNotConfigured, ValidationError
from .models import BrowserCookie, BrowserSession

DEFAULT_LOGIN_URL = "https://id.tsinghua.edu.cn/do/off/ui/auth/login/form/bb5df85216504820be7bba2b0ae1535b/0"
DEFAULT_SESSION_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "LearnAssistant" / "session.dpapi"


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


def _dpapi_protect(raw: bytes) -> bytes | None:
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
    """仅使用 Windows DPAPI 保存会话 cookie。"""

    MAGIC = b"LA-DPAPI-1\0"

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_SESSION_PATH

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def save(self, session: BrowserSession) -> bool:
        payload = {
            "version": 1,
            "cookies": [asdict(cookie) for cookie in session.cookies],
            "user_agent": session.user_agent,
            "csrf_token": session.csrf_token,
            "captured_at": session.captured_at,
            "source": session.source,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        protected = _dpapi_protect(raw)
        if protected is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self.MAGIC + protected)
        return True

    def load(self) -> BrowserSession | None:
        if not self.path.is_file():
            return None
        try:
            raw = self.path.read_bytes()
            if not raw.startswith(self.MAGIC):
                return None
            plain = _dpapi_unprotect(raw[len(self.MAGIC) :])
            if plain is None:
                return None
            payload = json.loads(plain.decode("utf-8"))
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

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True


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
