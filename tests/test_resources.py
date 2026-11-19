"""Synthetic acceptance tests for module 4.2 (course resource centre).

Every account, course, notice, attachment and file is synthetic and generated
locally.  No test contacts a campus service, a model endpoint or the internet.
The suite asserts the behaviours the module promises:

* indexing never downloads and never uploads;
* identity is the source record, not the title;
* same title from different courses keeps two identities and two archive paths;
* versions are explicit, and content that changes is never silently overwritten;
* search reports body coverage instead of claiming to have parsed every file;
* a batch keeps successful items when one item fails;
* unsafe titles cannot escape the chosen directory or use a device name;
* another account cannot read the index, references or download records;
* exports and errors carry no unselected body and no private path.
"""
from __future__ import annotations

import ast
import errno
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from thu_assistant.secure_store import SecureStore, create_key_file, load_provider
from thu_assistant.resources import (
    DEFAULT_LIMITS,
    DownloadRecord,
    ResourceCenter,
    ResourceLimits,
    empty_index,
    stable_uid,
)
from thu_assistant.resources import archive as archive_module
import synthetic_resources as synthetic
from thu_assistant.resources.archive import Archive
from thu_assistant.resources.errors import (
    ERROR_CLASSES,
    ResourceAuthRequired,
    ResourceConfirmationRequired,
    ResourceDiskFull,
    ResourceExportEmpty,
    ResourceExportExists,
    ResourceExportInRepository,
    ResourceHashConflict,
    ResourceIndexCorrupt,
    ResourceInvalidArgument,
    ResourceIndexIncomplete,
    ResourceLimitExceeded,
    ResourceNotFound,
    ResourcePlanChanged,
    ResourcePlanExpired,
    ResourceTitleUnsafe,
    ResourceVersionConflict,
)
from thu_assistant.resources.index import ResourceIndex
from thu_assistant.resources.manifest import build_manifest
from thu_assistant.resources.model import ResourceEntry as Entry

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "thu_assistant" / "resources"
SEMESTER = "2026-2027-1"
OTHER_SEMESTER = "2025-2026-2"
CHECKED = "2026-09-02T09:00:00+08:00"
UPDATED = "2026-09-01T10:00:00+08:00"


def key_backend() -> str:
    return "windows-dpapi" if os.name == "nt" else "file-key"


@pytest.fixture()
def secure(tmp_path: Path):
    key_path = tmp_path / "keys" / "master.key"
    create_key_file(key_path, key_backend())
    provider = load_provider(key_path)
    store = SecureStore(provider, tmp_path / "data")
    return key_path, provider, store, tmp_path


@pytest.fixture()
def clock():
    class Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone(timedelta(hours=8)))

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: int) -> None:
            self.now = self.now + timedelta(seconds=seconds)

    return Clock()


@pytest.fixture()
def centre(secure, clock):
    _, _, store, _ = secure
    return ResourceCenter(store, "synthetic-student", clock=clock)


def observation(
    candidates,
    *,
    semester: str = SEMESTER,
    checked_at: str = CHECKED,
    complete: bool = True,
    full_semester: bool = True,
    course_id: str | None = None,
    stale_courses=(),
    source_errors=(),
    warnings=(),
):
    return {
        "adapter_version": "test",
        "checked_at": checked_at,
        "semester": semester,
        "course_id": course_id,
        "full_semester": full_semester,
        "complete": complete,
        "stale_courses": list(stale_courses),
        "source_errors": list(source_errors),
        "warnings": list(warnings),
        "candidates": list(candidates),
    }


def add(centre, candidates, **kwargs):
    return centre.ingest(observation(candidates, **kwargs))


def download(centre, uid, destination, **kwargs):
    fetcher = kwargs.pop("fetcher", None) or synthetic.SyntheticFetcher()
    plan = centre.preview_download(
        semester=SEMESTER, selections=[uid], destination=str(destination), **kwargs
    )
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=fetcher)
    return plan, result


# -- indexing and source coverage ----------------------------------------


def test_index_build_never_downloads_or_uploads(centre):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf")]},
        notices={"course-a": [synthetic.FixtureNotice("n1", "公告", "公告正文", ("a1",))]},
    )
    result = centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    assert result["downloads"] == 0
    assert provider.download_calls == 0
    assert provider.read_calls > 0
    assert result["merge"]["added"] == 2
    assert {key for key, value in result["coverage"]["by_source"].items() if value} == {
        "COURSE_FILE",
        "NOTICE_ATTACHMENT",
    }


def test_package_imports_no_network_client_and_never_prints():
    banned = {"socket", "urllib", "requests", "http", "httpx", "aiohttp"}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
                assert not names & banned, f"{path.name} imports a network client"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, f"{path.name} imports a network client"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                raise AssertionError(f"{path.name} prints output")


def test_adapter_reads_attachment_metadata_and_body_state(centre):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        notices={
            "course-a": [
                synthetic.FixtureNotice("n1", "实验安排", "第一次实验在三教。", ("a1", "a2"))
            ]
        },
    )
    centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    result = centre.search(semester=SEMESTER, query="三教", source="NOTICE_ATTACHMENT")
    assert result["total"] == 2
    row = result["items"][0]
    assert row["body_state"] == "READ"
    assert "body_excerpt" in row["matched_fields"]
    assert row["stable_resource_uid"].startswith("learn:NOTICE_ATTACHMENT:attachment:notice:")


def test_adapter_reads_assignment_attachments_only_when_requested(centre):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        assignments={
            "course-a": [
                synthetic.FixtureAssignment(
                    "a1", "第一次作业", "请提交实验报告。", ("paper1",)
                )
            ]
        },
    )
    centre.collect(provider, semester=SEMESTER)
    assert centre.search(semester=SEMESTER, source="ASSIGNMENT_ATTACHMENT")["total"] == 0
    centre.collect(
        provider, semester=SEMESTER, include_assignment_details=True
    )
    result = centre.search(semester=SEMESTER, query="实验报告", source="ASSIGNMENT_ATTACHMENT")
    assert result["total"] == 1
    assert result["items"][0]["body_state"] == "READ"
    assert "body_excerpt" in result["items"][0]["matched_fields"]


def test_adapter_reports_course_and_detail_limits_as_incomplete(secure, clock):
    _, _, store, _ = secure
    limits = ResourceLimits(max_courses_per_read=1, max_detail_reads=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    provider = synthetic.FixtureProvider(
        courses=[
            synthetic.FixtureCourse("course-a", "甲"),
            synthetic.FixtureCourse("course-b", "乙"),
        ],
        notices={
            "course-a": [
                synthetic.FixtureNotice("n1", "公告一", "正文", ("a1",)),
                synthetic.FixtureNotice("n2", "公告二", "正文", ("a2",)),
            ]
        },
    )
    result = centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    assert result["complete"] is False
    assert "COURSE_LIMIT_REACHED" in result["warnings"]
    assert "DETAIL_READ_LIMIT_REACHED" in result["warnings"]
    coverage = centre.coverage(SEMESTER)
    assert coverage["complete"] is False


def test_adapter_course_filter_limits_scope_and_does_not_mark_other_courses_absent(centre):
    provider = synthetic.FixtureProvider(
        courses=[
            synthetic.FixtureCourse("course-a", "甲"),
            synthetic.FixtureCourse("course-b", "乙"),
        ],
        files={
            "course-a": [synthetic.FixtureFile("f1", "甲讲义.pdf")],
            "course-b": [synthetic.FixtureFile("f2", "乙讲义.pdf")],
        },
    )
    centre.collect(provider, semester=SEMESTER)
    result = centre.collect(provider, semester=SEMESTER, course_id="course-a")
    assert result["complete"] is True
    assert result["merge"].get("missing", 0) == 0
    assert centre.search(semester=SEMESTER)["total"] == 2


def test_adapter_partial_source_failure_is_not_complete(centre):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf")]},
        fail_notices={"course-a"},
    )
    result = centre.collect(provider, semester=SEMESTER)
    assert result["complete"] is False
    assert result["source_errors"]
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["historical"] is True
    search = centre.search(semester=SEMESTER)
    assert search["status"] == "PARTIAL"
    assert search["complete"] is False


