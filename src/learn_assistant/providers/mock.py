"""不需要真实账号的可重复测试提供方。"""

from __future__ import annotations

from dataclasses import replace

from ..errors import ProviderNotConfigured, ValidationError
from ..models import Assignment, Course, CourseFile, LoginStatus


class MockProvider:
    """内置的课程、作业和文件数据。

    ``include_login_html`` 只用于验证下载器不会把登录页当作文件保存。
    """

    name = "mock"

    def __init__(self, include_login_html: bool = False) -> None:
        self._courses = [
            Course(
                id="course-geology",
                name="工程地质",
                semester="2026-2027-1",
                teacher="示例教师",
            ),
            Course(
                id="course-survey",
                name="测量学",
                semester="2026-2027-1",
                teacher="示例教师",
            ),
            Course(
                id="course-history",
                name="工程地质",
                semester="2025-2026-2",
                teacher="历史教师",
            ),
        ]
        self._assignments = [
            Assignment(
                id="assignment-geology-1",
                course_id="course-geology",
                course_name="工程地质",
                title="边坡稳定性分析",
                deadline="2026-09-20T23:59:00+08:00",
                status="未提交",
                instructions="提交边坡稳定性分析报告，要求 PDF 格式。",
                allowed_extensions=(".pdf",),
                max_bytes=20 * 1024 * 1024,
                allow_multiple_files=False,
            ),
            Assignment(
                id="assignment-survey-1",
                course_id="course-survey",
                course_name="测量学",
                title="实验二：导线测量",
                deadline="2026-09-25T23:59:00+08:00",
                status="未提交",
                instructions="上传实验报告，可包含 PDF 或 DOCX。",
                allowed_extensions=(".pdf", ".docx"),
                max_bytes=50 * 1024 * 1024,
            ),
        ]
        self._files = [
            CourseFile(
                id="file-geology-intro",
                course_id="course-geology",
                course_name="工程地质",
                name="工程地质绪论.pdf",
                relative_path="课件/工程地质绪论.pdf",
                size=33,
                mime_type="application/pdf",
                uploaded_at="2026-09-01T09:00:00+08:00",
            ),
            CourseFile(
                id="file-geology-homework",
                course_id="course-geology",
                course_name="工程地质",
                name="作业要求-边坡分析.pdf",
                relative_path="作业/作业要求-边坡分析.pdf",
                size=33,
                mime_type="application/pdf",
                uploaded_at="2026-09-03T10:00:00+08:00",
            ),
            CourseFile(
                id="file-survey-template",
                course_id="course-survey",
                course_name="测量学",
                name="导线测量记录表.docx",
                relative_path="实验/导线测量记录表.docx",
                size=39,
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                uploaded_at="2026-09-04T14:00:00+08:00",
            ),
        ]
        self._payloads = {
            "file-geology-intro": b"%PDF-1.4\nMock geology introduction.\n%%EOF\n",
            "file-geology-homework": b"%PDF-1.4\nMock slope assignment requirements.\n%%EOF\n",
            "file-survey-template": b"PK\x03\x04 Mock DOCX package for testing only.\n",
        }
        self._files = [
            replace(item, size=len(self._payloads[item.id])) for item in self._files
        ]
        if include_login_html:
            html = b"<!doctype html><html><head><title>SSO Login</title></head><body>login</body></html>"
            self._files.append(
                CourseFile(
                    id="file-login-html",
                    course_id="course-geology",
                    course_name="工程地质",
                    name="伪装下载结果.pdf",
                    relative_path="错误/伪装下载结果.pdf",
                    size=len(html),
                    mime_type="application/pdf",
                )
            )
            self._payloads["file-login-html"] = html

    def get_login_status(self) -> LoginStatus:
        return LoginStatus(
            authenticated=True,
            provider=self.name,
            account="mock-user",
            message="当前使用内置 mock 数据，不代表真实网络学堂登录状态。",
        )

    def login(
        self,
        *,
        start_url: str | None = None,
        timeout_seconds: int = 300,
        persist: bool = True,
    ) -> dict[str, object]:
        raise ProviderNotConfigured("mock provider 不执行真实浏览器登录。")

    def list_courses(self, semester: str | None = None, query: str | None = None) -> list[Course]:
        result = self._courses
        if semester:
            result = [course for course in result if course.semester == semester]
        if query:
            needle = query.casefold()
            result = [course for course in result if needle in course.name.casefold()]
        return list(result)

    def list_assignments(self, course_id: str | None = None) -> list[Assignment]:
        if course_id is None:
            return list(self._assignments)
        return [item for item in self._assignments if item.course_id == course_id]

    def get_assignment(self, assignment_id: str) -> Assignment:
        for item in self._assignments:
            if item.id == assignment_id:
                return item
        raise ValidationError(f"未找到作业：{assignment_id}")

    def list_course_files(
        self, course_id: str | None = None, query: str | None = None
    ) -> list[CourseFile]:
        result = self._files
        if course_id:
            result = [item for item in result if item.course_id == course_id]
        if query:
            needle = query.casefold()
            result = [
                item
                for item in result
                if needle in item.name.casefold() or needle in item.relative_path.casefold()
            ]
        return list(result)

    def get_course_file(self, file_id: str) -> CourseFile:
        for item in self._files:
            if item.id == file_id:
                return item
        raise ValidationError(f"未找到课程文件：{file_id}")

    def download_file(self, file_id: str) -> bytes:
        self.get_course_file(file_id)
        try:
            return self._payloads[file_id]
        except KeyError as exc:
            raise ValidationError(f"mock 文件没有内容：{file_id}") from exc
