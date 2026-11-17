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
from thu_assistant.resources import synthetic
from thu_assistant.resources.archive import Archive
from thu_assistant.resources.errors import (
    ERROR_CLASSES,
    ResourceConfirmationRequired,
    ResourceDiskFull,
    ResourceExportEmpty,
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
    source_errors=(),
    warnings=(),
):
    return {
        "adapter_version": "test",
        "checked_at": checked_at,
        "semester": semester,
        "full_semester": full_semester,
        "complete": complete,
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


def test_other_account_cannot_read_index_references_or_records(secure, clock):
    _, _, store, _ = secure
    first = ResourceCenter(store, "synthetic-student", clock=clock)
    add(first, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    second = ResourceCenter(store, "another-student", clock=clock)
    assert second.search(semester=SEMESTER)["total"] == 0
    assert second.download_records(SEMESTER)["count"] == 0
    with pytest.raises(ResourceNotFound):
        second.resource(SEMESTER, "learn:FILE:f1")
    plan = first.preview_download(
        semester=SEMESTER, selections=["learn:FILE:f1"], destination=str(Path("/tmp") / "synthetic")
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
    add(centre, [], complete=False, source_errors=["SYNTHETIC_PARTIAL"])
    assert centre.search(semester=SEMESTER)["total"] == 0
    historical = centre.search(semester=SEMESTER, allow_historical=True)
    assert historical["total"] == 1
    assert historical["items"][0]["historical"] is True
    assert centre.coverage(SEMESTER)["states"]["STALE"] == 1


def test_stale_index_cannot_be_downloaded_without_explicit_history(centre, tmp_path):
    add(centre, [synthetic.candidate(record_id="f1", title="讲义.pdf")])
    add(centre, [], complete=False, source_errors=["SYNTHETIC_PARTIAL"])
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
    assert registered["account_scope"] == centre.account_scope
    assert registered["task_id"].startswith("rplan_")
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
    assert str(tmp_path / "private-download-root") not in raw
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
    assert str(tmp_path) not in text
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
        assert cls.code.startswith("RESOURCE_")
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
