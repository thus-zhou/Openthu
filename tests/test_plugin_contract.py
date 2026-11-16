from __future__ import annotations

import json
import re
from pathlib import Path

from learn_assistant.core import LearnAssistant
from learn_assistant.mcp_stdio import McpServer as LegacyServer
from thu_assistant.mcp_stdio import McpServer
from learn_assistant.providers.mock import MockProvider


PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "codex"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "tsinghua-assistant"

EXPECTED_TOOLS = {
    "login",
    "get_login_status",
    "list_courses",
    "list_assignments",
    "get_assignment",
    "list_course_files",
    "download_files",
    "prepare_submission",
}


def test_product_plugin_manifest_and_marketplace_entry():
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "openthu"
    assert manifest["interface"]["displayName"] == "Openthu 清华助手"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["version"]
    assert 1 <= len(manifest["interface"]["defaultPrompt"]) <= 3
    assert any("$openthu" in prompt for prompt in manifest["interface"]["defaultPrompt"])

    assert (PLUGIN_ROOT / "skills/openthu/SKILL.md").is_file()



def test_product_plugin_mcp_uses_real_web_provider():
    config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert set(config["mcpServers"]) == {"tsinghua-assistant"}
    server = config["mcpServers"]["tsinghua-assistant"]
    assert server["command"] == "python"
    assert server["args"] == ["-m", "thu_assistant.mcp_stdio"]
    assert server["env"]["THU_MADMODEL_DIR"] == "<absolute-repository-path>/vendor/madmodel-proxy"
    assert "mock" not in json.dumps(config, ensure_ascii=False).casefold()


def test_product_mcp_initialize_reports_new_server_id(monkeypatch):
    response = McpServer().handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert response["result"]["serverInfo"]["name"] == "tsinghua-assistant"


def test_product_skill_metadata_and_tool_contract():
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill_text.split("---", 2)[1]
    assert re.search(r"(?m)^name:\s*tsinghua-assistant\s*$", frontmatter)
    description = re.search(r"(?m)^description:\s*(.+)$", frontmatter)
    assert description is not None
    assert all(word in description.group(1) for word in ("登录", "课程", "作业", "图书馆", "预约"))
    assert "[TODO:" not in skill_text
    assert "submit_assignment" not in skill_text
    assert all(f"`{tool}`" in skill_text for tool in EXPECTED_TOOLS)

    openai_yaml = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert 'display_name: "Openthu 清华助手"' in openai_yaml
    assert "$tsinghua-assistant" in openai_yaml
    assert "allow_implicit_invocation: true" in openai_yaml


def test_legacy_tools_remain_compatible_and_campus_writes_are_explicit():
    server = LegacyServer(LearnAssistant(MockProvider()))
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == EXPECTED_TOOLS
    assert "submit_assignment" not in names
    campus = McpServer().handle({"jsonrpc":"2.0","id":2,"method":"tools/list"})
    added={t['name']:t for t in campus['result']['tools']}
    assert EXPECTED_TOOLS.issubset(added)
    assert added['execute_browser_action']['inputSchema']['required']==['plan_id','authorization']