def test_free_text_source_errors_are_not_persisted(centre):
    stats = add(
        centre,
        [synthetic.candidate(record_id="f1", title="讲义.pdf")],
        complete=False,
        source_errors=["/home/someone/private failed"],
        warnings=["token=secret /tmp/file"],
    )
    coverage = centre.coverage(SEMESTER)
    assert stats["source_errors"] == []
    assert coverage["source_errors"] == []
    assert coverage["warnings"] == []
    assert coverage["complete"] is False


def test_search_filters_and_pagination(centre):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="线性代数讲义.pdf", course_id="math"),
            synthetic.candidate(record_id="f2", title="线性代数作业.pdf", course_id="math"),
            synthetic.candidate(record_id="f3", title="物理讲义.pdf", course_id="physics"),
        ],
    )
    assert centre.search(semester=SEMESTER, query="线性代数")["total"] == 2
    assert centre.search(semester=SEMESTER, course_id="physics")["total"] == 1
    assert centre.search(semester=SEMESTER, source="COURSE_FILE")["total"] == 3
    page = centre.search(semester=SEMESTER, limit=2)
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["next_offset"] == 2
    second = centre.search(semester=SEMESTER, limit=2, offset=2)
    assert len(second["items"]) == 1
    assert second["next_offset"] is None
    assert second["snapshot_complete"] is True


def test_body_coverage_is_reported_for_full_text(centre):
    add(
        centre,
        [
            synthetic.candidate(
                source="NOTICE_ATTACHMENT",
                record_id="attachment:notice:c:n1:a1",
                title="附件一.pdf",
                body_state="READ",
                body_excerpt="实验课在三教。",
            ),
            synthetic.candidate(
                source="NOTICE_ATTACHMENT",
                record_id="attachment:notice:c:n2:a2",
                title="附件二.pdf",
                body_state="NOT_READ",
            ),
            synthetic.candidate(record_id="f1", title="课件.pdf"),
        ],
    )
    result = centre.search(semester=SEMESTER, query="三教")
    assert result["total"] == 1
    assert result["body_coverage"]["read"] == 1
    assert result["body_coverage"]["complete"] is False
    assert "BODY_TEXT_COVERAGE_PARTIAL" in result["warnings"]


def test_unknown_update_time_never_overwrites_a_known_version(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)])
    stats = add(
        centre,
        [synthetic.candidate(record_id="f1", title="讲义（更新）.pdf", updated_at=None)],
        checked_at="2026-09-03T09:00:00+08:00",
    )
    assert stats["merge"]["updated"] == 1
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["version"] == UPDATED
    assert entry["updated_at"] == UPDATED
    assert entry["title"] == "讲义（更新）.pdf"
    assert "VERSION_RETAINED_SOURCE_UNRELIABLE" in stats["merge"]["warnings"]


def test_unknown_version_still_indexed_and_searchable(centre):
    add(
        centre,
        [
            synthetic.candidate(
                record_id="attachment:notice:c:n1:a1",
                source="NOTICE_ATTACHMENT",
                title="未知时间附件.pdf",
                updated_at=None,
            )
        ],
    )
    entry = centre.resource(SEMESTER, "learn:NOTICE_ATTACHMENT:attachment:notice:c:n1:a1")["resource"]
    assert entry["version"].startswith("u")
    assert entry["updated_at"] is None
    assert centre.coverage(SEMESTER)["updated_time_unknown"] == 1


# -- identity, versions and manifest ------------------------------------


def test_same_title_different_course_keeps_identity_and_paths(centre, tmp_path):
    payload = synthetic.pdf_bytes("same")
    fetcher = synthetic.SyntheticFetcher(default=payload)
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="同名附件.pdf", course_id="course-a", course_name="甲"),
            synthetic.candidate(record_id="f2", title="同名附件.pdf", course_id="course-b", course_name="乙"),
        ],
    )
    manifest = centre.manifest(SEMESTER)
    assert manifest["count"] == 2
    groups = manifest["same_title_groups"]
    assert len(groups) == 1
    assert len(groups[0]["stable_resource_uids"]) == 2
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(tmp_path / "downloads"),
    )
    paths = {item["stable_resource_uid"]: item["archive_relative_path"] for item in plan["items"]}
    assert paths["learn:FILE:f1"] != paths["learn:FILE:f2"]
    assert paths["learn:FILE:f1"].split("/")[0] != paths["learn:FILE:f2"].split("/")[0]
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=fetcher)
    assert result["counts"]["downloaded"] == 2
    files = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(files) == 2
    assert files[0].parent != files[1].parent


def test_new_source_version_supersedes_and_keeps_old_file(centre, tmp_path):
    fetcher = synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("v1"))
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)])
    _, first = download(centre, "learn:FILE:f1", tmp_path / "downloads", fetcher=fetcher)
    assert first["counts"]["downloaded"] == 1
    old_file = next((tmp_path / "downloads").rglob("*.pdf"))
    old_bytes = old_file.read_bytes()

    add(
        centre,
        [
            synthetic.candidate(
                record_id="f1",
                title="讲义.pdf",
                updated_at="2026-09-05T10:00:00+08:00",
                size_bytes=256,
            )
        ],
        checked_at="2026-09-06T09:00:00+08:00",
    )
    manifest = centre.manifest(SEMESTER)
    versions = manifest["resources"][0]["versions"]
    assert len(versions) == 2
    assert manifest["resources"][0]["active_version"] == "2026-09-05T10:00:00+08:00"
    assert [row["state"] for row in versions].count("SUPERSEDED") == 1
    _, second = download(centre, "learn:FILE:f1", tmp_path / "downloads", fetcher=fetcher)
    assert second["counts"]["downloaded"] == 1
    files = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(files) == 2
    assert old_file.exists()
    assert old_file.read_bytes() == old_bytes


def test_content_change_under_same_version_creates_new_version(centre, tmp_path):
    first_payload = synthetic.pdf_bytes("first")
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads", fetcher=synthetic.SyntheticFetcher(default=first_payload))
    old_files = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(old_files) == 1

    second_payload = synthetic.pdf_bytes("second")
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
        verify_known=True,
    )
    assert plan["items"][0]["action"] == "VERIFY"
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=second_payload)
    )
    assert result["counts"]["downloaded"] == 1
    assert "CONTENT_CHANGED_NEW_VERSION" in result["warnings"]
    files = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(files) == 2
    assert old_files[0].read_bytes() == first_payload
    assert any(path.read_bytes() == second_payload for path in files)
    manifest = centre.manifest(SEMESTER)
    assert len(manifest["resources"][0]["versions"]) == 2
    assert any(
        row["version"].startswith("c:") for row in manifest["resources"][0]["versions"]
    )


