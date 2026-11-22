from __future__ import annotations

import json
import os
import stat

import pytest

from learn_assistant.core import LearnAssistant, _add_controlled_extension
from learn_assistant.errors import UnsafeDestinationError
from learn_assistant.mcp_stdio import McpServer
from learn_assistant.models import CourseFile
from learn_assistant.providers.mock import MockProvider


def test_mock_queries_are_structured():
    assistant = LearnAssistant(MockProvider())

    status = assistant.get_login_status()
    assert status["status"]["authenticated"] is True
    assert assistant.list_courses()["count"] == 3
    assignments = assistant.list_assignments(course_id="course-geology")
    assert assignments["count"] == 1
    detail = assistant.get_assignment(assignment_id="assignment-geology-1")["assignment"]
    assert detail["allowed_extensions"] == [".pdf"]
    files = assistant.list_course_files(course_id="course-geology")
    assert files["count"] == 2


def test_download_writes_file_and_manifest(tmp_path):
    assistant = LearnAssistant(MockProvider())
    result = assistant.download_files(
        file_ids=["file-geology-intro"],
        destination=tmp_path / "downloads",
    )

    assert result["ok"] is True
    assert result["selected_count"] == 1
    assert result["downloaded"][0]["status"] == "downloaded"
    target = tmp_path / "downloads" / "课件" / "工程地质绪论.pdf"
    assert target.read_bytes().startswith(b"%PDF")
    manifest = tmp_path / "downloads" / ".learn-assistant-downloads.jsonl"
    assert manifest.exists()
    record = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert record["file_id"] == "file-geology-intro"

    second = assistant.download_files(
        file_ids=["file-geology-intro"],
        destination=tmp_path / "downloads",
    )
    assert second["skipped"][0]["status"] == "skipped_same_content"


def test_login_html_is_rejected_without_writing(tmp_path):
    assistant = LearnAssistant(MockProvider(include_login_html=True))
    result = assistant.download_files(
        file_ids=["file-login-html"],
        destination=tmp_path / "downloads",
    )

    assert result["ok"] is False
    assert result["failures"][0]["error_type"] == "DownloadRejectedError"
    assert not list((tmp_path / "downloads").rglob("伪装下载结果.pdf"))


def test_download_rejects_relative_destination():
    assistant = LearnAssistant(MockProvider())
    with pytest.raises(UnsafeDestinationError):
        assistant.download_files(file_ids=["file-geology-intro"], destination="relative")


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("doc", "课件/无后缀.doc"),
        ("pptx", "课件/无后缀.pptx"),
        ("application/pdf", "课件/无后缀.pdf"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "课件/无后缀.docx",
        ),
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "课件/无后缀.pptx"),
        ("application/vnd.ms-excel", "课件/无后缀.xls"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "课件/无后缀.xlsx"),
        ("application/zip", "课件/无后缀.zip"),
        ("application/pdf; charset=binary", "课件/无后缀.pdf"),
        ("application/pdf", "课件/已有后缀.pdf"),
        ("../../outside", "课件/无后缀"),
        ("application/x-unknown; path=../../outside", "课件/无后缀"),
    ],
)
def test_download_extension_inference_is_controlled(mime_type, expected):
    relative_path = "课件/已有后缀.pdf" if expected.endswith("已有后缀.pdf") else "课件/无后缀"
    assert _add_controlled_extension(relative_path, mime_type) == expected


def test_download_uses_inferred_extension_for_target(tmp_path):
    provider = MockProvider()
    provider._files.append(
        CourseFile(
            id="file-without-extension",
            course_id="course-geology",
            course_name="工程地质",
            name="无后缀",
            relative_path="课件/无后缀",
            size=4,
            mime_type="docx",
        )
    )
    provider._payloads["file-without-extension"] = b"DOCX"

    result = LearnAssistant(provider).download_files(
        file_ids=["file-without-extension"],
        destination=tmp_path / "downloads",
    )

    assert result["ok"] is True
    assert (tmp_path / "downloads" / "课件" / "无后缀.docx").read_bytes() == b"DOCX"
    assert not (tmp_path / "downloads" / "课件" / "无后缀").exists()


def test_prepare_submission_only_creates_local_confirmation_summary(tmp_path):
    report = tmp_path / "边坡分析.pdf"
    report.write_bytes(b"%PDF-1.4 mock report")
    assistant = LearnAssistant(MockProvider(), system_data_root=tmp_path / "system")
    result = assistant.prepare_submission(
        assignment_id="assignment-geology-1",
        file_paths=[report],
        note="请老师查收",
    )

    assert result["ready_for_manual_confirmation"] is True
    assert result["confirmation_required"] is True
    assert result["submit_supported"] is False
    assert result["files"][0]["name"] == "边坡分析.pdf"
    assert "path" not in result["files"][0]
    assert result["preview"] == f"submission-previews/{result['operation_id']}.json"
    assert str(report) not in json.dumps(result, ensure_ascii=False)
    preview = tmp_path / "system" / "submission-previews" / f"{result['operation_id']}.json"
    assert preview.exists()
    saved = json.loads(preview.read_text(encoding="utf-8"))
    assert saved["assignment"]["id"] == "assignment-geology-1"
    assert "path" not in saved["files"][0]
    assert str(report) not in preview.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(preview).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(preview.parent).st_mode) == 0o700


def test_prepare_submission_blocks_bad_extension(tmp_path):
    bad = tmp_path / "report.txt"
    bad.write_text("not a pdf", encoding="utf-8")
    assistant = LearnAssistant(MockProvider(), system_data_root=tmp_path / "system")
    result = assistant.prepare_submission(
        assignment_id="assignment-geology-1",
        file_paths=[bad],
    )
    assert result["status"] == "blocked"
    assert any("扩展名" in item for item in result["errors"])


def test_mcp_lists_only_safe_mvp_tools_and_calls_mock():
    server = McpServer(LearnAssistant(MockProvider()))
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert names == {
        "login",
        "get_login_status",
        "list_courses",
        "list_assignments",
        "get_assignment",
        "list_course_files",
        "download_files",
        "prepare_submission",
    }
    assert "submit_assignment" not in names
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_courses", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is False
    structured = response["result"]["structuredContent"]
    assert structured["count"] == 3


def test_mcp_prepare_submission_returns_no_absolute_path(tmp_path):
    from thu_assistant.mcp_stdio import CampusAssistant

    report = tmp_path / "private-name.pdf"
    report.write_bytes(b"%PDF-1.4 mcp report")
    assistant = CampusAssistant(
        learning_factory=lambda: LearnAssistant(MockProvider(),
                                                system_data_root=tmp_path / "system"))
    result = assistant.call("prepare_submission",
                            {"assignment_id": "assignment-geology-1",
                             "file_paths": [str(report)]})
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(report) not in serialized
    assert str(tmp_path) not in serialized
    assert result["files"][0]["name"] == "private-name.pdf"
    assert "path" not in result["files"][0]
    assert result["preview"].startswith("submission-previews/")
    preview = tmp_path / "system" / result["preview"]
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(preview).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(preview.parent).st_mode) == 0o700
