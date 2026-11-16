"""清华网络学堂只读 Web provider。

接口路径和字段映射依据当前 ``thu-learn-lib`` 与 ``learn2018-autodown`` 源码整理：

* 登录入口使用隔离可见 Playwright 浏览器，用户或授权智能体完成认证后仅提取 cookie；
* 课程使用 ``getCurrentAndNextSemester`` 与 student course list；
* 文件使用 ``kjxxbByWlkcidAndSizeForStudent``、分类列表和 ``downloadFile``；
* 作业使用新/已提交/已批阅三个列表，并按作业页面与 detail JSON 补充详情。

这里不实现任何提交/写入远端接口。所有请求默认 ``verify=True``，显式传入 False 会被拒绝。
"""

from __future__ import annotations
from ..errors import ProviderRateLimited, ProviderMaintenance

import html
import json
import re
import time
from dataclasses import replace
from html.parser import HTMLParser
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from ..auth import (
    DEFAULT_LOGIN_URL,
    PlaywrightLogin,
    SecureSessionStore,
    browser_session_from_cookies,
    extract_csrf_token,
)
from ..errors import (
    AuthenticationRequired,
    InterfaceDriftError,
    ProviderNotConfigured,
    ValidationError,
)
from ..models import Assignment, BrowserSession, Course, CourseFile, LoginStatus
from ..transport import HttpTransport, RequestsTransport, TransportResponse


class BrowserSessionAdapter(Protocol):
    """从已经完成认证的浏览器提取 cookie 的最小协议。"""

    def extract_cookies(self) -> Mapping[str, str]:
        ...


class SeleniumSessionAdapter:
    """兼容 Selenium driver 的会话提取适配器，不启动浏览器也不填写密码。"""

    def __init__(self, driver: Any) -> None:
        self.driver = driver

    def extract_cookies(self) -> Mapping[str, str]:
        cookies = self.driver.get_cookies()
        return {
            str(item["name"]): str(item["value"])
            for item in cookies
            if isinstance(item, Mapping) and "name" in item and "value" in item
        }

    def capture_session(self, *, user_agent: str = "", csrf_token: str = "") -> BrowserSession:
        cookies = self.driver.get_cookies()
        return browser_session_from_cookies(
            cookies,
            user_agent=user_agent,
            csrf_token=csrf_token,
            source="selenium-injected",
        )


class _HomeworkDetailParser(HTMLParser):
    """解析作业详情页面中 ``list/left/right`` 结构，不依赖 BeautifulSoup。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[set[str]] = []
        self.records: list[tuple[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture: str | None = None
        self.capture_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes: set[str] = set()
        if tag.lower() == "div":
            attrs_map = dict(attrs)
            classes = set((attrs_map.get("class") or "").split())
            if "list" in classes:
                self.current = {"label": "", "value": ""}
            if self.current is not None and "left" in classes:
                self.capture = "label"
                self.capture_depth = len(self.stack) + 1
            elif self.current is not None and "right" in classes:
                self.capture = "value"
                self.capture_depth = len(self.stack) + 1
        self.stack.append(classes)

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            classes = self.stack.pop()
            if self.capture_depth == len(self.stack) + 1:
                self.capture = None
                self.capture_depth = None
            if "list" in classes and self.current is not None:
                label = self.current["label"].strip()
                value = self.current["value"].strip()
                if label:
                    self.records.append((label, value))
                self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture is not None:
            self.current[self.capture] += data


def _decode_text(value: Any) -> str:
    return html.unescape("" if value is None else str(value)).strip()


def _first(raw: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _append_query(url: str, params: Mapping[str, Any]) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, str(value)) for key, value in params.items())
    return urlunparse(parsed._replace(query=urlencode(query)))


class _LoginHtmlParser(HTMLParser):
    """Inspect visible prompts and form controls, ignoring script vocabulary."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.visible: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.form: dict[str, Any] | None = None
        self.controls: list[str] = []
        self.control_stack: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template"}:
            self.ignored += 1
        if self.ignored:
            return
        values = {key: (value or "").casefold() for key, value in attrs}
        if tag == "form":
            self.form = {"hint": " ".join(values.get(key, "") for key in ("id", "name", "action")), "password": False, "identity": False}
            self.forms.append(self.form)
        elif tag == "input" and self.form is not None:
            names = {values.get("name", ""), values.get("id", "")}
            self.form["password"] |= values.get("type") == "password" or bool(names & {"i_pass", "password", "passwd"})
            self.form["identity"] |= bool(names & {"i_user", "username", "user_name", "user", "account", "j_username"})
        if tag in {"a", "button", "title", "h1", "h2"}:
            self.control_stack.append((tag, []))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template"}:
            self.ignored = max(0, self.ignored - 1)
            return
        if self.ignored:
            return
        if tag == "form":
            self.form = None
        if self.control_stack and self.control_stack[-1][0] == tag:
            _, parts = self.control_stack.pop()
            self.controls.append("".join(parts))

    def handle_data(self, data: str) -> None:
        if self.ignored:
            return
        self.visible.append(data)
        for _, parts in self.control_stack:
            parts.append(data)


