from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

import learn_assistant.auth as auth_module
from learn_assistant.auth import SecureSessionStore
from learn_assistant.errors import AuthenticationRequired, InterfaceDriftError, ValidationError
from learn_assistant.models import BrowserCookie, BrowserSession
from learn_assistant.providers.web import WebProvider, _is_login_html


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "web"


class FakeResponse:
    def __init__(self, *, status_code: int = 200, body: bytes = b"", json_data=None, url: str = ""):
        self.status_code = status_code
        self.content = body
        self._json_data = json_data
        self.url = url
        self.headers = {"content-type": "application/json" if json_data is not None else "text/html"}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        if self._json_data is None:
            raise ValueError("not JSON")
        return self._json_data


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlparse(url).path
        if path == "/f/wlxt/index/course/student/":
            return _html("homepage.html", url)
        if path.endswith("/getCurrentAndNextSemester"):
            return _json("current_semester.json", url)
        if "/loadCourseBySemesterId/" in path:
            return _json("courses.json", url)
        if path.endswith("/zyListWj"):
            return _json("assignments_new.json", url)
        if path.endswith("/zyListYjwg"):
            return _json("assignments_submitted.json", url)
        if path.endswith("/zyListYpg"):
            return _json("assignments_graded.json", url)
        if path.endswith("/viewCj"):
            return _html("assignment_detail.html", url)
        if path.endswith("/student/detail"):
            return _json("assignment_detail.json", url)
        if path.endswith("kjxxbByWlkcidAndSizeForStudent"):
            return _json("files.json", url)
        if path.endswith("kjflb/student/pageList"):
            return _json("categories.json", url)
        if path.endswith("/downloadFile"):
            return _binary("download.pdf", url)
        return FakeResponse(status_code=404, body=b"not found", url=url)