def test_verify_can_rebuild_a_lost_index_without_overwriting(centre, tmp_path):
    payload = synthetic.pdf_bytes("kept")
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads", fetcher=synthetic.SyntheticFetcher(default=payload))
    centre.delete_cache(SEMESTER)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
        verify_known=True,
    )
    assert plan["items"][0]["action"] == "VERIFY"
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=payload)
    )
    assert result["counts"]["downloaded"] + result["counts"]["reused"] == 1
    assert len(list((tmp_path / "downloads").rglob("*.pdf"))) == 1


def test_same_content_different_sources_is_grouped_not_merged(centre, tmp_path):
    payload = synthetic.pdf_bytes("identical")
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="甲.pdf", course_id="a"),
            synthetic.candidate(record_id="f2", title="乙.pdf", course_id="b"),
        ],
    )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(tmp_path / "downloads"),
    )
    centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=payload))
    manifest = centre.manifest(SEMESTER)
    assert manifest["count"] == 2
    assert len(manifest["content_duplicate_groups"]) == 1
    group = manifest["content_duplicate_groups"][0]
    assert len(group["resources"]) == 2
    records = centre.download_records(SEMESTER)
    assert records["count"] == 2
    assert any(record["duplicate_of"] for record in records["records"])


def test_manifest_has_stable_identity_shape():
    entry = Entry(
        stable_resource_uid=stable_uid("COURSE_FILE", "SYNTHETIC-RESOURCE-A"),
        source="COURSE_FILE",
        source_record_id="SYNTHETIC-RESOURCE-A",
        course_id="course-a",
        course_name="合成课程",
        semester=SEMESTER,
        title="同名资料",
        version=UPDATED,
        size_bytes=10,
        updated_at=UPDATED,
        body_state="NOT_READ",
        body_excerpt=None,
        attachment_hint=None,
        source_url=None,
        synthetic=True,
        account_scope="acct_" + "a" * 24,
        first_checked_at=CHECKED,
        last_checked_at=CHECKED,
    )
    other = Entry(
        **{
            **{name: getattr(entry, name) for name in entry.__dataclass_fields__},
            "stable_resource_uid": stable_uid("COURSE_FILE", "SYNTHETIC-RESOURCE-B"),
            "source_record_id": "SYNTHETIC-RESOURCE-B",
        }
    )
    manifest = build_manifest([entry, other])
    assert manifest["count"] == 2
    assert len(manifest["same_title_groups"]) == 1


# -- safety and storage --------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "../../escape.pdf",
        "..\\..\\escape.pdf",
        "/absolute/path/escape.pdf",
        "C:\\Temp\\escape.pdf",
        "CON.pdf",
        "LPT1",
        "a" * 300 + ".pdf",
    ],
)
def test_unsafe_titles_cannot_escape_or_use_device_names(centre, tmp_path, title):
    add(centre, [synthetic.candidate(record_id="f1", title=title)])
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
    )
    relative = plan["items"][0]["archive_relative_path"]
    assert not relative.startswith("/")
    assert ".." not in Path(relative).parts
    assert "\\" not in relative
    assert not relative.split("/")[-1].startswith(".")
    base = Path(relative).name.split(" [")[0]
    assert base.upper().split(".")[0] not in {"CON", "LPT1"}
    centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    files = list((tmp_path / "downloads").rglob("*"))
    for path in files:
        if path.is_file():
            path.resolve().relative_to((tmp_path / "downloads").resolve())


def test_title_of_only_dots_is_rejected():
    with pytest.raises(ResourceTitleUnsafe):
        synthetic.candidate(record_id="f1", title="...")


def test_existing_file_is_never_overwritten(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    destination = tmp_path / "downloads"
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(destination)
    )
    target = destination / plan["items"][0]["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user-owned content")
    with pytest.raises(ResourceHashConflict):
        Archive(destination).write(
            centre.index(SEMESTER).find("learn:FILE:f1"), synthetic.pdf_bytes("new")
        )
    assert target.read_bytes() == b"user-owned content"


def test_disk_full_leaves_no_partial_file(monkeypatch, centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    entry = centre.index(SEMESTER).find("learn:FILE:f1")
    archive = Archive(tmp_path / "downloads")

    def fail_write(descriptor, data):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(archive_module.os, "write", fail_write)
    with pytest.raises(ResourceDiskFull):
        archive.write(entry, synthetic.pdf_bytes("full"))
    assert not list((tmp_path / "downloads").rglob("*.pdf"))
    assert not list((tmp_path / "downloads").rglob("*.part"))


def test_full_flow_makes_no_socket_connection(centre, monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("the resource centre must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="讲义.pdf"),
            synthetic.candidate(
                source="NOTICE_ATTACHMENT",
                record_id="attachment:notice:c:n1:a1",
                title="附件.pdf",
                body_state="READ",
                body_excerpt="合成正文",
            ),
        ],
    )
    centre.search(semester=SEMESTER, query="合成")
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    centre.export_summary(SEMESTER, destination=str(tmp_path / "summary.json"))


def test_real_encryption_backend_is_used(secure):
    _, provider, store, _ = secure
    assert provider.backend in ("file-key", "windows-dpapi")
    assert store.health()["backend"] == provider.backend
    assert store.health()["records"] >= 0


def test_index_is_encrypted_and_persisted(secure, clock):
    _, _, store, tmp_path = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="秘密课程讲义.pdf")])
    records = list(store.records_dir.glob("*.secure"))
    assert records
    raw = b"".join(path.read_bytes() for path in records)
    assert "秘密课程讲义".encode("utf-8") not in raw
    assert "learn:FILE:f1".encode("utf-8") not in raw
    reopened = ResourceCenter(store, "synthetic-student", clock=clock)
    assert reopened.search(semester=SEMESTER, query="秘密")["total"] == 1


def test_other_account_cannot_read_index_references_or_records(secure, clock, tmp_path):
    _, _, store, _ = secure
    first = ResourceCenter(store, "synthetic-student", clock=clock)
    add(first, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    second = ResourceCenter(store, "another-student", clock=clock)
    assert second.search(semester=SEMESTER)["total"] == 0
    assert second.download_records(SEMESTER)["count"] == 0
    with pytest.raises(ResourceNotFound):
        second.resource(SEMESTER, "learn:FILE:f1")
    plan = first.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "synthetic")
    )
    with pytest.raises(ResourcePlanExpired):
        second.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())


def test_delete_cache_clears_entries_and_records(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    assert centre.download_records(SEMESTER)["count"] == 1
    result = centre.delete_cache(SEMESTER)
    assert result["deleted"] is True
    assert centre.search(semester=SEMESTER)["total"] == 0
    assert centre.download_records(SEMESTER)["count"] == 0
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    assert centre.search(semester=SEMESTER)["total"] == 1


# -- refresh semantics ---------------------------------------------------


def test_incomplete_refresh_marks_unseen_as_historical(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    add(centre, [], complete=False, stale_courses=["course-a"], source_errors=["SYNTHETIC_PARTIAL"])
    assert centre.search(semester=SEMESTER)["total"] == 0
    historical = centre.search(semester=SEMESTER, allow_historical=True)
    assert historical["total"] == 1
    assert historical["items"][0]["historical"] is True
    assert centre.coverage(SEMESTER)["states"]["STALE"] == 1


def test_stale_index_cannot_be_downloaded_without_explicit_history(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    add(centre, [], complete=False, stale_courses=["course-a"], source_errors=["SYNTHETIC_PARTIAL"])
    with pytest.raises(ResourceIndexIncomplete):
        centre.preview_download(
            semester=SEMESTER,
            selections=["learn:FILE:f1"],
            destination=str(tmp_path / "downloads"),
        )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
        allow_historical=True,
    )
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    assert result["counts"]["downloaded"] == 1
    assert result["items"][0]["source_state"] == "STALE"
    assert result["items"][0]["source_checked_at"] == CHECKED


def test_unknown_time_update_does_not_count_as_a_new_entry_at_the_limit(secure, clock):
    _, _, store, _ = secure
    limits = ResourceLimits(max_entries_per_semester=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)])
    stats = add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=None)])
    assert stats["merge"]["updated"] == 1
    assert stats["coverage"]["entries"] == 1


