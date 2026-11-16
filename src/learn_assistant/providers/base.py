"""可替换的数据提供方协议。

核心逻辑只依赖这个协议，因此后续可以把真实网络学堂实现、缓存实现或测试实现替换进来。
"""

from __future__ import annotations

from typing import Protocol

from ..models import Assignment, Course, CourseFile, LoginStatus


class LearningProvider(Protocol):
    """助手所需的最小数据访问接口。"""

    name: str

    def get_login_status(self) -> LoginStatus:
        ...

    def list_courses(self, semester: str | None = None, query: str | None = None) -> list[Course]:
        ...

    def list_assignments(self, course_id: str | None = None) -> list[Assignment]:
        ...

    def get_assignment(self, assignment_id: str) -> Assignment:
        ...

    def list_course_files(
        self, course_id: str | None = None, query: str | None = None
    ) -> list[CourseFile]:
        ...

    def get_course_file(self, file_id: str) -> CourseFile:
        ...

    def download_file(self, file_id: str) -> bytes:
        ...

    def login(
        self,
        *,
        start_url: str | None = None,
        timeout_seconds: int = 300,
        persist: bool = True,
    ) -> dict[str, object]:
        ...
