"""Read-only adapter from the existing Learn interface to resource observations.

This module calls only existing read methods: ``list_courses``,
``list_course_files``, ``query_learning``, ``notice_detail``, ``list_assignments``
and ``get_assignment``.  It never calls ``download_file``; a download only
happens after an explicit plan and confirmation.

Attachment lists require an explicit detail read because they live on a notice
or assignment page.  Without that flag the index holds metadata only and says
so, instead of pretending the semester was fully catalogued.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..source_state import now_iso
from .errors import ResourceDownloadFailed, ResourceError, ResourceInvalidArgument
from .identity import stable_uid
from .limits import DEFAULT_LIMITS, ResourceLimits
from .model import ResourceCandidate

TZ = timezone(timedelta(hours=8))
ADAPTER_VERSION = "resource-source-v1"


def _truncate(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


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
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__.upper()


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
            raise ResourceDownloadFailed(_code(error)) from None
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
    if course_id is not None and (not isinstance(course_id, str) or not course_id):
        raise ResourceInvalidArgument("course_id")
    checked_at = now_iso(clock)
    candidates: list[ResourceCandidate] = []
    errors: list[str] = []
    warnings: set[str] = set()
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

    try:
        courses = list(provider.list_courses(semester=semester))
    except Exception as error:
        raise ResourceDownloadFailed(_code(error)) from None
    if course_id is not None:
        courses = [course for course in courses if _course_id(course) == course_id]
        if not courses:
            raise ResourceInvalidArgument("course_not_found")
    if len(courses) > limits.max_courses_per_read:
        courses = courses[: limits.max_courses_per_read]
        warnings.add("COURSE_LIMIT_REACHED")
        full_semester = False
    counts["courses"] = len(courses)

    for course in courses:
        cid = _course_id(course)
        cname = _course_name(course)
        try:
            files = list(provider.list_course_files(course_id=cid))
        except Exception as error:
            errors.append(_code(error))
            files = []
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
                        synthetic=synthetic,
                        max_title_chars=limits.max_title_chars,
                        max_body_chars=limits.max_body_chars,
                    )
                )
                counts["course_files"] += 1
            except ResourceError:
                warnings.add("COURSE_FILE_METADATA_SKIPPED")
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
                    errors.append("NOTICES_INCOMPLETE")
        except Exception as error:
            errors.append(_code(error))
            rows = []
        counts["notices"] += len(rows)
        for notice in rows:
            if not isinstance(notice, dict):
                continue
            notice_id = notice.get("id")
            if not isinstance(notice_id, str):
                continue
            body_state = "READ" if notice.get("body_state") == "READ" else "NOT_READ"
            body = notice.get("content_text") if body_state == "READ" else None
            attachments: list[dict[str, Any]] = []
            if include_notice_details and notice.get("attachment_hint"):
                if detail_reads >= limits.max_detail_reads:
                    warnings.add("DETAIL_READ_LIMIT_REACHED")
                else:
                    detail_reads += 1
                    try:
                        detail = provider.notice_detail(cid, notice_id)
                        counts["notice_details"] += 1
                        if isinstance(detail, dict):
                            body_state = (
                                "READ" if detail.get("body_state") == "READ" else body_state
                            )
                            body = detail.get("content_text") or body
                            attachments = [
                                row for row in detail.get("attachments") or () if isinstance(row, dict)
                            ]
                    except Exception as error:
                        errors.append(_code(error))
                        warnings.add("NOTICE_ATTACHMENT_UNRESOLVED")
            for attachment in attachments:
                file_id = attachment.get("file_id")
                name = attachment.get("name")
                if not isinstance(file_id, str) or not isinstance(name, str):
                    warnings.add("NOTICE_ATTACHMENT_METADATA_SKIPPED")
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
                            body_excerpt=_truncate(body, limits.max_body_chars),
                            attachment_hint=True,
                            synthetic=synthetic,
                            max_title_chars=limits.max_title_chars,
                            max_body_chars=limits.max_body_chars,
                        )
                    )
                    counts["attachments"] += 1
                except ResourceError:
                    warnings.add("NOTICE_ATTACHMENT_METADATA_SKIPPED")
        try:
            assignments = list(provider.list_assignments(course_id=cid))
        except Exception as error:
            errors.append(_code(error))
            assignments = []
        counts["assignments"] += len(assignments)
        for assignment in assignments:
            hint = getattr(assignment, "attachments_hint", None)
            if not include_assignment_details or hint is not True:
                continue
            if detail_reads >= limits.max_detail_reads:
                warnings.add("DETAIL_READ_LIMIT_REACHED")
                break
            detail_reads += 1
            try:
                detail = provider.get_assignment(getattr(assignment, "id", ""))
                counts["assignment_details"] += 1
            except Exception as error:
                errors.append(_code(error))
                warnings.add("ASSIGNMENT_ATTACHMENT_UNRESOLVED")
                continue
            attachments = getattr(detail, "attachments", ()) or ()
            body_state = "READ" if getattr(detail, "instructions_state", None) == "READ" else "NOT_READ"
            body = getattr(detail, "instructions", None) if body_state == "READ" else None
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                file_id = attachment.get("file_id")
                name = attachment.get("name")
                if not isinstance(file_id, str) or not isinstance(name, str):
                    warnings.add("ASSIGNMENT_ATTACHMENT_METADATA_SKIPPED")
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
                            body_excerpt=_truncate(body, limits.max_body_chars),
                            attachment_hint=True,
                            synthetic=synthetic,
                            max_title_chars=limits.max_title_chars,
                            max_body_chars=limits.max_body_chars,
                        )
                    )
                    counts["attachments"] += 1
                except ResourceError:
                    warnings.add("ASSIGNMENT_ATTACHMENT_METADATA_SKIPPED")
    if not include_notice_details:
        warnings.add("NOTICE_DETAILS_NOT_READ")
    if not include_assignment_details:
        warnings.add("ASSIGNMENT_DETAILS_NOT_READ")
    evidence = getattr(provider, "read_evidence", None)
    evidence_summary = None
    if isinstance(evidence, dict):
        evidence_summary = {
            "complete": bool(evidence.get("complete")),
            "warnings": [str(item) for item in evidence.get("warnings") or () if isinstance(item, str)][:50],
            "errors": len(evidence.get("errors") or ()),
            "pages": len(evidence.get("pages") or ()),
            "adapter_version": evidence.get("adapter_version"),
        }
        if evidence.get("complete") is False:
            errors.append("SOURCE_READ_INCOMPLETE")
    return {
        "adapter_version": ADAPTER_VERSION,
        "checked_at": checked_at,
        "semester": semester,
        "course_id": course_id,
        "full_semester": full_semester,
        "complete": not errors,
        "source_errors": sorted(set(errors)),
        "warnings": sorted(warnings),
        "counts": counts,
        "candidates": candidates,
        "read_evidence": evidence_summary,
        "downloads": 0,
    }


__all__ = ["ADAPTER_VERSION", "LearnDownloader", "collect"]