def test_retention_removes_old_entries_on_reload(secure, clock):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    clock.advance((DEFAULT_LIMITS.retention_days + 1) * 86400)
    reopened = ResourceCenter(store, "synthetic-student", clock=clock)
    assert reopened.search(semester=SEMESTER)["total"] == 0


def test_complete_full_refresh_marks_absent_as_missing(centre):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="甲.pdf"),
            synthetic.candidate(record_id="f2", title="乙.pdf"),
        ],
    )
    add(centre, [synthetic.candidate(record_id="f1", title="甲.pdf")], checked_at="2026-09-03T09:00:00+08:00")
    assert centre.search(semester=SEMESTER)["total"] == 1
    manifest = centre.manifest(SEMESTER)
    missing = [
        row
        for resource in manifest["resources"]
        for row in resource["versions"]
        if row["state"] == "MISSING"
    ]
    assert len(missing) == 1
    assert centre.coverage(SEMESTER)["states"]["MISSING"] == 1


# -- download plans ------------------------------------------------------


def test_plan_requires_confirmation_and_expires(centre, clock, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    with pytest.raises(ResourceConfirmationRequired):
        centre.execute_download(plan["plan_id"], confirm=False)
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    assert result["counts"]["downloaded"] == 1

    second = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    clock.advance(DEFAULT_LIMITS.plan_ttl_seconds + 1)
    with pytest.raises(ResourcePlanExpired):
        centre.execute_download(second["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())


def test_plan_is_invalidated_by_index_change(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    add(
        centre,
        [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at="2026-09-09T10:00:00+08:00")],
        checked_at="2026-09-10T09:00:00+08:00",
    )
    with pytest.raises(ResourcePlanChanged):
        centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())


def test_reuse_does_not_fetch_again(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    assert plan["items"][0]["action"] == "REUSE"

    class FailingFetcher:
        def fetch(self, entry):
            raise AssertionError("reuse must not fetch")

    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=FailingFetcher())
    assert result["counts"]["reused"] == 1
    assert result["counts"]["failed"] == 0


def test_batch_partial_failure_keeps_successes_and_continues(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="一.pdf"),
            synthetic.candidate(record_id="f2", title="二.pdf"),
            synthetic.candidate(record_id="f3", title="三.pdf"),
        ],
    )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2", "learn:FILE:f3"],
        destination=str(tmp_path / "downloads"),
    )
    fetcher = synthetic.SyntheticFetcher(fail_ids=["f2"], default=synthetic.pdf_bytes("ok"))
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=fetcher)
    assert result["counts"] == {"downloaded": 2, "reused": 0, "conflict": 0, "failed": 1, "cancelled": 0}
    assert result["complete"] is False
    assert result["status"] == "PARTIAL"
    assert len(result["items"]) == 3
    assert {row["result"]["status"] for row in result["items"]} == {"DOWNLOADED", "FAILED"}
    files = sorted((tmp_path / "downloads").rglob("*.pdf"))
    assert len(files) == 2
    records = centre.download_records(SEMESTER)
    assert records["count"] == 3
    assert any(row["status"] == "FAILED" and row["error_code"] for row in records["records"])


def test_cancel_stops_remaining_items_without_success_claim(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="一.pdf"),
            synthetic.candidate(record_id="f2", title="二.pdf"),
        ],
    )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(tmp_path / "downloads"),
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(), cancel=lambda: True
    )
    assert result["status"] == "CANCELLED"
    assert result["counts"]["cancelled"] == 2
    assert result["complete"] is False
    assert not list((tmp_path / "downloads").rglob("*.pdf"))


def test_conflict_policy_abort_blocks_plan(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    destination = tmp_path / "downloads"
    preview = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(destination)
    )
    target = destination / preview["items"][0]["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different")
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(destination),
        conflict_policy="ABORT",
    )
    assert plan["blocked"] is True
    with pytest.raises(ResourceHashConflict):
        centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    assert target.read_bytes() == b"different"


def test_conflict_is_reported_and_other_items_continue(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="冲突.pdf"),
            synthetic.candidate(record_id="f2", title="正常.pdf"),
        ],
    )
    destination = tmp_path / "downloads"
    preview = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(destination),
    )
    first = preview["items"][0]
    target = destination / first["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different")
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(destination),
    )
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    assert result["counts"]["conflict"] == 1
    assert result["counts"]["downloaded"] == 1
    assert result["complete"] is False
    assert target.read_bytes() == b"different"


def test_plan_limits_are_enforced(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_plan_items=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="一.pdf"),
            synthetic.candidate(record_id="f2", title="二.pdf"),
        ],
    )
    with pytest.raises(ResourceLimitExceeded):
        centre.preview_download(
            semester=SEMESTER,
            selections=["learn:FILE:f1", "learn:FILE:f2"],
            destination=str(tmp_path / "downloads"),
        )


def test_entry_limit_is_enforced(secure, clock):
    _, _, store, _ = secure
    limits = ResourceLimits(max_entries_per_semester=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    with pytest.raises(ResourceLimitExceeded):
        add(
            centre,
            [
                synthetic.candidate(record_id="f1", title="一.pdf"),
                synthetic.candidate(record_id="f2", title="二.pdf"),
            ],
        )


def test_collect_marks_fixture_candidates_synthetic_and_reports_source_recheck(centre, tmp_path):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf")]},
    )
    centre.collect(provider, semester=SEMESTER, synthetic=True)
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["synthetic"] is True
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher()
    )
    assert result["items"][0]["source_checked_at"] == CHECKED
    assert result["items"][0]["source_state"] == "ACTIVE"


def test_media_registration_uses_course_resource_purpose(centre, tmp_path):
    registered = {}

    class Media:
        def register_downloaded_attachment(self, path, **options):
            registered["path"] = Path(path)
            registered.update(options)
            return type("Reference", (), {"media_id": "media_" + "a" * 24})()

    centre.media_assets = Media()
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    assert registered["purpose"] == "COURSE_RESOURCE"
    assert registered["kind"] == "ATTACHMENT"
    assert registered["account_scope"] == centre.media_scope
    assert registered["service_domain"] == "LEARN"
    assert registered["kind"] == "ATTACHMENT"
    assert registered["retention"] == "EXPLICIT_DELETE"
    assert registered["expires_in"] == DEFAULT_LIMITS.media_reference_ttl_seconds
    assert registered["task_id"].startswith("dl_")
    record = centre.download_records(SEMESTER)["records"][0]
    assert record["media_asset_id"] == "media_" + "a" * 24


