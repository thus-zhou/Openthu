"""Synthetic fixtures for local tests; never imported by a production path.

The provider below implements only the read methods the adapter uses.  Its
``download_file`` raises, so a test that tries to download during indexing fails
loudly.  ``SyntheticFetcher`` serves deterministic bytes for an explicit plan.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Iterable

from thu_assistant.resources.identity import stable_uid
from thu_assistant.resources.limits import DEFAULT_LIMITS
from thu_assistant.resources.model import ResourceCandidate

TZ = timezone(timedelta(hours=8))


def pdf_bytes(label: str) -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
        + ("%% " + label + "\n").encode("utf-8")
    )


def candidate(
    *,
    source: str = "COURSE_FILE",
    record_id: str,
    course_id: str = "course-a",
    course_name: str = "合成课程 A",
    semester: str = "2026-2027-1",
    title: str = "合成资料.pdf",
    size_bytes: int | None = 128,
    updated_at: str | None = "2026-09-01T10:00:00+08:00",
    body_state: str = "NOT_READ",
    body_excerpt: str | None = None,
    body_truncated: bool = False,
    mime_type: str | None = None,
    limits: Any = DEFAULT_LIMITS,
) -> ResourceCandidate:
    if body_state == "READ" and body_excerpt is None:
        body_excerpt = "合成公告正文，仅用于本地测试。"
    return ResourceCandidate(
        stable_resource_uid=stable_uid(source, record_id),
        source=source,
        course_id=course_id,
        course_name=course_name,
        semester=semester,
        title=title,
        size_bytes=size_bytes,
        updated_at=updated_at,
        body_state=body_state,
        body_excerpt=body_excerpt,
        body_truncated=body_truncated,
        mime_type=mime_type,
        attachment_hint=source != "COURSE_FILE",
        synthetic=True,
        max_title_chars=limits.max_title_chars,
        max_body_chars=limits.max_body_chars,
    )


@dataclass(frozen=True)
class FixtureCourse:
    id: str
    name: str
    semester: str = "2026-2027-1"


@dataclass(frozen=True)
class FixtureFile:
    id: str
    name: str
    size: int = 128
    uploaded_at: str | None = "2026-09-01T10:00:00+08:00"
    mime_type: str = "application/pdf"


@dataclass(frozen=True)
class FixtureNotice:
    id: str
    title: str
    content_text: str | None
    attachment_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixtureAssignment:
    id: str
    title: str
    instructions: str | None
    attachment_ids: tuple[str, ...] = ()
    instructions_state: str = "READ"
    attachments_hint: bool | None = None
    attachments_state: str = "LIST_READ"

    def __post_init__(self) -> None:
        if self.attachments_hint is None:
            object.__setattr__(self, "attachments_hint", bool(self.attachment_ids))

    @property
    def attachments(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "file_id": f"attachment:assignment:{self.id}:{fid}",
                "name": f"作业附件 {fid}.pdf",
            }
            for fid in self.attachment_ids
        )


class FixtureProvider:
    """A minimal stand-in for the existing Learn read interface."""

    def __init__(
        self,
        *,
        courses: Iterable[FixtureCourse] = (),
        files: dict[str, list[FixtureFile]] | None = None,
        notices: dict[str, list[FixtureNotice]] | None = None,
        assignments: dict[str, list[FixtureAssignment]] | None = None,
        fail_courses: Iterable[str] = (),
        fail_notices: Iterable[str] = (),
        read_complete: bool = True,
    ) -> None:
        self.courses = list(courses)
        self.files = files or {}
        self.notices = notices or {}
        self.assignments = assignments or {}
        self.fail_courses = set(fail_courses)
        self.fail_notices = set(fail_notices)
        self.read_evidence = {
            "complete": read_complete,
            "warnings": [] if read_complete else ["PARTIAL_SOURCE_FAILURE"],
            "errors": [] if read_complete else [{"scope": "synthetic"}],
            "pages": [],
            "adapter_version": "synthetic-read-v1",
        }
        self.read_calls = 0
        self.download_calls = 0

    def list_courses(self, semester: str | None = None, query: str | None = None) -> list[FixtureCourse]:
        self.read_calls += 1
        rows = [course for course in self.courses if semester is None or course.semester == semester]
        if query:
            rows = [course for course in rows if query.casefold() in course.name.casefold()]
        return rows

    def list_course_files(self, course_id: str | None = None, query: str | None = None) -> list[FixtureFile]:
        self.read_calls += 1
        if course_id in self.fail_courses:
            raise RuntimeError("SYNTHETIC_FILES_FAILED")
        rows = list(self.files.get(course_id or "", ()))
        if query:
            rows = [item for item in rows if query.casefold() in item.name.casefold()]
        return rows

    def query_learning(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if operation != "notices":
            raise AssertionError(f"unexpected operation {operation}")
        self.read_calls += 1
        course_id = parameters.get("course_id")
        if course_id in self.fail_notices:
            raise RuntimeError("SYNTHETIC_NOTICES_FAILED")
        items = []
        for notice in self.notices.get(course_id or "", ()):
            items.append(
                {
                    "id": notice.id,
                    "course_id": course_id,
                    "title": notice.title,
                    "content_text": notice.content_text,
                    "body_state": "READ" if notice.content_text is not None else "UNKNOWN",
                    "attachment_hint": bool(notice.attachment_ids),
                    "attachments": [],
                }
            )
        return {
            "items": items,
            "complete": True,
            "warnings": [],
            "errors": [],
            "checked_at": "2026-09-02T09:00:00+08:00",
        }

    def notice_detail(self, course: Any, notice_id: str) -> dict[str, Any]:
        self.read_calls += 1
        course_id = getattr(course, "id", course)
        for notice in self.notices.get(course_id, ()):
            if notice.id != notice_id:
                continue
            return {
                "id": notice.id,
                "course_id": course_id,
                "title": notice.title,
                "content_text": notice.content_text,
                "body_state": "READ" if notice.content_text is not None else "UNKNOWN",
                "attachments": [
                    {
                        "file_id": f"attachment:notice:{course_id}:{notice_id}:{fid}",
                        "name": f"公告附件 {fid}.pdf",
                        "remote_file_id": fid,
                    }
                    for fid in notice.attachment_ids
                ],
                "attachments_state": "LIST_READ" if notice.attachment_ids else "READ",
            }
        raise RuntimeError("SYNTHETIC_NOTICE_NOT_FOUND")

    def list_assignments(self, course_id: str | None = None) -> list[FixtureAssignment]:
        self.read_calls += 1
        return list(self.assignments.get(course_id or "", ()))

    def get_assignment(self, assignment_id: str) -> FixtureAssignment:
        self.read_calls += 1
        for rows in self.assignments.values():
            for item in rows:
                if item.id == assignment_id:
                    return item
        raise RuntimeError("SYNTHETIC_ASSIGNMENT_NOT_FOUND")

    def download_file(self, file_id: str) -> bytes:
        self.download_calls += 1
        raise AssertionError("indexing must never download")


class SyntheticFetcher:
    """Deterministic bytes for explicit plan execution."""

    def __init__(
        self,
        payloads: dict[str, bytes] | None = None,
        *,
        fail_ids: Iterable[str] = (),
        default: bytes | None = None,
    ) -> None:
        self.payloads = dict(payloads or {})
        self.fail_ids = set(fail_ids)
        self.default = default
        self.calls: list[str] = []

    def fetch(self, entry: Any) -> bytes:
        self.calls.append(entry.source_record_id)
        if entry.source_record_id in self.fail_ids:
            raise RuntimeError("SYNTHETIC_TRANSFER_FAILED")
        if entry.source_record_id in self.payloads:
            return self.payloads[entry.source_record_id]
        if self.default is not None:
            return self.default
        return pdf_bytes(entry.title)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "FixtureAssignment",
    "FixtureCourse",
    "FixtureFile",
    "FixtureNotice",
    "FixtureProvider",
    "SyntheticFetcher",
    "candidate",
    "pdf_bytes",
    "sha256",
]
