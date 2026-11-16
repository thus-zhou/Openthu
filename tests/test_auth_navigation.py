"""Login navigation regressions using a fake browser; no network or credentials."""
from contextlib import contextmanager
import sys
from types import ModuleType, SimpleNamespace

import pytest

from learn_assistant import auth
from learn_assistant.errors import AuthenticationRequired


LEARNING = "https://learn.tsinghua.edu.cn/f/wlxt/index/course/student/"
SUCCESS = '<a href="myCourse">My courses</a><a>退出登录</a>'
TRANSIENT = "Page.content: Unable to retrieve content because the page is navigating and changing the content."


class FakeTimeout(Exception):
    pass


class FakeClock:
    value = 0.0

    def monotonic(self):
        return self.value


class FakePage:
    def __init__(self, clock, contents, *, url=LEARNING, goto_error=None, load_error=None, evaluate_error=None):
        self.clock = clock
        self.contents = list(contents)
        self.url = url
        self.goto_error = goto_error
        self.load_error = load_error
        self.evaluate_error = evaluate_error
        self.content_calls = 0
        self.goto_calls = []
        self.waits = 0
        self.browser = None

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error

    def wait_for_load_state(self, state, **kwargs):
        assert state == "domcontentloaded"
        if self.load_error:
            error, self.load_error = self.load_error, None
            raise error

    def content(self):
        assert self.browser.closed is False
        self.content_calls += 1
        item = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(self)
        return item

    def evaluate(self, expression):
        assert expression == "() => navigator.userAgent"
        if self.evaluate_error:
            error, self.evaluate_error = self.evaluate_error, None
            raise error
        return "fixture-agent"

    def wait_for_timeout(self, milliseconds):
        assert not self.browser.closed
        self.waits += 1
        self.clock.value += milliseconds / 1000


def install_browser(monkeypatch, contents, **kwargs):
    clock = FakeClock()
    page = FakePage(clock, contents, **kwargs)
    context = SimpleNamespace(new_page=lambda: page, cookie_calls=0)

    def cookies():
        context.cookie_calls += 1
        return [{"name":"fixture-session", "value":"not-a-real-cookie", "domain":"learn.tsinghua.edu.cn"}]

    context.cookies = cookies
    browser = SimpleNamespace(closed=False, new_context=lambda: context)
    browser.close = lambda: setattr(browser, "closed", True)
    page.browser = browser

    def launch(**launch_kwargs):
        assert launch_kwargs["headless"] is False
        return browser

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    package = ModuleType("playwright")
    module = ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright
    module.TimeoutError = FakeTimeout
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(auth.time, "monotonic", clock.monotonic)
    return page, context, browser


def test_content_navigation_keeps_visible_browser_until_success(monkeypatch):
    page, context, browser = install_browser(monkeypatch, [RuntimeError(TRANSIENT), SUCCESS, SUCCESS])
    session = auth.PlaywrightLogin().capture(timeout_seconds=3)
    assert session.user_agent == "fixture-agent"
    assert page.waits == 1
    assert len(page.goto_calls) == 1
    assert context.cookie_calls == 1
    assert browser.closed


def test_final_content_navigation_retries_capture(monkeypatch):
    page, context, browser = install_browser(monkeypatch, [SUCCESS, RuntimeError(TRANSIENT), SUCCESS, SUCCESS])
    auth.PlaywrightLogin().capture(timeout_seconds=3)
    assert page.content_calls == 4
    assert context.cookie_calls == 1
    assert page.waits == 1
    assert browser.closed


def test_navigation_during_user_agent_read_retries(monkeypatch):
    page, context, _ = install_browser(monkeypatch, [SUCCESS], evaluate_error=RuntimeError("Execution context was destroyed, most likely because of a navigation"))
    auth.PlaywrightLogin().capture(timeout_seconds=3)
    assert page.waits == 1
    assert context.cookie_calls == 1


@pytest.mark.parametrize("goto_error", [FakeTimeout("Page.goto timed out"), RuntimeError("Page.goto: net::ERR_ABORTED")])
def test_initial_redirect_or_slow_load_continues_user_wait(monkeypatch, goto_error):
    page, context, _ = install_browser(monkeypatch, [SUCCESS], goto_error=goto_error, load_error=FakeTimeout("load state timeout"))
    auth.PlaywrightLogin().capture(timeout_seconds=3)
    assert len(page.goto_calls) == 1
    assert context.cookie_calls == 1


@pytest.mark.parametrize("url,html", [
    (LEARNING, '<form>请登录</form>'),
    ("https://id.tsinghua.edu.cn/login", SUCCESS),
    ("https://learn.tsinghua.edu.cn.example.invalid/", SUCCESS),
    ("https://example.invalid/?next=learn.tsinghua.edu.cn", SUCCESS),
    ("https://learn.tsinghua.edu.cn/login", SUCCESS),
])
def test_host_and_success_marker_are_both_required(monkeypatch, url, html):
    page, context, browser = install_browser(monkeypatch, [html], url=url)
    with pytest.raises(AuthenticationRequired):
        auth.PlaywrightLogin().capture(timeout_seconds=2)
    assert context.cookie_calls == 0
    assert page.waits == 2
    assert browser.closed


def test_final_page_loses_success_marker_before_session_capture(monkeypatch):
    page, context, _ = install_browser(monkeypatch, [SUCCESS, '<form>请登录</form>'])
    with pytest.raises(AuthenticationRequired):
        auth.PlaywrightLogin().capture(timeout_seconds=2)
    assert context.cookie_calls == 0
    assert page.waits == 2


def test_url_change_during_content_read_cannot_capture_session(monkeypatch):
    def navigate(page):
        page.url = "https://id.tsinghua.edu.cn/login"
        return SUCCESS
    _, context, _ = install_browser(monkeypatch, [navigate, SUCCESS])
    with pytest.raises(AuthenticationRequired):
        auth.PlaywrightLogin().capture(timeout_seconds=2)
    assert context.cookie_calls == 0


def test_unexpected_browser_error_is_not_silently_retried(monkeypatch):
    page, context, browser = install_browser(monkeypatch, [RuntimeError("Target page, context or browser has been closed")])
    with pytest.raises(RuntimeError, match="has been closed"):
        auth.PlaywrightLogin().capture(timeout_seconds=2)
    assert page.waits == 0
    assert context.cookie_calls == 0
    assert browser.closed