def test_download_record_contains_no_absolute_path(secure, clock, tmp_path):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "private-download-root")
    payload = store.read_json(
        centre.index(SEMESTER).record_id,
        purpose="INDEX",
        account_domain="CAMPUS",
        service_domain="LEARN",
        account="synthetic-student",
    )
    raw = json.dumps(payload, ensure_ascii=False)
    assert json.dumps(str(tmp_path / "private-download-root"))[1:-1] not in raw
    assert "archive_relative_path" in raw
    assert payload["downloads"][0]["status"] == "DOWNLOADED"


# -- export --------------------------------------------------------------


def test_export_default_fields_exclude_unselected_courses_and_paths(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="已选讲义.pdf", course_id="a", course_name="甲"),
            synthetic.candidate(record_id="f2", title="未选讲义.pdf", course_id="b", course_name="乙"),
        ],
    )
    destination = tmp_path / "exports" / "summary.json"
    result = centre.export_summary(
        SEMESTER,
        destination=str(destination),
        selections=["learn:FILE:f1"],
    )
    assert result["count"] == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))
    text = destination.read_text(encoding="utf-8")
    assert "未选讲义" not in text
    assert payload["fields"] == [
        "stable_resource_uid",
        "source",
        "course_id",
        "course_name",
        "semester",
        "title",
        "version",
        "size_bytes",
        "updated_at",
        "state",
        "checked_at",
        "source_link",
    ]
    assert "account_scope" not in text
    assert json.dumps(str(tmp_path))[1:-1] not in text
    assert payload["personal_content_included"] is False


def test_export_with_body_requires_acknowledgement(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(
                source="NOTICE_ATTACHMENT",
                record_id="attachment:notice:c:n1:a1",
                title="附件.pdf",
                body_state="READ",
                body_excerpt="个人课程正文摘要。",
            )
        ],
    )
    destination = tmp_path / "exports" / "summary.json"
    with pytest.raises(ResourceConfirmationRequired):
        centre.export_summary(
            SEMESTER, destination=str(destination), include_body=True
        )
    assert not destination.exists()
    result = centre.export_summary(
        SEMESTER,
        destination=str(destination),
        include_body=True,
        acknowledge_personal=True,
    )
    assert result["personal_content_included"] is True
    assert "个人课程正文摘要" in destination.read_text(encoding="utf-8")


def test_export_inside_git_tree_is_refused(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(ResourceExportInRepository):
        centre.export_summary(SEMESTER, destination=str(repository / "summary.json"))


def test_export_empty_selection_raises(centre, tmp_path):
    with pytest.raises(ResourceExportEmpty):
        centre.export_summary(SEMESTER, destination=str(tmp_path / "summary.json"))


# -- errors --------------------------------------------------------------


def test_error_taxonomy_is_complete_and_leaks_nothing():
    contract = json.loads((ROOT / "contracts" / "v0.2" / "error-classification.json").read_text("utf-8"))
    categories = set(contract["categories"])
    codes = set()
    for cls in ERROR_CLASSES:
        if cls.code.startswith("RESOURCE_"):
            assert cls.code not in codes
        else:
            assert cls.code in contract["codes"]
            assert cls.code not in codes
        codes.add(cls.code)
        assert cls.category in categories
        error = cls("/home/someone/private/file.pdf")
        assert error.detail == "DETAIL_REDACTED"
        public = error.as_error()
        assert public["automatic_replay_allowed"] is False
        assert len(public["message"]) <= 200
        assert "/home" not in public["message"]


def test_index_corruption_is_reported_not_ignored(secure, clock):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    record_id = centre.index(SEMESTER).record_id
    store.write_json(
        record_id,
        {"format_version": "broken", "entries": "nope"},
        purpose="INDEX",
        account_domain="CAMPUS",
        service_domain="LEARN",
        account="synthetic-student",
    )
    with pytest.raises(ResourceIndexCorrupt):
        ResourceIndex(
            store,
            account="synthetic-student",
            account_scope=centre.account_scope,
            semester=SEMESTER,
            clock=clock,
        ).load()


def test_empty_index_shape_is_valid():
    payload = empty_index("acct_" + "a" * 24, SEMESTER)
    assert payload["format_version"] == "resource-index-v1"
    assert payload["entries"] == []
    assert payload["downloads"] == []


def test_download_record_rejects_absolute_paths():
    with pytest.raises(ResourceInvalidArgument):
        DownloadRecord(
            download_id="dl_" + "a" * 24,
            stable_resource_uid="learn:FILE:f1",
            version=UPDATED,
            content_sha256="a" * 64,
            byte_size=1,
            archive_relative_path="/home/user/file.pdf",
            destination_id="dst_" + "a" * 12,
            status="DOWNLOADED",
            written_at=CHECKED,
        )


# -- audit P0/P1 regressions ---------------------------------------------


def test_older_known_source_version_does_not_replace_active(centre):
    newer = "2026-09-05T10:00:00+08:00"
    older = "2026-09-01T10:00:00+08:00"
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=newer)])
    stats = add(
        centre,
        [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=older)],
        checked_at="2026-09-07T09:00:00+08:00",
    )
    resource = centre.manifest(SEMESTER)["resources"][0]
    assert resource["active_version"] == newer
    states = {row["version"]: row["state"] for row in resource["versions"]}
    assert states[newer] == "ACTIVE"
    assert states[older] == "SUPERSEDED"
    assert "OLDER_SOURCE_VERSION_IGNORED" in stats["merge"]["warnings"]


def test_content_version_survives_same_base_refresh(centre, tmp_path):
    payload_a = synthetic.pdf_bytes("A")
    payload_b = synthetic.pdf_bytes("B")
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)])
    download(
        centre,
        "learn:FILE:f1",
        tmp_path / "downloads",
        fetcher=synthetic.SyntheticFetcher(default=payload_a),
    )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"),
        verify_known=True,
    )
    centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=payload_b)
    )
    content_version = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]["version"]
    assert content_version.startswith("c:")

    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)],
        checked_at="2026-09-03T09:00:00+08:00")
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["version"] == content_version
    assert entry["state"] == "ACTIVE"
    states = {row["version"]: row["state"] for row in centre.manifest(SEMESTER)["resources"][0]["versions"]}
    assert states[content_version] == "ACTIVE"
    assert states[UPDATED] == "SUPERSEDED"

    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    assert plan["items"][0]["action"] == "REUSE"
    result = centre.execute_download(
        plan["plan_id"],
        confirm=True,
        fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("must-not-fetch")),
    )
    assert result["counts"]["reused"] == 1
    assert any(path.read_bytes() == payload_b for path in (tmp_path / "downloads").rglob("*.pdf"))


def test_newer_source_version_supersedes_content_version(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=UPDATED)])
    download(centre, "learn:FILE:f1", tmp_path / "downloads",
             fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("A")))
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"), verify_known=True,
    )
    centre.execute_download(plan["plan_id"], confirm=True,
                            fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("B")))
    content_version = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]["version"]
    assert content_version.startswith("c:")

    newer = "2026-09-05T10:00:00+08:00"
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", updated_at=newer)],
        checked_at="2026-09-06T09:00:00+08:00")
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["version"] == newer
    assert entry["state"] == "ACTIVE"
    states = {row["version"]: row["state"] for row in centre.manifest(SEMESTER)["resources"][0]["versions"]}
    assert states[content_version] == "SUPERSEDED"


