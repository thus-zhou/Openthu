"""与网络学堂领域对象对应的、可序列化的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class LoginStatus:
    authenticated: bool
    provider: str
    account: str | None = None
    expires_at: str | None = None
    message: str = ""
    error_code: str | None = None


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    semester: str
    teacher: str = ""
    role: str = "student"


@dataclass(frozen=True)
class Assignment:
    id: str
    course_id: str
    course_name: str
    title: str
    deadline: str | None
    status: str
    base_id: str = ""
    instructions: str = ""
    allowed_extensions: tuple[str, ...] = field(default_factory=tuple)
    max_bytes: int | None = None
    allow_multiple_files: bool = True
    submitted_at: str | None = None
    late_submission_deadline: str | None = None
    completion_type: int | str | None = None
    submission_type: int | str | None = None
    grade: int | float | None = None
    grade_level: str | None = None
    submitted: bool | None = None
    graded: bool | None = None
    feedback: str | None = None
    graded_at: str | None = None
    overdue: bool | None = None
    attachments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    attachments_state: str = 'NOT_READ'
    attachments_hint: bool | None = None
    instructions_state: str = 'NOT_READ'


@dataclass(frozen=True)
class CourseFile:
    id: str
    course_id: str
    course_name: str
    name: str
    relative_path: str
    size: int
    mime_type: str = "application/octet-stream"
    uploaded_at: str | None = None
    source_kind: str = 'course_file'
    source_id: str | None = None


@dataclass(frozen=True)
class BrowserCookie:
    """浏览器导出的单个 cookie；不包含账号密码。"""

    name: str
    value: str
    domain: str = ""
    path: str = "/"
    secure: bool = True
    expires: float | None = None


@dataclass(frozen=True)
class BrowserSession:
    """可见浏览器完成认证后产生的短期会话材料。"""

    cookies: tuple[BrowserCookie, ...]
    user_agent: str = ""
    csrf_token: str = ""
    captured_at: str = ""
    source: str = "browser"


def to_dict(value: Any) -> Any:
    """递归转换 dataclass/tuple，供 JSON、MCP 和 CLI 输出。"""

    if hasattr(value, "__dataclass_fields__"):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_dict(item) for item in value]
    if isinstance(value, list):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: to_dict(item) for key, item in value.items()}
    return value