def _is_login_html(response: TransportResponse) -> bool:
    url = str(getattr(response, "url", "") or "")
    try:
        location = urlparse(url)
    except ValueError:
        location = urlparse("")
    if "login_timeout" in location.path.casefold() or location.hostname == "id.tsinghua.edu.cn":
        return True
    content_type = str((getattr(response, "headers", {}) or {}).get("content-type", "")).casefold()
    if "html" not in content_type:
        content = bytes(getattr(response, "content", b"") or b"")[:4096].lstrip().lower()
        if not (content.startswith(b"<!doctype html") or content.startswith(b"<html")):
            return False
    try:
        text = str(getattr(response, "text", "") or "")
    except Exception:
        text = ""
    parser = _LoginHtmlParser()
    parser.feed(text)
    if any(form["password"] and (form["identity"] or any(marker in form["hint"] for marker in ("login", "sso", "auth"))) for form in parser.forms):
        return True
    visible = re.sub(r"\s+", "", " ".join(parser.visible)).casefold()
    # A logged-in page normally contains '退出登录', and bundled scripts may
    # contain LOGIN/SSO/password symbols. Those are not evidence of expiry.
    visible = visible.replace("退出登录", "").replace("退出登陆", "")
    prompts = (
        "未登录", "未登陆", "尚未登录", "没有登录", "请先登录", "请重新登录", "重新登录",
        "登录超时", "登录已超时", "登录失效", "登录已失效", "登录过期", "会话已过期", "会话失效",
        "身份认证失败", "登录验证失败", "pleaselogin", "pleasesignin", "sessionexpired",
    )
    if any(marker in visible for marker in prompts):
        return True
    login_labels = {"登录", "登陆", "用户登录", "统一身份认证", "统一认证登录", "登录网络学堂", "网络学堂登录", "login", "signin"}
    return any(re.sub(r"\s+", "", label).casefold() in login_labels for label in parser.controls)