def test_failed_archive_write_does_not_mutate_the_index(centre, tmp_path, monkeypatch):
    payload_a = synthetic.pdf_bytes("A")
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads",
             fetcher=synthetic.SyntheticFetcher(default=payload_a))
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"), verify_known=True,
    )

    def fail_write(self, entry, data):
        raise ResourceDiskFull("no_space")

    monkeypatch.setattr(Archive, "write", fail_write)
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("B"))
    )
    assert result["counts"]["failed"] == 1
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["version"] == UPDATED
    assert entry["content_sha256"] == synthetic.sha256(payload_a)
    assert len(centre.manifest(SEMESTER)["resources"][0]["versions"]) == 1
    files = list((tmp_path / "downloads").rglob("*.pdf"))
    assert len(files) == 1
    assert files[0].read_bytes() == payload_a


def test_existing_user_file_conflict_does_not_poison_index(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    target = tmp_path / "downloads" / plan["items"][0]["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user-owned content")
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"), verify_known=True,
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("remote"))
    )
    assert result["counts"]["failed"] == 1
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["content_sha256"] is None
    assert entry["version"] == UPDATED
    assert target.read_bytes() == b"user-owned content"


def test_historical_content_change_is_refused(centre, tmp_path):
    payload_a = synthetic.pdf_bytes("A")
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads",
             fetcher=synthetic.SyntheticFetcher(default=payload_a))
    add(centre, [], complete=False, stale_courses=["course-a"], source_errors=["SYNTHETIC_PARTIAL"])
    entry = centre.index(SEMESTER).find("learn:FILE:f1", allow_historical=True)
    assert entry.state == "STALE"
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=[{"stable_resource_uid": "learn:FILE:f1", "version": entry.version}],
        destination=str(tmp_path / "downloads"),
        allow_historical=True,
        verify_known=True,
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("B"))
    )
    assert result["counts"]["failed"] == 1
    assert result["items"][0]["result"]["error_code"] == ResourceVersionConflict.code
    assert centre.index(SEMESTER).find("learn:FILE:f1", allow_historical=True).content_sha256 == synthetic.sha256(payload_a)


def test_content_change_at_entry_cap_fails_before_write(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_entries_per_semester=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads",
             fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("A")))
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"), verify_known=True,
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("B"))
    )
    assert result["counts"]["failed"] == 1
    assert result["counts"]["downloaded"] == 0
    assert len(list((tmp_path / "downloads").rglob("*.pdf"))) == 1
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["version"] == UPDATED
    assert entry["content_sha256"] == synthetic.sha256(synthetic.pdf_bytes("A"))


def test_content_change_can_prune_a_superseded_version(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_entries_per_semester=2)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads",
             fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("A")))
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"],
        destination=str(tmp_path / "downloads"), verify_known=True,
    )
    result = centre.execute_download(
        plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher(default=synthetic.pdf_bytes("B"))
    )
    assert result["counts"]["downloaded"] == 1
    versions = centre.manifest(SEMESTER)["resources"][0]["versions"]
    assert len(versions) == 2
    assert any(row["version"].startswith("c:") and row["state"] == "ACTIVE" for row in versions)


def test_metadata_only_refresh_does_not_mark_attachments_missing(centre):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "合成课程 A")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf")]},
        notices={"course-a": [synthetic.FixtureNotice("n1", "公告", "正文", ("a1",))]},
    )
    centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    second = centre.collect(provider, semester=SEMESTER)
    assert second["complete"] is False
    assert "NOTICE_DETAILS_NOT_READ" in second["source_errors"]
    assert centre.coverage(SEMESTER)["states"]["MISSING"] == 0
    assert centre.search(semester=SEMESTER)["total"] == 2


def test_skipped_course_file_metadata_is_incomplete_not_missing(centre):
    good = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "甲")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf")]},
    )
    centre.collect(good, semester=SEMESTER)
    bad = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "甲")],
        files={"course-a": [synthetic.FixtureFile("f1", "讲义.pdf", size="bad")]},
    )
    result = centre.collect(bad, semester=SEMESTER)
    assert result["complete"] is False
    assert "COURSE_FILE_METADATA_SKIPPED" in result["source_errors"]
    assert centre.coverage(SEMESTER)["states"]["ACTIVE"] == 1
    assert centre.coverage(SEMESTER)["states"]["MISSING"] == 0


def test_single_course_failure_does_not_stale_another_course(centre):
    provider = synthetic.FixtureProvider(
        courses=[
            synthetic.FixtureCourse("course-a", "甲"),
            synthetic.FixtureCourse("course-b", "乙"),
        ],
        files={
            "course-a": [synthetic.FixtureFile("f1", "甲.pdf")],
            "course-b": [synthetic.FixtureFile("f2", "乙.pdf")],
        },
        notices={"course-a": [synthetic.FixtureNotice("n1", "公告", "正文", ("a1",))]},
    )
    centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    failing = synthetic.FixtureProvider(
        courses=[
            synthetic.FixtureCourse("course-a", "甲"),
            synthetic.FixtureCourse("course-b", "乙"),
        ],
        files={
            "course-a": [synthetic.FixtureFile("f1", "甲.pdf")],
            "course-b": [synthetic.FixtureFile("f2", "乙.pdf")],
        },
        notices={"course-a": [synthetic.FixtureNotice("n1", "公告", "正文", ("a1",))]},
        fail_notices={"course-a"},
    )
    result = centre.collect(failing, semester=SEMESTER, course_id="course-a")
    assert result["complete"] is False
    states = centre.coverage(SEMESTER)["states"]
    assert states["STALE"] == 1
    assert states["ACTIVE"] == 2
    assert centre.search(semester=SEMESTER, course_id="course-b")["total"] == 1


class _ResetEvidenceProvider:
    """Mimic LearnReadProvider: every operation resets read_evidence."""

    def __init__(self) -> None:
        self.read_evidence = {}

    def _begin(self) -> None:
        self.read_evidence = {
            "complete": True,
            "warnings": [],
            "errors": [],
            "pages": [],
            "adapter_version": "synthetic-reset",
        }

    def list_courses(self, semester=None, query=None):
        self._begin()
        return [synthetic.FixtureCourse("course-a", "甲")]

    def list_course_files(self, course_id=None, query=None):
        self._begin()
        self.read_evidence["complete"] = False
        self.read_evidence["warnings"].append("PARTIAL_SOURCE_FAILURE")
        return []

    def query_learning(self, operation, parameters):
        self._begin()
        return {"items": [], "complete": True, "warnings": [], "errors": [], "pages": []}

    def list_assignments(self, course_id=None):
        self._begin()
        return []


