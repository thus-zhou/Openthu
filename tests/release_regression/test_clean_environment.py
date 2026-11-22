"""A clean CI start must not depend on a developer machine or user profile.

The MCP server is launched in a subprocess with an empty environment except for
an isolated HOME, a temporary local-app-data directory and the repository's
``src`` path.  It must answer the frozen 29-tool catalog and the static service
directory without reading a developer config, a saved credential or a private
path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASELINE = json.loads((ROOT / "contracts" / "v0.2" / "compatibility-baseline.json")
                      .read_text(encoding="utf-8"))


def clean_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "local"),
        "APPDATA": str(home / "roaming"),
        "TMPDIR": str(home / "tmp"),
        "TEMP": str(home / "tmp"),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_mcp(tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[dict]]:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_services", "arguments": {}}},
    ]
    result = subprocess.run(
        [sys.executable, "-m", "thu_assistant.mcp_stdio"],
        input="".join(json.dumps(message) + "\n" for message in messages),
        cwd=tmp_path, env=clean_environment(tmp_path / "home"),
        capture_output=True, text=True, timeout=120, check=False)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result, rows


def test_mcp_server_starts_from_a_clean_home_without_user_config(tmp_path):
    result, rows = run_mcp(tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert not result.stderr.strip()
    assert [row["id"] for row in rows] == [1, 2, 3]
    tools = rows[1]["result"]["tools"]
    assert len(tools) == BASELINE["mcp_tool_count"] == 29
    assert [tool["name"] for tool in tools] == BASELINE["mcp_tools"]
    assert rows[2]["result"].get("isError") is False
    payload = json.loads(rows[2]["result"]["content"][0]["text"])
    assert payload["services"]
    assert str(Path.home()) not in result.stdout


def test_default_paths_are_derived_from_the_clean_home(tmp_path):
    from thu_assistant import paths

    home = tmp_path / "home"
    env = clean_environment(home)
    for platform_name in ("posix", "nt"):
        value = paths.default_paths(env=env, platform_name=platform_name)
        for field in ("config_file", "state_dir", "download_dir", "log_dir"):
            candidate = Path(getattr(value, field))
            assert candidate.is_absolute(), (platform_name, field)
            assert str(candidate).startswith(str(home)), (platform_name, field, candidate)
        assert str(value.state_dir).startswith(str(home))


def test_generated_mcp_config_uses_the_checkout_without_credentials(tmp_path):
    output = tmp_path / "mcp.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "configure_mcp.py"), "--output", str(output)],
        cwd=tmp_path, env=clean_environment(tmp_path / "home"),
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    value = json.loads(output.read_text(encoding="utf-8"))
    server = value["mcpServers"]["tsinghua-assistant"]
    assert server["command"] == sys.executable
    assert server["env"]["PYTHONPATH"] == str(ROOT / "src")
    assert set(server["env"]) == {"PYTHONUTF8", "PYTHONPATH", "THU_PYTHON", "THU_NODE",
                                  "THU_MADMODEL_DIR", "THU_ACCESS_HELPER", "THU_LEARN_PROVIDER"}
    text = json.dumps(value, ensure_ascii=False)
    # The checkout and the chosen interpreter/node are explicit, but no saved
    # user configuration, state directory or credential may be embedded.
    assert str(Path.home() / ".config") not in text
    assert str(Path.home() / ".local" / "state") not in text
    assert "cookie" not in text.casefold()
    assert "token" not in text.casefold()
    assert "OPENTHU_SECRET" not in text


def test_missing_dependencies_are_a_stable_diagnosis_not_a_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dev.py"), "doctor", "--json"],
        cwd=ROOT, env=clean_environment(tmp_path / "home"),
        capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode in (0, 1)
    value = json.loads(result.stdout)
    assert value["mode"] == "offline-development"
    assert isinstance(value["checks"], list) and value["checks"]
    assert "Traceback" not in result.stderr
    assert str(Path.home()) not in result.stdout
