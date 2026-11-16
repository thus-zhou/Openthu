"""结构化命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .core import LearnAssistant
from .errors import LearnAssistantError
from .factory import create_provider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="learn-assistant",
        description="网络学堂本地助手（登录、只读查询、下载、提交前校验）",
    )
    parser.add_argument(
        "--provider",
        default=None,
        choices=("mock", "web", "live"),
        help="数据提供方；无账号时请使用 mock，web 使用用户主动浏览器登录。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="打开可见浏览器，等待用户或授权智能体完成 SSO/二次认证")
    login.add_argument("--start-url")
    login.add_argument("--timeout-seconds", type=int, default=300)
    login.add_argument("--no-persist", action="store_true", help="只在当前进程保留会话，不写入 DPAPI")

    sub.add_parser("get-login-status", help="返回登录状态")

    courses = sub.add_parser("list-courses", help="列出课程")
    courses.add_argument("--semester")
    courses.add_argument("--query")

    assignments = sub.add_parser("list-assignments", help="列出作业")
    assignments.add_argument("--course-id")

    assignment = sub.add_parser("get-assignment", help="查看作业详情")
    assignment.add_argument("assignment_id")

    files = sub.add_parser("list-course-files", help="列出课程文件")
    files.add_argument("--course-id")
    files.add_argument("--query")

    download = sub.add_parser("download-files", help="下载文件并生成清单")
    download.add_argument("--file-id", action="append", dest="file_ids", default=[])
    download.add_argument("--course-id")
    download.add_argument("--query")
    download.add_argument("--destination", help="绝对路径；省略时使用 用户下载目录中的 Openthu 子目录")

    prepare = sub.add_parser("prepare-submission", help="校验本地待提交文件并生成摘要")
    prepare.add_argument("assignment_id")
    prepare.add_argument("--file", action="append", dest="file_paths", default=[])
    prepare.add_argument("--note", default="")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    assistant = LearnAssistant(create_provider(args.provider))
    if args.command == "login":
        return assistant.login(
            start_url=args.start_url,
            timeout_seconds=args.timeout_seconds,
            persist=not args.no_persist,
        )
    if args.command == "get-login-status":
        return assistant.get_login_status()
    if args.command == "list-courses":
        return assistant.list_courses(semester=args.semester, query=args.query)
    if args.command == "list-assignments":
        return assistant.list_assignments(course_id=args.course_id)
    if args.command == "get-assignment":
        return assistant.get_assignment(assignment_id=args.assignment_id)
    if args.command == "list-course-files":
        return assistant.list_course_files(course_id=args.course_id, query=args.query)
    if args.command == "download-files":
        return assistant.download_files(
            file_ids=args.file_ids,
            course_id=args.course_id,
            query=args.query,
            destination=args.destination,
        )
    if args.command == "prepare-submission":
        return assistant.prepare_submission(
            assignment_id=args.assignment_id,
            file_paths=args.file_paths,
            note=args.note,
        )
    raise LearnAssistantError(f"未知命令：{args.command}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = {"ok": True, "result": _run(args)}
    except Exception as exc:
        result = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