def test_per_operation_read_evidence_is_aggregated(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    result = centre.collect(_ResetEvidenceProvider(), semester=SEMESTER)
    assert result["complete"] is False
    assert "SOURCE_READ_INCOMPLETE" in result["source_errors"]
    states = centre.coverage(SEMESTER)["states"]
    assert states["STALE"] == 1
    assert states["MISSING"] == 0


def test_truncated_body_is_reported_as_partial(centre, tmp_path):
    provider = synthetic.FixtureProvider(
        courses=[synthetic.FixtureCourse("course-a", "甲")],
        notices={
            "course-a": [
                synthetic.FixtureNotice("n1", "公告", "A" * 10 + "NEEDLE", ("a1",))
            ]
        },
    )
    limits = ResourceLimits(max_body_chars=10)
    centre = ResourceCenter(centre.store, "synthetic-student", limits=limits, clock=centre.clock)
    result = centre.collect(provider, semester=SEMESTER, include_notice_details=True)
    assert result["complete"] is True
    search = centre.search(semester=SEMESTER, query="NEEDLE", source="NOTICE_ATTACHMENT")
    assert search["total"] == 0
    assert search["status"] == "PARTIAL"
    assert search["body_coverage"]["truncated"] == 1
    assert search["body_coverage"]["complete"] is False
    assert "BODY_TEXT_COVERAGE_TRUNCATED" in search["warnings"]
    exported = centre.export_summary(
        SEMESTER,
        destination=str(tmp_path / "body.json"),
        fields=["title", "body_excerpt"],
        acknowledge_personal=True,
    )
    assert exported["personal_content_included"] is True
    payload = json.loads((tmp_path / "body.json").read_text(encoding="utf-8"))
    assert payload["rows"][0]["body_truncated"] is True


class _RealSignatureProvider:
    """The real provider expects a Course object for notice_detail."""

    def __init__(self) -> None:
        self.read_evidence = {"complete": True, "warnings": [], "errors": [], "pages": []}

    def list_courses(self, semester=None, query=None):
        return [synthetic.FixtureCourse("course-a", "甲")]

    def list_course_files(self, course_id=None, query=None):
        return []

    def query_learning(self, operation, parameters):
        return {
            "items": [
                {
                    "id": "n1",
                    "title": "公告",
                    "content_text": "正文",
                    "body_state": "READ",
                    "attachment_hint": True,
                }
            ],
            "complete": True,
            "warnings": [],
            "errors": [],
            "pages": [],
        }

    def notice_detail(self, course, notice_id):
        return {
            "id": notice_id,
            "content_text": "正文",
            "body_state": "READ",
            "attachments_state": "LIST_READ",
            "attachments": [
                {"file_id": f"attachment:notice:{course.id}:{notice_id}:a1", "name": "附件.pdf"}
            ],
        }

    def list_assignments(self, course_id=None):
        return []


def test_notice_detail_receives_a_course_object(centre):
    result = centre.collect(_RealSignatureProvider(), semester=SEMESTER, include_notice_details=True)
    assert result["complete"] is True
    assert centre.search(semester=SEMESTER, source="NOTICE_ATTACHMENT")["total"] == 1


def test_long_chinese_title_uses_utf8_byte_budget(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="中" * 100 + ".pdf")])
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    relative = plan["items"][0]["archive_relative_path"]
    assert len(Path(relative).name.encode("utf-8")) <= 255
    assert len(relative.encode("utf-8")) <= 240
    result = centre.execute_download(plan["plan_id"], confirm=True, fetcher=synthetic.SyntheticFetcher())
    assert result["counts"]["downloaded"] == 1


