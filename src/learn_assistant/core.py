"""网络学堂助手的业务层。

这里不包含网页点击逻辑，只编排 provider、文件安全检查、提交前校验和审计记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

from .errors import DownloadRejectedError, UnsafeDestinationError, ValidationError
from .models import Assignment, CourseFile, to_dict
from .providers.base import LearningProvider

DEFAULT_DOWNLOAD_ROOT = Path(os.environ.get("OPENTHU_DOWNLOAD_DIR") or Path.home() / "Downloads" / "Openthu")
DEFAULT_SYSTEM_DATA_ROOT = Path(os.environ.get("OPENTHU_DATA_DIR") or Path.home() / ".local" / "state" / "openthu")

# 网络学堂部分接口把文件类型返回成 ``docx``/``pptx`` 等简写，而不是
# 文件名后缀。这里只允许明确列出的类型参与后缀推断；未知值绝不直接拼接，
# 也不调用 mimetypes.guess_extension，以免把不受信任的接口字段带入路径。
_CONTROLLED_FILE_EXTENSIONS: dict[str, str] = {
    "pdf": ".pdf",
    "doc": ".doc",
    "docx": ".docx",
    "ppt": ".ppt",
    "pptx": ".pptx",
    "xls": ".xls",
    "xlsx": ".xlsx",
    "zip": ".zip",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_download_root() -> Path:
    explicit = (os.environ.get("OPENTHU_DOWNLOAD_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from thu_assistant.configuration import ConfigurationError, load_config
        return load_config().storage.download_dir
    except (ConfigurationError, ImportError, OSError, ValueError):
        return DEFAULT_DOWNLOAD_ROOT


def _default_system_data_root() -> Path:
    explicit = (os.environ.get("OPENTHU_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from thu_assistant import catalog
        return catalog.state_root()
    except (ImportError, OSError, ValueError):
        return DEFAULT_SYSTEM_DATA_ROOT


def _safe_destination(destination: str | os.PathLike[str] | None) -> Path:
    """解析允许的下载目录。

    未指定时只允许默认下载根目录；指定目录必须是绝对路径。相对路径不隐式落到
    当前工作目录，以免自然语言误解造成文件散落到代码树中。
    """

    if destination is None or str(destination).strip() == "":
        path = _default_download_root()
    else:
        path = Path(destination).expanduser()
        if not path.is_absolute():
            raise UnsafeDestinationError("下载目录必须是绝对路径，或省略以使用 用户下载目录中的 Openthu 子目录。")
    return path.resolve()


def _safe_relative_path(destination: Path, relative_path: str) -> Path:
    """确保 provider 返回的相对路径不会逃逸下载目录。"""

    normalized = relative_path.replace("\\", "/")
    candidate_relative = Path(normalized)
    if candidate_relative.is_absolute() or any(part == ".." for part in candidate_relative.parts):
        raise UnsafeDestinationError(f"provider 返回了不安全的文件路径：{relative_path}")
    if not candidate_relative.parts or normalized.endswith("/"):
        raise UnsafeDestinationError(f"provider 返回了空文件路径：{relative_path}")
    candidate = (destination / candidate_relative).resolve()
    try:
        candidate.relative_to(destination)
    except ValueError as exc:
        raise UnsafeDestinationError(f"文件路径逃逸下载目录：{relative_path}") from exc
    return candidate


def _add_controlled_extension(relative_path: str, mime_type: str | None) -> str:
    """仅为没有后缀的文件名补充受控白名单中的扩展名。

    ``mime_type`` 来自 provider/远端接口，不能直接作为路径片段使用。函数只
    查表识别标准 MIME 或有限的扩展名简写；未知、带路径字符或其他值均保持
    原路径不变，后续仍由 ``_safe_relative_path`` 执行路径逃逸检查。
    """

    normalized_path = str(relative_path).replace("\\", "/")
    if not normalized_path or normalized_path.endswith("/"):
        return normalized_path
    filename = PurePosixPath(normalized_path).name
    if not filename or PurePosixPath(filename).suffix:
        return normalized_path

    type_key = str(mime_type or "").strip().casefold()
    # 标准 MIME 可能带参数，例如 ``application/pdf; charset=binary``。
    type_key = type_key.split(";", 1)[0].strip()
    if type_key.startswith("."):
        type_key = type_key[1:]
    extension = _CONTROLLED_FILE_EXTENSIONS.get(type_key)
    if extension is None:
        return normalized_path
    return f"{normalized_path}{extension}"


def _looks_like_login_page(payload: bytes, expected_mime: str | None = None) -> bool:
    """识别常见的 HTML/SSO 登录页伪装下载。"""

    head = payload[:4096].lstrip().lower()
    if not head:
        return False
    is_html = head.startswith(b"<!doctype html") or head.startswith(b"<html")
    if not is_html:
        return False
    # 课程文件元数据若声明为 PDF/DOCX 等二进制类型，实际却返回 HTML，优先按
    # 登录页/错误页处理，即便页面没有英文 login 文案。
    if expected_mime and expected_mime.casefold() not in {"text/html", "application/xhtml+xml"}:
        return True
    login_markers = (
        b"login",
        b"sign in",
        b"sso",
        b"password",
        "登录".encode("utf-8"),
        "统一认证".encode("utf-8"),
    )
    return any(marker in head for marker in login_markers)


def _unique_target(path: Path, payload_hash: str) -> tuple[Path, bool]:
    """避免覆盖用户已有文件；相同哈希则安全跳过。"""

    if not path.exists():
        return path, False
    if path.is_file():
        try:
            if _sha256(path.read_bytes()) == payload_hash:
                return path, True
        except OSError:
            pass
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate, False
        if candidate.is_file() and not candidate.is_symlink():
            try:
                if _sha256(candidate.read_bytes())==payload_hash:return candidate,True
            except OSError:pass
    raise UnsafeDestinationError(f"无法为重名文件生成安全路径：{path}")


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class LearnAssistant:
    """围绕一个可替换 provider 的结构化助手。"""

    def __init__(
        self,
        provider: LearningProvider,
        *,
        system_data_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.provider = provider
        root = _default_system_data_root() if system_data_root is None else system_data_root
        self.system_data_root = Path(root).expanduser().resolve()

    def get_login_status(self) -> dict[str, Any]:
        return {"status": to_dict(self.provider.get_login_status())}

    def login(
        self,
        *,
        start_url: str | None = None,
        timeout_seconds: int = 300,
        persist: bool = True,
    ) -> dict[str, Any]:
        """打开用户可见浏览器完成登录；provider 决定实际登录适配器。"""

        return to_dict(
            self.provider.login(
                start_url=start_url,
                timeout_seconds=timeout_seconds,
                persist=persist,
            )
        )

    def list_courses(
        self, *, semester: str | None = None, query: str | None = None
    ) -> dict[str, Any]:
        courses = self.provider.list_courses(semester=semester, query=query)
        return {"courses": to_dict(courses), "count": len(courses), "provider": self.provider.name}

    def list_assignments(self, *, course_id: str | None = None) -> dict[str, Any]:
        assignments = self.provider.list_assignments(course_id=course_id)
        return {
            "assignments": to_dict(assignments),
            "count": len(assignments),
            "provider": self.provider.name,
            "read_evidence": getattr(self.provider,"read_evidence",None),
        }

    def get_assignment(self, *, assignment_id: str) -> dict[str, Any]:
        if not assignment_id:
            raise ValidationError("assignment_id 不能为空。")
        item=self.provider.get_assignment(assignment_id)
        return {"assignment": to_dict(item),"read_evidence":getattr(self.provider,"read_evidence",None)}

    def list_course_files(
        self, *, course_id: str | None = None, query: str | None = None
    ) -> dict[str, Any]:
        files = self.provider.list_course_files(course_id=course_id, query=query)
        return {"files": to_dict(files), "count": len(files), "provider": self.provider.name,"read_evidence":getattr(self.provider,"read_evidence",None)}

    def download_files(
        self,
        *,
        file_ids: Sequence[str] | None = None,
        course_id: str | None = None,
        query: str | None = None,
        destination: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """下载指定文件，并记录可审计清单。

        调用方至少需要给出 file_ids、course_id 或 query 之一。不会默认下载所有课程，
        防止自然语言中的模糊请求触发大批量写入。
        """

        normalized_ids = [str(item) for item in (file_ids or []) if str(item).strip()]
        if not normalized_ids and not course_id and not query:
            raise ValidationError("下载至少需要 file_ids、course_id 或 query 之一。")

        dest = _safe_destination(destination)
        dest.mkdir(parents=True, exist_ok=True)

        selected: list[CourseFile] = []
        seen: set[str] = set()
        for file_id in normalized_ids:
            item = self.provider.get_course_file(file_id)
            if item.id not in seen:
                selected.append(item)
                seen.add(item.id)
        if course_id or query:
            for item in self.provider.list_course_files(course_id=course_id, query=query):
                if item.id not in seen:
                    selected.append(item)
                    seen.add(item.id)
        if not selected:
            raise ValidationError("没有匹配到需要下载的课程文件。")

        manifest_path = dest / ".learn-assistant-downloads.jsonl"
        downloaded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for item in selected:
            try:
                payload = self.provider.download_file(item.id)
                if _looks_like_login_page(payload, item.mime_type):
                    raise DownloadRejectedError(
                        f"文件 {item.name} 返回了疑似 SSO/登录 HTML，已阻止写入。"
                    )
                digest = _sha256(payload)
                relative_path = _add_controlled_extension(
                    item.relative_path or item.name,
                    item.mime_type,
                )
                target = _safe_relative_path(dest, relative_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target, is_same = _unique_target(target, digest)
                common = {
                    "file_id": item.id,
                    "course_id": item.course_id,
                    "course_name": item.course_name,
                    "source_name": item.name,
                    "source_kind": item.source_kind,
                    "source_id": item.source_id,
                    "sha256": digest,
                    "bytes": len(payload),
                    "recorded_at": _now_iso(),
                    "provider": self.provider.name,
                }
                if is_same:
                    record = {**common, "status": "skipped_same_content", "path": str(target)}
                    skipped.append(record)
                    _append_jsonl(manifest_path, record)
                    continue
                with target.open('xb') as stream:stream.write(payload)
                record = {**common, "status": "downloaded", "path": str(target)}
                downloaded.append(record)
                _append_jsonl(manifest_path, record)
            except Exception as exc:  # provider errors become per-file structured failures
                record = {
                    "file_id": item.id,
                    "source_name": item.name,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recorded_at": _now_iso(),
                    "provider": self.provider.name,
                }
                failures.append(record)
                _append_jsonl(manifest_path, record)

        return {
            "destination": str(dest),
            "manifest_path": str(manifest_path),
            "selected_count": len(selected),
            "downloaded": downloaded,
            "skipped": skipped,
            "failures": failures,
            "ok": not failures,
        }

    def prepare_submission(
        self,
        *,
        assignment_id: str,
        file_paths: Sequence[str | os.PathLike[str]],
        note: str = "",
    ) -> dict[str, Any]:
        """只校验本地文件并生成确认摘要；永远不上传、不点击提交。"""

        if not assignment_id:
            raise ValidationError("assignment_id 不能为空。")
        if not file_paths:
            raise ValidationError("至少需要一个本地待提交文件。")
        assignment = self.provider.get_assignment(assignment_id)
        errors: list[str] = []
        warnings: list[str] = []
        files: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        if not assignment.allow_multiple_files and len(file_paths) > 1:
            errors.append("该作业不允许多个文件，但传入了多个文件。")

        allowed = {ext.casefold() for ext in assignment.allowed_extensions}
        for raw_path in file_paths:
            path = Path(raw_path).expanduser().resolve()
            path_key = str(path).casefold()
            if path_key in seen_paths:
                errors.append(f"重复指定文件：{path.name}")
                continue
            seen_paths.add(path_key)
            # Only the display name, size and hash cross the local boundary.
            # The absolute path stays inside this method; it is not copied to
            # the MCP result or to the plaintext confirmation preview.
            record: dict[str, Any] = {"name": path.name, "exists": path.exists()}
            if not path.exists():
                errors.append(f"文件不存在：{path.name}")
                files.append(record)
                continue
            if not path.is_file():
                errors.append(f"路径不是普通文件：{path.name}")
                files.append(record)
                continue
            size = path.stat().st_size
            digest = _sha256(path.read_bytes())
            extension = path.suffix.casefold()
            record.update({"bytes": size, "sha256": digest, "extension": extension})
            if allowed and extension not in allowed:
                errors.append(
                    f"文件扩展名不符合要求：{path.name}（允许：{', '.join(assignment.allowed_extensions)}）"
                )
            if assignment.max_bytes is not None and size > assignment.max_bytes:
                errors.append(
                    f"文件超过大小限制：{path.name}（{size} > {assignment.max_bytes} bytes）"
                )
            if size == 0:
                errors.append(f"文件为空：{path.name}")
            files.append(record)

        if assignment.submitted_at:
            warnings.append("该作业已有提交记录；当前版本不会覆盖或修改远端提交。")

        operation_id = f"prep-{uuid.uuid4().hex}"
        ready = not errors
        summary: dict[str, Any] = {
            "operation_id": operation_id,
            "prepared_at": _now_iso(),
            "provider": self.provider.name,
            "assignment": to_dict(assignment),
            "files": files,
            "note": note,
            "errors": errors,
            "warnings": warnings,
            "ready_for_manual_confirmation": ready,
            "confirmation_required": True,
            "submit_supported": False,
            "status": "ready_for_manual_confirmation" if ready else "blocked",
            "message": (
                "本地校验通过；正式提交仍未实现，当前只生成确认摘要。"
                if ready
                else "本地校验未通过；未生成可提交的文件集合。"
            ),
        }
        # The preview is legacy plaintext (documented in the source matrix);
        # keep it owner-only and return a logical name, never a local path.
        preview_name = f"submission-previews/{operation_id}.json"
        preview_dir = self.system_data_root / "submission-previews"
        preview_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(preview_dir, 0o700)
        preview_path = preview_dir / f"{operation_id}.json"
        summary["preview"] = preview_name
        payload = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(preview_path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if os.name != "nt":
            os.chmod(preview_path, 0o600)
        return summary

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """把 MCP/CLI 的工具名统一映射到核心方法。"""

        args = arguments or {}
        if name == 'query_learning':
            method=getattr(self.provider,'query_learning',None)
            if method is None:raise ValidationError('EXPANDED_LEARN_READ_DISABLED')
            return method(args['operation'],args['parameters'])
        if name == "login":
            return self.login(
                start_url=args.get("start_url"),
                timeout_seconds=int(args.get("timeout_seconds", 300)),
                persist=bool(args.get("persist", True)),
            )
        if name == "get_login_status":
            return self.get_login_status()
        if name == "list_courses":
            return self.list_courses(semester=args.get("semester"), query=args.get("query"))
        if name == "list_assignments":
            return self.list_assignments(course_id=args.get("course_id"))
        if name == "get_assignment":
            return self.get_assignment(assignment_id=str(args.get("assignment_id", "")))
        if name == "list_course_files":
            return self.list_course_files(course_id=args.get("course_id"), query=args.get("query"))
        if name == "download_files":
            return self.download_files(
                file_ids=args.get("file_ids"),
                course_id=args.get("course_id"),
                query=args.get("query"),
                destination=args.get("destination"),
            )
        if name == "prepare_submission":
            return self.prepare_submission(
                assignment_id=str(args.get("assignment_id", "")),
                file_paths=args.get("file_paths") or [],
                note=str(args.get("note", "")),
            )
        raise ValidationError(f"未知工具：{name}")
