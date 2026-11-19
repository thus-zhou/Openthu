"""零额外依赖的 MCP 风格 stdio JSON-RPC 入口。

只使用标准库，便于插件在没有 ``mcp``/``selenium`` 包的环境中启动。支持 MCP 客户端
最常用的 initialize、tools/list、tools/call；所有真实提交相关能力都没有注册。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .core import LearnAssistant
from .factory import create_provider

# One product version for the whole distribution; see thu_assistant.version.
from thu_assistant.version import __version__ as PRODUCT_VERSION

PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "login",
        "description": "打开可见 Playwright 浏览器并等待认证成功；智能体可用已授权凭据和验证码，通过浏览器或桌面控制工具完成登录。本工具不接收或保存密码。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_url": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 30},
                "persist": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_login_status",
        "description": "检查插件 Web provider 会话状态；与用户浏览器登录态可能不同。login 打开可见浏览器并捕获完成认证后的会话，不保存密码。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_courses",
        "description": "列出课程，可按学期或课程名称筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "semester": {"type": "string"},
                "query": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_assignments",
        "description": "列出作业，可按 course_id 筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {"course_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_assignment",
        "description": "查看单个作业要求、截止时间和提交约束。",
        "inputSchema": {
            "type": "object",
            "properties": {"assignment_id": {"type": "string"}},
            "required": ["assignment_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_course_files",
        "description": "列出课程文件，可按课程或文件名筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "string"},
                "query": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "download_files",
        "description": "把指定课程文件下载到明确目录或默认 用户下载目录中的 Openthu 子目录，并记录清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_ids": {"type": "array", "items": {"type": "string"}},
                "course_id": {"type": "string"},
                "query": {"type": "string"},
                "destination": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prepare_submission",
        "description": "只校验本地待提交文件并生成确认摘要；当前不支持真实上传或提交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "assignment_id": {"type": "string"},
                "file_paths": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": ["assignment_id", "file_paths"],
            "additionalProperties": False,
        },
    },
]


class McpServer:
    """可直接测试的 JSON-RPC/MCP 处理器。"""

    def __init__(self, assistant: LearnAssistant, server_name: str | None = None) -> None:
        self.assistant = assistant
        # 保留旧入口默认名称；新插件通过环境变量将 MCP server id 显式命名为
        # ``wangluo-xuetang``，底层 Python 包仍可复用同一个 stdio 服务。
        self.server_name = server_name or os.environ.get(
            "LEARN_ASSISTANT_MCP_SERVER_NAME", "network-learning-assistant"
        )

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": self.server_name,
                        "version": PRODUCT_VERSION,
                    },
                    "instructions": "智能体可以使用已授权会话、凭据与验证码，通过可用浏览器/桌面控制工具完成清华官方登录；login 仅打开可见浏览器等待认证并捕获会话，不接收密码参数。公告使用浏览器读取，严格遵守用户只读/不下载要求。正式提交未实现，prepare_submission 只生成本地确认摘要。",
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOL_DEFINITIONS}}
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                result = self.assistant.call_tool(str(name), arguments)
                text = json.dumps(result, ensure_ascii=False)
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": result,
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": {"type": type(exc).__name__, "message": str(exc)}},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def run_stdio(assistant: LearnAssistant) -> None:
    # MCP stdio 通道固定使用 UTF-8，避免 Windows 控制台代码页破坏中文 JSON。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC 请求必须是对象。")
            response = McpServer(assistant).handle(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="learn-assistant-mcp")
    parser.add_argument("--provider", choices=("mock", "web", "live"), default=None)
    args = parser.parse_args(argv)
    run_stdio(LearnAssistant(create_provider(args.provider)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