def test_string_booleans_are_rejected(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    with pytest.raises(ResourceInvalidArgument):
        centre.preview_download(
            semester=SEMESTER,
            selections=["learn:FILE:f1"],
            destination=str(tmp_path / "d"),
            allow_historical="false",
        )
    with pytest.raises(ResourceInvalidArgument):
        centre.preview_download(
            semester=SEMESTER,
            selections=["learn:FILE:f1"],
            destination=str(tmp_path / "d"),
            verify_known="false",
        )
    with pytest.raises(ResourceInvalidArgument):
        centre.export_summary(
            SEMESTER,
            destination=str(tmp_path / "summary.json"),
            fields=["title", "body_excerpt"],
            acknowledge_personal="false",
        )
    with pytest.raises(ResourceInvalidArgument):
        centre.search(semester=SEMESTER, include_superseded="false")


def test_stale_entry_requires_allow_historical_for_explicit_version(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    add(centre, [], complete=False, stale_courses=["course-a"], source_errors=["SYNTHETIC_PARTIAL"])
    with pytest.raises(ResourceNotFound):
        centre.index(SEMESTER).find("learn:FILE:f1", version=UPDATED)
    with pytest.raises(ResourceNotFound):
        centre.resource(SEMESTER, "learn:FILE:f1")
    entry = centre.resource(SEMESTER, "learn:FILE:f1", allow_historical=True)["resource"]
    assert entry["state"] == "STALE"


def test_total_known_is_false_when_source_is_incomplete(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    add(centre, [], complete=False, stale_courses=["course-a"], source_errors=["SYNTHETIC_PARTIAL"])
    result = centre.search(semester=SEMESTER, allow_historical=True)
    assert result["total_known"] is False
    assert result["source_complete"] is False
    assert result["status"] == "PARTIAL"


def test_search_manifest_and_download_records_are_self_describing(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    search = centre.search(semester=SEMESTER, query="讲义")
    assert search["format_version"] == "resource-search-v1"
    assert search["semester"] == SEMESTER
    assert search["query"] == "讲义"
    manifest = centre.manifest(SEMESTER, limit=1)
    assert manifest["semester"] == SEMESTER
    assert manifest["limit"] == 1
    assert manifest["offset"] == 0
    assert "next_offset" in manifest
    records = centre.download_records(SEMESTER, limit=1)
    assert records["offset"] == 0
    assert records["limit"] == 1
    assert records["count"] == 1
    assert records["snapshot_complete"] is True


def test_authentication_error_is_not_a_download_failure(centre):
    class AuthenticationRequired(Exception):
        pass

    class Provider:
        def list_courses(self, semester=None, query=None):
            raise AuthenticationRequired("expired")

    with pytest.raises(ResourceAuthRequired):
        centre.collect(Provider(), semester=SEMESTER)


def test_encrypted_payload_that_is_not_json_is_index_corruption(secure, clock):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    record_id = centre.index(SEMESTER).record_id
    store.write(
        record_id,
        b"not json",
        purpose="INDEX",
        account_domain="CAMPUS",
        service_domain="LEARN",
        account="synthetic-student",
    )
    with pytest.raises(ResourceIndexCorrupt):
        ResourceIndex(
            store,
            account="synthetic-student",
            account_scope=centre.account_scope,
            semester=SEMESTER,
            clock=clock,
        ).load()


def test_refresh_preserves_known_size_and_updates_course_name(centre):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", size_bytes=128)])
    add(
        centre,
        [
            synthetic.candidate(
                record_id="f1", title="讲义.pdf", course_name="改名课程", size_bytes=None
            )
        ],
        checked_at="2026-09-03T09:00:00+08:00",
    )
    entry = centre.resource(SEMESTER, "learn:FILE:f1")["resource"]
    assert entry["size_bytes"] == 128
    assert entry["course_name"] == "改名课程"


def test_cross_semester_entry_is_rejected_on_load(secure, clock):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    index = centre.index(SEMESTER)
    payload = index._payload()
    payload["entries"][0]["semester"] = OTHER_SEMESTER
    store.write_json(
        index.record_id,
        payload,
        purpose="INDEX",
        account_domain="CAMPUS",
        service_domain="LEARN",
        account="synthetic-student",
    )
    with pytest.raises(ResourceIndexCorrupt):
        ResourceIndex(
            store,
            account="synthetic-student",
            account_scope=centre.account_scope,
            semester=SEMESTER,
            clock=clock,
        ).load()


def test_error_message_caps_a_long_detail():
    from thu_assistant.resources.errors import ResourceInvalidArgument as Error

    assert len(Error("A" * 120).as_error()["message"]) <= 200
    assert Error("A" * 200).detail == "DETAIL_REDACTED"


def test_reserved_windows_names_include_superscript_and_clock():
    from thu_assistant.resources.identity import safe_component

    assert safe_component("COM¹.pdf", fallback="x", max_length=80).startswith("_")
    assert safe_component("LPT²", fallback="x", max_length=80).startswith("_")
    assert safe_component("CLOCK$", fallback="x", max_length=80).startswith("_")


def test_archive_publish_race_never_overwrites(centre, tmp_path, monkeypatch):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    entry = centre.index(SEMESTER).find("learn:FILE:f1")
    target = Archive(tmp_path / "downloads").target(entry)

    def racing_link(source, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"racer")
        raise FileExistsError

    monkeypatch.setattr(archive_module.os, "link", racing_link)
    with pytest.raises(ResourceHashConflict):
        Archive(tmp_path / "downloads").write(entry, synthetic.pdf_bytes("ours"))
    assert target.read_bytes() == b"racer"


def test_export_refuses_existing_file_and_allows_explicit_overwrite(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    destination = tmp_path / "summary.json"
    destination.write_text("old", encoding="utf-8")
    with pytest.raises(ResourceExportExists):
        centre.export_summary(SEMESTER, destination=str(destination))
    assert destination.read_text(encoding="utf-8") == "old"
    centre.export_summary(SEMESTER, destination=str(destination), overwrite=True)
    assert "rows" in destination.read_text(encoding="utf-8")


def test_delete_cache_failure_keeps_index_and_plans(centre, tmp_path, monkeypatch):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )

    def fail_write(*args, **kwargs):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(centre.store, "write_json", fail_write)
    with pytest.raises(Exception):
        centre.delete_cache(SEMESTER)
    monkeypatch.undo()
    assert centre.search(semester=SEMESTER)["total"] == 1
    assert plan["plan_id"] in centre._plans


def test_pending_plans_are_capped(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_pending_plans=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "d")
    )
    with pytest.raises(ResourceLimitExceeded):
        centre.preview_download(
            semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "d")
        )


def test_preview_hash_budget_avoids_unbounded_reads(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_preview_hash_bytes=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    first = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    target = tmp_path / "downloads" / first["items"][0]["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(synthetic.pdf_bytes("existing"))
    plan = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    assert plan["items"][0]["action"] == "REVIEW_EXISTING"
    assert plan["totals"]["hash_budget_exhausted"] is True


def test_search_and_selections_reject_bad_types(centre, tmp_path):
    with pytest.raises(ResourceInvalidArgument):
        centre.search(semester=SEMESTER, query=123)
    with pytest.raises(ResourceInvalidArgument):
        centre.preview_download(
            semester=SEMESTER, selections=None, destination=str(tmp_path / "downloads")
        )


def test_body_truncated_field_requires_body_export(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(
                record_id="f1",
                title="讲义.pdf",
                body_state="READ",
                body_excerpt="正文",
            )
        ],
    )
    with pytest.raises(ResourceInvalidArgument):
        centre.export_summary(
            SEMESTER,
            destination=str(tmp_path / "summary.json"),
            fields=["body_truncated"],
        )


def test_cancelled_items_are_persisted_as_download_records(centre, tmp_path):
    add(
        centre,
        [
            synthetic.candidate(record_id="f1", title="一.pdf"),
            synthetic.candidate(record_id="f2", title="二.pdf"),
        ],
    )
    plan = centre.preview_download(
        semester=SEMESTER,
        selections=["learn:FILE:f1", "learn:FILE:f2"],
        destination=str(tmp_path / "downloads"),
    )
    result = centre.execute_download(
        plan["plan_id"],
        confirm=True,
        fetcher=synthetic.SyntheticFetcher(),
        cancel=lambda: True,
    )
    assert result["counts"]["cancelled"] == 2
    records = centre.download_records(SEMESTER)
    assert records["count"] == 2
    assert {row["status"] for row in records["records"]} == {"CANCELLED"}


def test_media_registration_uses_media_scope_and_learn_service(centre, tmp_path):
    from thu_assistant.media_assets import MediaAssets

    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    assets = MediaAssets(root)
    centre.media_assets = assets
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    record = centre.download_records(SEMESTER)["records"][0]
    assert record["media_asset_id"]
    reference = assets.resolve(
        record["media_asset_id"],
        account_scope=centre.media_scope,
        task_id=record["download_id"],
        purpose="COURSE_RESOURCE",
    )
    assert reference.kind == "ATTACHMENT"
    assert reference.mime_type == "application/pdf"
    assert reference.purpose == "COURSE_RESOURCE"
    assets.close()


def test_media_kind_uses_image_for_an_image_course_file(centre, tmp_path):
    captured = {}

    class Media:
        def register_downloaded_attachment(self, path, **options):
            captured.update(options)
            return type("Reference", (), {"media_id": "media_" + "a" * 24, "expires_at": None})()

    centre.media_assets = Media()
    add(
        centre,
        [
            synthetic.candidate(
                record_id="f1", title="示意图.png", mime_type="image/png"
            )
        ],
    )
    download(centre, "learn:FILE:f1", tmp_path / "downloads")
    assert captured["kind"] == "IMAGE"


def test_export_reports_source_link_state(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    result = centre.export_summary(SEMESTER, destination=str(tmp_path / "summary.json"))
    assert result["source_link_state"] == "NOT_AVAILABLE_IN_THIS_VERSION"
    payload = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert payload["source_link_state"] == "NOT_AVAILABLE_IN_THIS_VERSION"
    assert payload["rows"][0]["source_link"] is None


def test_download_port_maps_expired_authentication(centre):
    from thu_assistant.resources.sources import LearnDownloader

    class AuthenticationRequired(Exception):
        pass

    class Provider:
        def download_file(self, file_id):
            raise AuthenticationRequired("expired")

    downloader = LearnDownloader(Provider())
    with pytest.raises(ResourceAuthRequired):
        downloader.fetch(type("Entry", (), {"source_record_id": "f1"})())


def test_synthetic_fixtures_are_not_in_the_installed_package():
    assert not (PACKAGE / "synthetic.py").exists()
    assert (ROOT / "tests" / "synthetic_resources.py").is_file()


def test_verify_plan_bytes_count_toward_the_limit(secure, clock, tmp_path):
    _, _, store, _ = secure
    limits = ResourceLimits(max_plan_known_bytes=1)
    centre = ResourceCenter(store, "synthetic-student", limits=limits, clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf", size_bytes=None)])
    first = centre.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(tmp_path / "downloads")
    )
    target = tmp_path / "downloads" / first["items"][0]["archive_relative_path"]
    target.parent.mkdir(parents=True)
    target.write_bytes(synthetic.pdf_bytes("existing"))
    with pytest.raises(ResourceLimitExceeded):
        centre.preview_download(
            semester=SEMESTER,
            selections=["learn:FILE:f1"],
            destination=str(tmp_path / "downloads"),
            verify_known=True,
        )


def test_source_url_is_resanitised_on_load(secure, clock):
    _, _, store, _ = secure
    centre = ResourceCenter(store, "synthetic-student", clock=clock)
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    index = centre.index(SEMESTER)
    payload = index._payload()
    payload["entries"][0]["source_url"] = "https://learn.tsinghua.edu.cn/x?token=SECRET"
    store.write_json(
        index.record_id,
        payload,
        purpose="INDEX",
        account_domain="CAMPUS",
        service_domain="LEARN",
        account="synthetic-student",
    )
    loaded = ResourceIndex(
        store,
        account="synthetic-student",
        account_scope=centre.account_scope,
        semester=SEMESTER,
        clock=clock,
    ).load()
    assert "SECRET" not in (loaded.entries[0].source_url or "")



