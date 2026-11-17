"""Read-only adapter from the existing Learn interface to resource observations.

This module calls only existing read methods: ``list_courses``,
``list_course_files``, ``query_learning``, ``notice_detail``, ``list_assignments``
and ``get_assignment``.  It never calls ``download_file``; a download only
happens after an explicit plan and confirmation.

The real ``LearnReadProvider`` resets ``read_evidence`` at the start of every
operation and records a partial source failure there without throwing.  This
adapter therefore captures and aggregates evidence after *each* call, not once
at the end.  A metadata-only refresh (details not requested) is not reported as
a complete inventory: it marks the detail range as unread so the index never
turns "not read" into "source says it no longer exists".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..source_state import ERROR_CODES, now_iso
from .errors import (
    ResourceAuthRequired,
    ResourceDownloadFailed,
    ResourceError,
    ResourceForbidden,
    ResourceInvalidArgument,
    require_bool,
)
from .identity import stable_uid, validate_identifier
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import ResourceCandidate

TZ = timezone(timedelta(hours=8))
ADAPTER_VERSION = "resource-source-v2"
_AUTH_NAMES = {"AuthenticationRequired", "AuthRequired", "ProviderAuthRequired"}
_PERMISSION_NAMES = {"Forbidden", "PermissionDenied", "ProviderForbidden"}


def _truncate(value: Any, limit: int) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _course_name(course: Any) -> str:
    name = getattr(course, "name", None)
    if isinstance(name, str) and name:
        return name[:240]
    return str(getattr(course, "id", "course"))[:240]


def _course_id(course: Any) -> str:
    value = getattr(course, "id", None)
    if not isinstance(value, str) or not value:
        raise ResourceInvalidArgument("course_id")
    return value


def _code(error: BaseException) -> str:
    """Map an exception to a frozen code; never return a raw class name."""
    code = getattr(error, "code", None)
    if code in ("AUTH_REQUIRED", "FORBIDDEN"):
        return code
    if isinstance(error, PermissionError):
        return "FORBIDDEN"
    name = type(error).__name__
    if name in _AUTH_NAMES:
        return "AUTH_REQUIRED"
    if name in _PERMISSION_NAMES:
        return "FORBIDDEN"
    if isinstance(code, str) and code in ERROR_CODES:
        return code
    return "SOURCE_ERROR"


def _raise_mapped(error: BaseException) -> None:
    code = _code(error)
    if code == "AUTH_REQUIRED":
        raise ResourceAuthRequired("session") from None
    if code == "FORBIDDEN":
        raise ResourceForbidden("source") from None
    raise ResourceDownloadFailed(code) from None


class _Evidence:
    """Aggregate per-call provider evidence without trusting the last reset."""

    def __init__(self) -> None:
        self.complete = True
        self.warnings: list[str] = []
        self.errors = 0
        self.pages = 0
        self.adapter_version: str | None = None
        self.seen = False

    def capture(self, provider: Any, *, course_id: str | None = None) -> bool:
        evidence = getattr(provider, "read_evidence", None)
        if not isinstance(evidence, dict):
            return True
        self.seen = True
        self.adapter_version = evidence.get("adapter_version") or self.adapter_version
        self.pages += len(evidence.get("pages") or ())
        for item in evidence.get("warnings") or ():
            if isinstance(item, str) and item not in self.warnings:
                self.warnings.append(item)
        errors = evidence.get("errors") or ()
        self.errors += len(errors)
        if evidence.get("complete") is False or errors:
            self.complete = False
            return False
        return True

    def summary(self) -> dict[str, Any] | None:
        if not self.seen:
            return None
        return {
            "complete": self.complete,
            "warnings": self.warnings[:50],
            "errors": self.errors,
            "pages": self.pages,
            "adapter_version": self.adapter_version,
        }


class LearnDownloader:
    """The only download port; wraps the existing course file interface."""

    def __init__(self, provider: Any, *, limits: ResourceLimits = DEFAULT_LIMITS) -> None:
        if provider is None or not hasattr(provider, "download_file"):
            raise ResourceInvalidArgument("provider")
        self.provider = provider
        self.limits = limits
        self.reads = 0

    def fetch(self, entry: Any) -> bytes:
        self.reads += 1
        try:
            payload = self.provider.download_file(entry.source_record_id)
        except ResourceError:
            raise
        except Exception as error:
            _raise_mapped(error)
        if isinstance(payload, bytearray):
            payload = bytes(payload)
        if not isinstance(payload, bytes):
            raise ResourceDownloadFailed("fetch_type")
        if len(payload) > self.limits.max_attachment_bytes:
            raise ResourceDownloadFailed("attachment_too_large")
        return payload


def collect(
    provider: Any,
    *,
    semester: str,
    course_id: str | None = None,
    include_notice_details: bool = False,
    include_assignment_details: bool = False,
    limits: ResourceLimits = DEFAULT_LIMITS,
    clock: Callable[[], datetime] = lambda: datetime.now(TZ),
    synthetic: bool = False,
) -> dict[str, Any]:
    """Read metadata (and explicitly requested bodies) without downloading."""
    if provider is None:
        raise ResourceInvalidArgument("provider")
    require_bool(include_notice_details, "include_notice_details")
    require_bool(include_assignment_details, "include_assignment_details")
    require_bool(synthetic, "synthetic")
    if course_id is not None:
        validate_identifier(course_id, name="course_id")
    checked_at = now_iso(clock)
    candidates: list[ResourceCandidate] = []
    errors: list[str] = []
    warnings: set[str] = set()
    failed_courses: set[str] = set()
    evidence = _Evidence()
    counts = {
        "courses": 0,
        "course_files": 0,
        "notices": 0,
        "notice_details": 0,
        "assignments": 0,
        "assignment_details": 0,
        "attachments": 0,
    }
    detail_reads = 0
    full_semester = course_id is None
    truncated = False
    details_incomplete = False
    notice_hint_seen = False
    assignment_hint_seen = False

    try:
        courses = list(provider.list_courses(semester=semester))
    except Exception as error:
        _raise_mapped(error)
    if not evidence.capture(provider):
        errors.append("SOURCE_READ_INCOMPLETE")
    if course_id is not None:
        courses = [course for course in courses if _course_id(course) == course_id]
        if not courses:
            raise ResourceInvalidArgument("course_not_found")
    if len(courses) > limits.max_courses_per_read:
        courses = courses[: limits.max_courses_per_read]
        warnings.add("COURSE_LIMIT_REACHED")
        full_semester = False
        truncated = True
    counts["courses"] = len(courses)

    for course in courses:
        cid = _course_id(course)
        cname = _course_name(course)
        try:
            files = list(provider.list_course_files(course_id=cid))
        except Exception as error:
            errors.append(_code(error))
            failed_courses.add(cid)
            files = []
        if not evidence.capture(provider, course_id=cid):
            errors.append("SOURCE_READ_INCOMPLETE")
            failed_courses.add(cid)
        for item in files:
            try:
                candidates.append(
                    ResourceCandidate(
                        stable_resource_uid=stable_uid("COURSE_FILE", item.id),
                        source="COURSE_FILE",
                        course_id=cid,
                        course_name=cname,
                        semester=semester,
                        title=str(getattr(item, "name", "") or ""),
                        size_bytes=getattr(item, "size", None),
                        updated_at=getattr(item, "uploaded_at", None),
                        body_state="NOT_READ",
                        attachment_hint=None,
                        mime_type=getattr(item, "mime_type", None),
                        synthetic=synthetic,
                        max_title_chars=limits.max_title_chars,
                        max_body_chars=limits.max_body_chars,
                    )
                )
                counts["course_files"] += 1
            except ResourceError:
                errors.append("COURSE_FILE_METADATA_SKIPPED")
        try:
            notices = provider.query_learning(
                "notices", {"semester": semester, "course_id": cid}
            )
            rows = notices.get("items") if isinstance(notices, dict) else None
            if not isinstance(rows, list):
                errors.append("NOTICES_SHAPE")
                rows = []
            if isinstance(notices, dict):
                warnings.update(
                    str(item) for item in notices.get("warnings") or () if isinstance(item, str)
                )
                if notices.get("complete") is False:
                    errors.append("SOURCE_READ_INCOMPLETE")
                    failed_courses.add(cid)
        except Exception as error:
            errors.append(_code(error))
            failed_courses.add(cid)
            rows = []
        if not evidence.capture(provider, course_id=cid):
            errors.append("SOURCE_READ_INCOMPLETE")
            failed_courses.add(cid)
        counts["notices"] += len(rows)
        for notice in rows:
            if not isinstance(notice, dict):
                continue
            notice_id = notice.get("id")
            if not isinstance(notice_id, str):
                continue
            hint = bool(notice.get("attachment_hint"))
            notice_hint_seen = notice_hint_seen or hint
            body_state = "READ" if notice.get("body_state") == "READ" else "NOT_READ"
            body = notice.get("content_text") if body_state == "READ" else None
            attachments: list[dict[str, Any]] = []
            if include_notice_details and hint:
                if detail_reads >= limits.max_detail_reads:
                    warnings.add("DETAIL_READ_LIMIT_REACHED")
                    details_incomplete = True
                    failed_courses.add(cid)
                else:
                    detail_reads += 1
                    try:
                        # The real provider takes a Course object and reads
                        # course.id/course.name; passing a string breaks it.
                        detail = provider.notice_detail(course, notice_id)
                        counts["notice_details"] += 1
                        if isinstance(detail, dict):
                            body_state = (
                                "READ" if detail.get("body_state") == "READ" else body_state
                            )
                            body = detail.get("content_text") or body
                            attachments = [
                                row for row in detail.get("attachments") or () if isinstance(row, dict)
                            ]
                            state = detail.get("attachments_state")
                            if state is not None and state not in ("LIST_READ", "READ"):
                                errors.append("NOTICE_ATTACHMENT_UNRESOLVED")
                                failed_courses.add(cid)
                            elif not attachments:
                                errors.append("NOTICE_ATTACHMENT_UNRESOLVED")
                                failed_courses.add(cid)
                    except Exception as error:
                        errors.append(_code(error))
                        failed_courses.add(cid)
                        warnings.add("NOTICE_ATTACHMENT_UNRESOLVED")
                    if not evidence.capture(provider, course_id=cid):
                        errors.append("SOURCE_READ_INCOMPLETE")
                        failed_courses.add(cid)
            excerpt, body_truncated = _truncate(body, limits.max_body_chars)
            for attachment in attachments:
                file_id = attachment.get("file_id")
                name = attachment.get("name")
                if not isinstance(file_id, str) or not isinstance(name, str):
                    errors.append("NOTICE_ATTACHMENT_METADATA_SKIPPED")
                    failed_courses.add(cid)
                    continue
                try:
                    candidates.append(
                        ResourceCandidate(
                            stable_resource_uid=stable_uid("NOTICE_ATTACHMENT", file_id),
                            source="NOTICE_ATTACHMENT",
                            course_id=cid,
                            course_name=cname,
                            semester=semester,
                            title=name,
                            size_bytes=None,
                            updated_at=None,
                            body_state=body_state,
                            body_excerpt=excerpt,
                            body_truncated=body_truncated and body_state == "READ",
                            attachment_hint=True,
                            synthetic=synthetic,
                            max_title_chars=limits.max_title_chars,
                            max_body_chars=limits.max_body_chars,
                        )
                    )
                    counts["attachments"] += 1
                except ResourceError:
                    errors.append("NOTICE_ATTACHMENT_METADATA_SKIPPED")
                    failed_courses.add(cid)
        try:
            assignments = list(provider.list_assignments(course_id=cid))
        except Exception as error:
            errors.append(_code(error))
            failed_courses.add(cid)
            assignments = []
        if not evidence.capture(provider, course_id=cid):
            errors.append("SOURCE_READ_INCOMPLETE")
            failed_courses.add(cid)
        counts["assignments"] += len(assignments)
        for assignment in assignments:
            hint = getattr(assignment, "attachments_hint", None)
            assignment_hint_seen = assignment_hint_seen or hint is True
            if not include_assignment_details or hint is not True:
                continue
            if detail_reads >= limits.max_detail_reads:
                warnings.add("DETAIL_READ_LIMIT_REACHED")
                details_incomplete = True
                failed_courses.add(cid)
                break
            detail_reads += 1
            try:
                detail = provider.get_assignment(getattr(assignment, "id", ""))
                counts["assignment_details"] += 1
            except Exception as error:
                errors.append(_code(error))
                failed_courses.add(cid)
                warnings.add("ASSIGNMENT_ATTACHMENT_UNRESOLVED")
                continue
            if not evidence.capture(provider, course_id=cid):
                errors.append("SOURCE_READ_INCOMPLETE")
                failed_courses.add(cid)
            attachments = getattr(detail, "attachments", ()) or ()
            state = getattr(detail, "attachments_state", None)
            if state is not None and state not in ("LIST_READ", "READ"):
                errors.append("ASSIGNMENT_ATTACHMENT_UNRESOLVED")
                failed_courses.add(cid)
            elif not attachments:
                errors.append("ASSIGNMENT_ATTACHMENT_UNRESOLVED")
                failed_courses.add(cid)
            body_state = "READ" if getattr(detail, "instructions_state", None) == "READ" else "NOT_READ"
            body = getattr(detail, "instructions", None) if body_state == "READ" else None
            excerpt, body_truncated = _truncate(body, limits.max_body_chars)
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                file_id = attachment.get("file_id")
                name = attachment.get("name")
                if not isinstance(file_id, str) or not isinstance(name, str):
                    errors.append("ASSIGNMENT_ATTACHMENT_METADATA_SKIPPED")
                    failed_courses.add(cid)
                    continue
                try:
                    candidates.append(
                        ResourceCandidate(
                            stable_resource_uid=stable_uid("ASSIGNMENT_ATTACHMENT", file_id),
                            source="ASSIGNMENT_ATTACHMENT",
                            course_id=cid,
                            course_name=cname,
                            semester=semester,
                            title=name,
                            size_bytes=None,
                            updated_at=None,
                            body_state=body_state,
                            body_excerpt=excerpt,
                            body_truncated=body_truncated and body_state == "READ",
                            attachment_hint=True,
                            synthetic=synthetic,
                            max_title_chars=limits.max_title_chars,
                            max_body_chars=limits.max_body_chars,
                        )
                    )
                    counts["attachments"] += 1
                except ResourceError:
                    errors.append("ASSIGNMENT_ATTACHMENT_METADATA_SKIPPED")
                    failed_courses.add(cid)
    if not include_notice_details:
        warnings.add("NOTICE_DETAILS_NOT_READ")
        if notice_hint_seen:
            errors.append("NOTICE_DETAILS_NOT_READ")
    if not include_assignment_details:
        warnings.add("ASSIGNMENT_DETAILS_NOT_READ")
        if assignment_hint_seen:
            errors.append("ASSIGNMENT_DETAILS_NOT_READ")
    complete = not errors and not truncated and not details_incomplete
    return {
        "adapter_version": ADAPTER_VERSION,
        "checked_at": checked_at,
        "semester": semester,
        "course_id": course_id,
        "full_semester": full_semester,
        "complete": complete,
        "source_errors": sorted(set(errors)),
        "warnings": sorted(warnings),
        "stale_courses": sorted(failed_courses),
        "counts": counts,
        "candidates": candidates,
        "read_evidence": evidence.summary(),
        "downloads": 0,
    }


__all__ = ["ADAPTER_VERSION", "LearnDownloader", "collect"]