class WebProvider:
    """可注入 transport 的真实只读 provider。"""

    name = "web"
    base_url = "https://learn.tsinghua.edu.cn"
    homepage_path = "/f/wlxt/index/course/student/"
    default_headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://learn.tsinghua.edu.cn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    }

    def __init__(
        self,
        session_adapter: BrowserSessionAdapter | None = None,
        base_url: str | None = None,
        verify_tls: bool = True,
        *,
        transport: HttpTransport | None = None,
        session_store: SecureSessionStore | None = None,
        browser_session: BrowserSession | None = None,
        load_persisted: bool = True,
        timeout_seconds: int = 30,
    ) -> None:
        if not verify_tls:
            raise ValidationError("TLS 证书校验必须保持开启；WebProvider 不接受 verify_tls=False。")
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.verify_tls = True
        self.timeout_seconds = timeout_seconds
        self.transport = transport or RequestsTransport()
        self.session_store = session_store or SecureSessionStore()
        self.browser_session: BrowserSession | None = browser_session
        self._csrf_token = browser_session.csrf_token if browser_session else ""
        self._file_cache: dict[str, CourseFile] = {}
        self._assignment_cache: dict[str, Assignment] = {}
        if self.browser_session is None and load_persisted:
            self.browser_session = self.session_store.load()
            self._csrf_token = self.browser_session.csrf_token if self.browser_session else ""
        if session_adapter is not None and self.browser_session is None:
            cookies = session_adapter.extract_cookies()
            self.browser_session = browser_session_from_cookies(
                [{"name": name, "value": value, "domain": ".tsinghua.edu.cn"} for name, value in cookies.items()],
                source="injected",
            )
        self._apply_browser_session(self.browser_session)

    def _apply_browser_session(self, session: BrowserSession | None) -> None:
        if session is None:
            return
        if isinstance(self.transport, RequestsTransport):
            request_session = self.transport.session
            request_session.headers.update(self.default_headers)
            if session.user_agent:
                request_session.headers["User-Agent"] = session.user_agent
            request_session.cookies.clear()
            for cookie in session.cookies:
                if cookie.expires is not None and cookie.expires<=time.time():continue
                from requests.cookies import create_cookie
                item=create_cookie(cookie.name,cookie.value,domain=cookie.domain or 'learn.tsinghua.edu.cn',
                    path=cookie.path or '/',secure=cookie.secure,expires=int(cookie.expires) if cookie.expires is not None else None)
                item.domain_specified=bool(cookie.domain.startswith('.'))
                request_session.cookies.set_cookie(item)

    def _url(self, path: str) -> str:
        return path if path.startswith("http://") or path.startswith("https://") else urljoin(self.base_url + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        add_csrf: bool = True,
    ) -> TransportResponse:
        url = self._url(path)
        query_params: dict[str, Any] = dict(params or {})
        if add_csrf and self._csrf_token:
            query_params.setdefault("_csrf", self._csrf_token)
        if query_params:
            url = _append_query(url, query_params)
        headers = dict(self.default_headers)
        if self.browser_session and self.browser_session.user_agent:
            headers["User-Agent"] = self.browser_session.user_agent
        if add_csrf and self._csrf_token:
            headers["X-XSRF-TOKEN"] = self._csrf_token
        response = self.transport.request(
            method.upper(),
            url,
            data=dict(data) if data else None,
            headers=headers,
            timeout=self.timeout_seconds,
            verify=True,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        response_url = str(getattr(response, "url", "") or url)
        if status==429:raise ProviderRateLimited('LEARN_RATE_LIMITED')
        if status==503 and re.search(r'维护|maintenance',str(getattr(response,'text','')),re.I):
            raise ProviderMaintenance('LEARN_MAINTENANCE')
        if status == 403:
            raise PermissionError('LEARN_FORBIDDEN')
        if status == 401 or "login_timeout" in response_url.casefold() or _is_login_html(response):
            raise AuthenticationRequired("网络学堂会话已失效或返回登录页，请重新执行 login。")
        if status < 200 or status >= 300:
            raise InterfaceDriftError(f"网络学堂接口返回 HTTP {status}：{path}")
        if not self._csrf_token:
            try:
                token = extract_csrf_token(str(getattr(response, "text", "") or ""))
            except Exception:
                token = ""
            if token:
                self._csrf_token = token
        return response

    @staticmethod
    def _json(response: TransportResponse, endpoint: str) -> Any:
        try:
            value = response.json()
        except Exception as exc:
            raise InterfaceDriftError(f"接口 {endpoint} 未返回有效 JSON。") from exc
        if not isinstance(value, (dict, list)):
            raise InterfaceDriftError(f"接口 {endpoint} 返回结构不是对象或数组。")
        if isinstance(value, Mapping):
            if value.get('success') is False:
                raise InterfaceDriftError('网络学堂业务接口返回失败状态。')
            marker = value.get("result") if isinstance(value.get("result"), str) else value.get("message")
            if isinstance(marker, str) and marker.casefold() not in {"success", "ok"}:
                raise InterfaceDriftError(f"接口 {endpoint} 返回失败状态：{marker}。")
        return value

    def _require_session(self) -> None:
        if self.browser_session is None or not self.browser_session.cookies:
            raise AuthenticationRequired("尚未获得用户浏览器会话，请先执行 login。")

    def set_browser_session(self, session: BrowserSession, *, persist: bool = False) -> dict[str, Any]:
        self.browser_session = session
        self._csrf_token = session.csrf_token
        self._apply_browser_session(session)
        persisted = False
        if persist:
            persisted = self.session_store.save(session)
        message = (
            "会话已使用 Windows DPAPI 加密保存。"
            if persisted
            else "会话仅保留在当前进程；DPAPI 不可用或未启用，不会明文落盘。"
        )
        return {
            "authenticated": True,
            "provider": self.name,
            "cookie_count": len(session.cookies),
            "persisted": persisted,
            "session_path": str(self.session_store.path) if persisted else None,
            "persistence_available": self.session_store.available,
            "message": message,
        }

    def login(
        self,
        *,
        start_url: str | None = None,
        timeout_seconds: int = 300,
        persist: bool = True,
    ) -> dict[str, Any]:
        session = PlaywrightLogin().capture(
            start_url=start_url or DEFAULT_LOGIN_URL,
            timeout_seconds=timeout_seconds,
        )
        return self.set_browser_session(session, persist=persist)

    def get_login_status(self) -> LoginStatus:
        if self.browser_session is None or not self.browser_session.cookies:
            return LoginStatus(
                authenticated=False,
                provider=self.name,
                message="未找到 DPAPI 会话；请运行 login 并通过可用浏览器/桌面工具完成认证，或直接使用已登录的浏览器读取网页。",
            )
        try:
            response = self._request("GET", self.homepage_path, add_csrf=False)
            if not self._csrf_token:
                self._csrf_token = extract_csrf_token(str(getattr(response, "text", "") or ""))
            return LoginStatus(
                authenticated=True,
                provider=self.name,
                message="已通过网络学堂首页验证当前浏览器会话。",
            )
        except AuthenticationRequired as exc:
            return LoginStatus(authenticated=False, provider=self.name, message=str(exc))

    def _current_semester(self) -> str:
        response = self._request("GET", "/b/kc/zhjw_v_code_xnxq/getCurrentAndNextSemester")
        payload = self._json(response, "getCurrentAndNextSemester")
        result = payload.get("result") if isinstance(payload, dict) else None
        semester = result.get("xnxq") if isinstance(result, dict) else None
        if not semester:
            raise InterfaceDriftError("当前学期接口缺少 result.xnxq。")
        return str(semester)

    def list_courses(self, semester: str | None = None, query: str | None = None) -> list[Course]:
        self._require_session()
        semester_id = semester or self._current_semester()
        response = self._request(
            "GET",
            f"/b/wlxt/kc/v_wlkc_xs_xkb_kcb_extend/student/loadCourseBySemesterId/{semester_id}/zh/",
        )
        payload = self._json(response, "loadCourseBySemesterId")
        raw_courses = payload.get("resultList") if isinstance(payload, dict) else None
        if not isinstance(payload,dict) or not any(payload.get(k)=='success' for k in ('message','result')):
            raise InterfaceDriftError('课程接口缺少明确成功状态。')
        if not isinstance(raw_courses, list):
            raise InterfaceDriftError("课程接口缺少 resultList 数组。")
        courses: list[Course] = []
        for raw in raw_courses:
            if not isinstance(raw, Mapping):
                raise InterfaceDriftError("课程接口包含非对象元素。")
            course_id = _first(raw, "wlkcid", "id")
            if not course_id:
                raise InterfaceDriftError("课程元素缺少 wlkcid。")
            name = _decode_text(_first(raw, "kcm", "zywkcm", "ywkcm", default=""))
            course = Course(
                id=str(course_id),
                name=name,
                semester=semester_id,
                teacher=_decode_text(_first(raw, "jsm", "teacherName", default="")),
                role="student",
            )
            if query and query.casefold() not in course.name.casefold():
                continue
            courses.append(course)
        return courses

    def _assignment_from_raw(
        self,
        raw: Mapping[str, Any],
        course_id: str,
        course_name: str,
        *,
        submitted: bool,
        graded: bool,
    ) -> Assignment:
        assignment_id = _first(raw, "xszyid", "zyid", "id")
        if not assignment_id:
            raise InterfaceDriftError("作业接口元素缺少 xszyid/zyid。")
        status = "已批阅" if graded else "已提交" if submitted else "未提交"
        return Assignment(
            id=str(assignment_id),
            base_id=str(_first(raw, "zyid", "baseId", default=assignment_id)),
            course_id=str(_first(raw, "wlkcid", default=course_id)),
            course_name=course_name,
            title=_decode_text(_first(raw, "bt", "title", default="")),
            deadline=str(_first(raw, "jzsj", "deadline", default="")) or None,
            status=status,
            instructions="",
            allowed_extensions=(),
            max_bytes=None,
            allow_multiple_files=True,
            submitted_at=str(_first(raw, "scsj", "submitTime", default="")) or None,
            late_submission_deadline=str(_first(raw, "bjjzsj", "lateSubmissionDeadline", default="")) or None,
            completion_type=_first(raw, "zywcfs", "completionType"),
            submission_type=_first(raw, "zytjfs", "submissionType"),
            grade=_first(raw, "cj", "grade"),
        )

    def _list_assignments_for_course(self, course_id: str, course_name: str = "") -> list[Assignment]:
        sources = (
            ("/b/wlxt/kczy/zy/student/zyListWj", False, False),
            ("/b/wlxt/kczy/zy/student/zyListYjwg", True, False),
            ("/b/wlxt/kczy/zy/student/zyListYpg", True, True),
        )
        by_id: dict[str, Assignment] = {}
        body = {"aoData": json.dumps([{"name": "wlkcid", "value": course_id}], ensure_ascii=False)}
        for endpoint, submitted, graded in sources:
            payload = self._json(self._request("POST", endpoint, data=body), endpoint)
            obj = payload.get("object") if isinstance(payload, dict) else None
            rows = obj.get("aaData") if isinstance(obj, Mapping) else None
            if not isinstance(rows, list):
                raise InterfaceDriftError(f"作业接口 {endpoint} 缺少 object.aaData 数组。")
            for raw in rows:
                if not isinstance(raw, Mapping):
                    raise InterfaceDriftError(f"作业接口 {endpoint} 包含非对象元素。")
                assignment = self._assignment_from_raw(
                    raw,
                    course_id,
                    course_name,
                    submitted=submitted,
                    graded=graded,
                )
                prior = by_id.get(assignment.id)
                if prior is None or (graded and prior.status != "已批阅"):
                    by_id[assignment.id] = assignment
        return list(by_id.values())

    def list_assignments(self, course_id: str | None = None) -> list[Assignment]:
        self._require_session()
        if course_id:
            courses = [course for course in self.list_courses() if course.id == course_id]
            course_name = courses[0].name if courses else ""
            assignments = self._list_assignments_for_course(course_id, course_name)
        else:
            assignments = []
            for course in self.list_courses():
                assignments.extend(self._list_assignments_for_course(course.id, course.name))
        self._assignment_cache.update({item.id: item for item in assignments})
        return assignments

    def _find_assignment(self, assignment_id: str) -> Assignment:
        if assignment_id in self._assignment_cache:
            return self._assignment_cache[assignment_id]
        for item in self.list_assignments():
            if item.id == assignment_id or item.base_id == assignment_id:
                self._assignment_cache[item.id] = item
                return item
        raise ValidationError(f"未找到作业：{assignment_id}")

    def get_assignment(self, assignment_id: str) -> Assignment:
        self._require_session()
        assignment = self._find_assignment(assignment_id)
        page_endpoint = f"/f/wlxt/kczy/zy/student/viewCj?wlkcid={assignment.course_id}&xszyid={assignment.id}"
        page_response = self._request("GET", page_endpoint)
        parser = _HomeworkDetailParser()
        parser.feed(str(getattr(page_response, "text", "") or ""))
        detail = {label: value for label, value in parser.records}
        json_response = self._request(
            "POST",
            "/b/wlxt/kczy/zy/student/detail",
            data={"id": assignment.base_id},
        )
        detail_payload = self._json(json_response, "student/detail")
        detail_text = _decode_text(detail_payload.get("msg") if isinstance(detail_payload, Mapping) else "")
        instructions = detail_text or detail.get("作业说明", "")
        if detail.get("截止日期(GMT+8)") and not assignment.deadline:
            assignment = replace(assignment, deadline=detail["截止日期(GMT+8)"])
        assignment = replace(
            assignment,
            instructions=instructions,
            late_submission_deadline=assignment.late_submission_deadline or detail.get("补交截止时间") or None,
        )
        self._assignment_cache[assignment.id] = assignment
        return assignment

    def _course_file_categories(self, course_id: str) -> dict[str, str]:
        endpoint = f"/b/wlxt/kj/wlkc_kjflb/student/pageList?wlkcid={course_id}"
        payload = self._json(self._request("GET", endpoint), endpoint)
        obj = payload.get("object") if isinstance(payload, Mapping) else None
        rows = obj.get("rows") if isinstance(obj, Mapping) else None
        if not isinstance(rows, list):
            raise InterfaceDriftError("课程文件分类接口缺少 object.rows 数组。")
        result: dict[str, str] = {}
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise InterfaceDriftError("课程文件分类接口包含非对象元素。")
            category_id = _first(raw, "kjflid", "id")
            title = _first(raw, "bt", "title", default="未分类")
            if category_id:
                result[str(category_id)] = _decode_text(title) or "未分类"
        return result

    def list_course_files(self, course_id: str | None = None, query: str | None = None) -> list[CourseFile]:
        self._require_session()
        if course_id is None:
            courses = self.list_courses()
        else:
            found = [course for course in self.list_courses() if course.id == course_id]
            courses = found or [Course(course_id, "", "", role="student")]
        result: list[CourseFile] = []
        for course in courses:
            endpoint = f"/b/wlxt/kj/wlkc_kjxxb/student/kjxxbByWlkcidAndSizeForStudent?wlkcid={course.id}&size=200"
            payload = self._json(self._request("GET", endpoint), endpoint)
            obj = payload.get("object") if isinstance(payload, Mapping) else None
            raw_files = obj if isinstance(obj, list) else None
            if raw_files is None and isinstance(obj, Mapping):
                raw_files = obj.get("resultsList")
            if not isinstance(raw_files, list):
                raise InterfaceDriftError(f"课程文件接口 {endpoint} 缺少 object 数组/resultsList。")
            categories = self._course_file_categories(course.id)
            for raw in raw_files:
                if not isinstance(raw, Mapping):
                    raise InterfaceDriftError("课程文件接口包含非对象元素。")
                file_id = _first(raw, "wjid", "fileId", "id")
                name = _decode_text(_first(raw, "bt", "title", "name", default=""))
                if not file_id or not name:
                    raise InterfaceDriftError("课程文件元素缺少 wjid 或 bt。")
                category = categories.get(str(_first(raw, "kjflid", "categoryId", default="")), "未分类")
                item = CourseFile(
                    id=str(file_id),
                    course_id=course.id,
                    course_name=course.name,
                    name=name,
                    relative_path=f"{category}/{name}",
                    size=_as_int(_first(raw, "wjdx", "rawSize", default=0)),
                    mime_type=_decode_text(_first(raw, "wjlx", "fileType", default="application/octet-stream")),
                    uploaded_at=str(_first(raw, "scsj", "uploadTime", default="")) or None,
                )
                if query and query.casefold() not in f"{item.name} {item.relative_path}".casefold():
                    continue
                result.append(item)
                self._file_cache[item.id] = item
        return result

    def get_course_file(self, file_id: str) -> CourseFile:
        self._require_session()
        if file_id in self._file_cache:
            return self._file_cache[file_id]
        for item in self.list_course_files():
            if item.id == file_id:
                return item
        raise ValidationError(f"未找到课程文件：{file_id}")

    def download_file(self, file_id: str) -> bytes:
        self._require_session()
        self.get_course_file(file_id)
        endpoint = f"/b/wlxt/kj/wlkc_kjxxb/student/downloadFile?sfgk=0&wjid={file_id}"
        response = self._request("GET", endpoint)
        content = bytes(getattr(response, "content", b"") or b"")
        if not content:
            raise InterfaceDriftError(f"文件下载接口返回空内容：{file_id}")
        return content