def _body(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def _json(name: str, url: str) -> FakeResponse:
    payload = json.loads(_body(name).decode("utf-8"))
    return FakeResponse(json_data=payload, url=url)


def _html(name: str, url: str) -> FakeResponse:
    response = FakeResponse(body=_body(name), url=url)
    response.headers = {"content-type": "text/html; charset=utf-8"}
    return response


def _binary(name: str, url: str) -> FakeResponse:
    response = FakeResponse(body=_body(name), url=url)
    response.headers = {"content-type": "application/pdf"}
    return response


def _session() -> BrowserSession:
    return BrowserSession(
        cookies=(BrowserCookie(name="JSESSIONID", value="fixture-session", domain="learn.tsinghua.edu.cn"),),
        user_agent="fixture-agent",
        source="fixture",
    )


def _provider(transport: FakeTransport) -> WebProvider:
    return WebProvider(
        transport=transport,
        browser_session=_session(),
        session_store=SecureSessionStore(path=FIXTURE_ROOT / "unused.dpapi"),
        load_persisted=False,
    )


def test_cookie_import_keeps_paths_expiry_and_session_cookie_semantics():
    import time
    from learn_assistant.auth import browser_session_from_cookies
    from learn_assistant.transport import RequestsTransport
    cookies=[{'name':'sid','value':'session','domain':'learn.tsinghua.edu.cn','path':'/','expires':-1,'secure':True},
             {'name':'sid','value':'path','domain':'.tsinghua.edu.cn','path':'/course','expires':time.time()+600,'secure':True},
             {'name':'expired','value':'old','domain':'learn.tsinghua.edu.cn','path':'/','expires':1}]
    transport=RequestsTransport()
    WebProvider(transport=transport,browser_session=browser_session_from_cookies(cookies),load_persisted=False)
    imported=list(transport.session.cookies)
    assert len(imported)==2
    host=next(c for c in imported if c.path=='/')
    assert host.expires is None and host.secure and not host.domain_specified
    scoped=next(c for c in imported if c.path=='/course')
    assert scoped.expires>time.time() and scoped.domain_specified


def test_forbidden_is_not_an_expired_session():
    class Forbidden(FakeTransport):
        def request(self,*a,**k):return FakeResponse(status_code=403,body=b'forbidden')
    with pytest.raises(PermissionError,match='LEARN_FORBIDDEN'):_provider(Forbidden()).list_courses()


@pytest.mark.parametrize('payload',[{'success':False,'resultList':[]},{'message':'failure','resultList':[]},{'resultList':[]},{'code':0,'resultList':[]}])
def test_courses_require_known_success_contract(payload):
    class Changed(FakeTransport):
        def request(self,method,url,**kwargs):
            if 'loadCourseBySemesterId' in url:return FakeResponse(json_data=payload,url=url)
            return super().request(method,url,**kwargs)
    with pytest.raises(InterfaceDriftError):_provider(Changed()).list_courses()


def test_missing_assignment_list_is_not_empty():
    class Missing(FakeTransport):
        def request(self,method,url,**kwargs):
            if '/zyList' in url:return FakeResponse(json_data={'result':'success','object':{}},url=url)
            return super().request(method,url,**kwargs)
    with pytest.raises(InterfaceDriftError):_provider(Missing()).list_assignments(course_id='course-real-1')


@pytest.mark.parametrize('status,body,error',[(429,b'rate limit','ProviderRateLimited'),(503,'系统维护'.encode(),'ProviderMaintenance')])
def test_provider_throttle_and_maintenance_are_distinct(status,body,error):
    class Unavailable(FakeTransport):
        def request(self,*a,**k):return FakeResponse(status_code=status,body=body)
    with pytest.raises(InterfaceDriftError) as caught:_provider(Unavailable()).list_courses()
    assert type(caught.value).__name__==error


def test_web_provider_parses_courses_assignments_files_and_download():
    transport = FakeTransport()
    provider = _provider(transport)

    status = provider.get_login_status()
    assert status.authenticated is True
    assert provider._csrf_token == "fixture-csrf-token"

    courses = provider.list_courses()
    assert courses[0].id == "course-real-1"
    assert courses[0].name == "工程地质"

    assignments = provider.list_assignments(course_id="course-real-1")
    assert {item.id for item in assignments} == {"student-hw-1", "student-hw-2"}
    submitted = next(item for item in assignments if item.id == "student-hw-2")
    assert submitted.status == "已提交"
    assert submitted.base_id == "base-hw-2"

    detail = provider.get_assignment("student-hw-1")
    assert "边坡稳定性" in detail.instructions
    assert detail.late_submission_deadline is not None

    files = provider.list_course_files(course_id="course-real-1")
    assert files[0].id == "remote-file-1"
    assert files[0].relative_path == "课件/工程地质绪论.pdf"
    assert provider.download_file("remote-file-1").startswith(b"%PDF")
    assert all(call[2]["verify"] is True for call in transport.calls)


def test_web_provider_never_falls_back_to_mock_and_rejects_disabled_tls():
    with pytest.raises(AuthenticationRequired):
        WebProvider(transport=FakeTransport(), load_persisted=False).list_courses()
    with pytest.raises(ValidationError):
        WebProvider(transport=FakeTransport(), verify_tls=False, load_persisted=False)


def test_web_provider_surfaces_interface_drift():
    class DriftTransport(FakeTransport):
        def request(self, method: str, url: str, **kwargs):
            path = urlparse(url).path
            if path.endswith("/getCurrentAndNextSemester"):
                return FakeResponse(json_data={"message": "success"}, url=url)
            return super().request(method, url, **kwargs)

    with pytest.raises(InterfaceDriftError, match="xnxq"):
        _provider(DriftTransport()).list_courses()


def test_login_status_rejects_login_html_response():
    class LoginTransport(FakeTransport):
        def request(self, method: str, url: str, **kwargs):
            if urlparse(url).path == "/f/wlxt/index/course/student/":
                response = FakeResponse(body=b"<!doctype html><form id='theform'><input id='i_user'></form>", url="https://id.tsinghua.edu.cn/login")
                response.headers = {"content-type": "text/html"}
                return response
            return super().request(method, url, **kwargs)

    status = _provider(LoginTransport()).get_login_status()
    assert status.authenticated is False
    assert "会话" in status.message


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_dpapi_session_store_never_writes_plaintext_cookie(tmp_path):
    session = _session()
    store = SecureSessionStore(tmp_path / "session.dpapi")
    assert store.save(session) is True
    raw = (tmp_path / "session.dpapi").read_bytes()
    assert b"fixture-session" not in raw
    loaded = store.load()
    assert loaded is not None
    assert loaded.cookies[0].value == "fixture-session"


def test_session_store_does_not_fallback_to_plaintext(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_module, "_dpapi_protect", lambda raw: None)
    store = SecureSessionStore(tmp_path / "session.dpapi")
    assert store.save(_session()) is False
    assert not (tmp_path / "session.dpapi").exists()


def test_logged_in_homepage_logout_and_script_login_words_are_not_expiry():
    class HomepageTransport(FakeTransport):
        def request(self, method: str, url: str, **kwargs):
            if urlparse(url).path == "/f/wlxt/index/course/student/":
                return _html("homepage_with_logout.html", url)
            return super().request(method, url, **kwargs)

    provider = _provider(HomepageTransport())
    assert provider.get_login_status().authenticated is True
    assert provider._csrf_token == "fixture-csrf-token"
    assert provider.list_courses()[0].id == "course-real-1"


@pytest.mark.parametrize("fixture", ["session_expired_notice.html", "login_form.html"])
def test_same_learning_url_still_rejects_expiry_notice_or_login_form(fixture):
    class ExpiredTransport(FakeTransport):
        def request(self, method: str, url: str, **kwargs):
            if urlparse(url).path == "/f/wlxt/index/course/student/":
                return _html(fixture, url)
            return super().request(method, url, **kwargs)

    provider = _provider(ExpiredTransport())
    assert provider.get_login_status().authenticated is False


@pytest.mark.parametrize("body", [
    '<html><body><p>介绍 SSO 与 password 字段，说明 LOGIN 状态。</p></body></html>',
    '<html><body><a>退出 登录</a><p>上次登录时间：测试时间。</p></body></html>',
    '<html><body><!-- 请重新登录 --><script>var message="未登录";</script><p>我的课程</p></body></html>',
])
def test_generic_authentication_words_do_not_indicate_login_page(body):
    response = FakeResponse(body=body.encode("utf-8"), url="https://learn.tsinghua.edu.cn/f/wlxt/index/course/student/")
    assert _is_login_html(response) is False


@pytest.mark.parametrize("body", [
    '<html><body><p>您尚未登录，当前会话失效。</p></body></html>',
    '<html><body><a href="/">登录网络学堂</a></body></html>',
    '<html><body><h1>Please sign in</h1></body></html>',
])
def test_visible_short_login_prompts_remain_authentication_required(body):
    response = FakeResponse(body=body.encode("utf-8"), url="https://learn.tsinghua.edu.cn/f/wlxt/index/course/student/")
    assert _is_login_html(response) is True
