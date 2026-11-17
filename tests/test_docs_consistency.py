"""Documentation consistency checks for module 7.3.

These tests compare the public documentation with the running capability
registry, the frozen tool list and the real CLI.  They are behavioural: a new,
renamed or removed registered operation, tool or command fails the check
instead of silently drifting away from the manual and the capability matrix.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md",
             ROOT / "SECURITY.md", ROOT / "THIRD_PARTY.md"]
DOC_FILES += sorted((ROOT / "docs").rglob("*.md"))
DOC_FILES += sorted((ROOT / "integrations").rglob("*.md"))

CAPABILITY_ROW = re.compile(
    r"^\| `(?P<service>[a-z0-9-]+)\.(?P<operation>[a-z_]+)` \| "
    r"(?P<kind>[A-Z_]+) \| (?P<owner>[0-9.]+) \| (?P<state>[A-Z]+) \| "
    r"(?P<evidence>[A-Z_]+) \|"
)
TOOL_ROW = re.compile(r"^\| (?P<index>\d+) \| `(?P<name>[a-z_]+)` \|")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

OWNER_PATHS = {
    "3.2": ["src/thu_assistant/queries.py"],
    "3.3": ["src/thu_assistant/media_assets"],
    "4.1": ["src/thu_assistant/academics"],
    "4.2": ["src/thu_assistant/resources"],
    "4.3": ["src/thu_assistant/library.py", "src/thu_assistant/sports.py"],
    "4.4": ["src/thu_assistant/actions.py"],
    "4.5": ["src/thu_assistant/model_media.py"],
    "5.1": ["src/thu_assistant/library_actions.py", "src/thu_assistant/renewal"],
    "5.2": ["src/thu_assistant/sports_actions.py"],
    "5.3": ["src/thu_assistant/submissions"],
    "5.4": ["src/thu_assistant/events.py", "src/thu_assistant/snapshot_diff.py"],
}


def _service_ids():
    from thu_assistant import catalog

    ids = []
    for row in catalog.load_services():
        identifier = row.get("id") if isinstance(row, dict) else None
        if isinstance(identifier, str) and identifier and identifier not in ids:
            ids.append(identifier)
    for fallback in ("info", "learn", "library", "sports", "campus", "local"):
        if fallback not in ids:
            ids.append(fallback)
    return tuple(ids)


def default_registrations():
    """The same default surface the CLI describes: todos present, no 4.2 provider."""
    from thu_assistant import queries
    from thu_assistant.facade import registry

    entries, failures = registry.registrations(
        registry.Providers(todos=object()),
        service_ids=_service_ids(),
        describe=queries.describe,
        env={},
    )
    assert failures == []
    return list(entries)


def capability_rows():
    text = (ROOT / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        match = CAPABILITY_ROW.match(line)
        if not match:
            continue
        key = (match.group("service"), match.group("operation"))
        assert key not in rows, f"duplicate capability row: {key}"
        rows[key] = match.groupdict()
    return rows


def tool_rows():
    text = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        match = TOOL_ROW.match(line)
        if match:
            rows.append((int(match.group("index")), match.group("name")))
    return rows


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index(f"## {version}")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def test_every_registered_operation_has_one_capability_row():
    entries = default_registrations()
    rows = capability_rows()
    expected = {(entry.service_id, entry.operation) for entry in entries}
    assert set(rows) == expected, (
        "docs/CAPABILITIES.md must list exactly the running registry: "
        f"missing={sorted(expected - set(rows))} extra={sorted(set(rows) - expected)}"
    )
    for entry in entries:
        row = rows[(entry.service_id, entry.operation)]
        key = f"{entry.service_id}.{entry.operation}"
        assert row["kind"] == entry.kind, key
        assert row["owner"] == entry.owner, key
        assert row["state"] == entry.state, key
        assert row["evidence"] == entry.evidence, key


def test_capability_rows_are_well_formed_markdown_tables():
    text = (ROOT / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not CAPABILITY_ROW.match(line):
            continue
        cells = line.split("|")
        assert len(cells) == 8, (
            "a capability row contains an unescaped pipe in its note: " + line[:80])


def test_supported_operations_reach_an_implementation():
    from thu_assistant import actions, local_ops, queries

    for entry in default_registrations():
        key = (entry.service_id, entry.operation)
        if entry.state == "AVAILABLE" and entry.kind == "READ":
            if entry.service_id == "local":
                assert entry.operation in local_ops.operations(), key
            else:
                operations = {row["operation"] for row in queries.describe(entry.service_id)["operations"]}
                assert entry.operation in operations, key
        if entry.state == "AVAILABLE" and entry.kind == "CAMPUS_WRITE":
            contract = actions.REGISTRY.get(key)
            assert contract is not None, key
            assert contract["enabled"] is True, key
        if entry.operation == "renew_loan":
            assert key in actions.REGISTRY, "renewal keeps a prepare-only contract"
        if entry.service_id == "academics":
            from thu_assistant.academics import CAPABILITIES

            assert entry.operation in CAPABILITIES
        if key == ("learn", "submit_assignment"):
            from thu_assistant.submissions import capability_report

            assert capability_report()["operation"] == "submit_assignment"
        for path in OWNER_PATHS.get(entry.owner, []):
            assert (ROOT / path).exists(), f"owner {entry.owner} has no {path}"


def test_tool_table_matches_the_frozen_legacy_surface():
    baseline = json.loads((ROOT / "contracts" / "v0.2" / "compatibility-baseline.json")
                          .read_text(encoding="utf-8"))
    rows = tool_rows()
    assert len(rows) == baseline["mcp_tool_count"] == 29
    assert [index for index, _ in rows] == list(range(1, 30))
    assert {name for _, name in rows} == set(baseline["mcp_tools"])


def test_cli_commands_named_in_the_docs_exist():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-m", "thu_assistant", "--help"],
                            cwd=ROOT, env=environment, capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    choices = re.search(r"\{([a-z][a-z,]+)\}", result.stdout)
    assert choices, result.stdout
    known = set(choices.group(1).split(","))
    known |= {"openthu", "openthu-mcp", "openthu-doctor"}
    used = set()
    for path in DOC_FILES:
        for match in re.finditer(r"\bopenthu ([a-z][a-z-]+)", path.read_text(encoding="utf-8")):
            used.add(match.group(1))
    unknown = sorted(used - known)
    assert not unknown, f"documented commands not in the CLI: {unknown}"


def test_internal_markdown_links_resolve():
    broken = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split()[0].strip("<>")
            relative = target.split("#")[0]
            if not relative:
                continue
            destination = (path.parent / relative).resolve()
            if not destination.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_public_docs_contain_no_personal_paths_or_credentials():
    patterns = {
        "home directory": re.compile(r"/home/[A-Za-z0-9._-]+/"),
        "windows user directory": re.compile(r"(?i)[a-z]:\\+Users\\+[^\\\s]+"),
        "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "school address": re.compile(r"@mails\.tsinghua\.edu\.cn", re.I),
        "unfinished placeholder": re.compile(r"\{\{[A-Z_]+\}\}"),
    }
    findings = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for name, pattern in patterns.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {name}")
    assert findings == []


def test_candidate_notes_are_not_release_claims():
    section = changelog_section("0.2.0")
    assert "候选（未发布）" in section.splitlines()[0]
    for heading in ("用户可见变化", "兼容性", "安装资源", "已知限制", "未完成事项"):
        assert f"### {heading}" in section
    forbidden = ("已正式发布", "v0.2.0 已发布", "0.2.0 正式版", "published: true")
    assert not [phrase for phrase in forbidden if phrase in section]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "未发布候选" in readme


def test_user_guide_covers_the_required_paths():
    text = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    headings = [line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")]
    for topic in ("首次使用", "只读查询", "本地看板", "明确授权的办理", "课程资料中心",
                  "日程、增量变化与日历导出", "图片输入", "常见状态"):
        assert any(topic in heading for heading in headings), topic
    heading_index = {}
    for index, heading in enumerate(headings):
        for topic in ("首次使用", "只读查询", "本地看板", "明确授权的办理"):
            if topic in heading and topic not in heading_index:
                heading_index[topic] = index
    ordered = [heading_index[topic] for topic in
               ("首次使用", "只读查询", "本地看板", "明确授权的办理")]
    assert ordered == sorted(ordered)
    assert "THU_SECURE_KEY_FILE" in text and "THU_SECURE_DATA_DIR" in text
    assert "PARTIAL" in text and "UNKNOWN" in text and "PENDING_PAYMENT" in text
    assert "synthetic-account" in text and "learn:FILE:1001" in text
    assert "media:IMG:synthetic-0001" in text
    assert "无条件全自动登录" not in text


def test_upgrade_guide_preserves_pending_state_and_keys():
    text = (ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
    for phrase in ("不搬迁、不复制", "未决事务", "密钥", "重新认证", "migrate_state.py preview",
                   "不要用“删除用户目录”", "不改变任何校园侧"):
        assert phrase in text, phrase
    assert "升级不会自动" in text


def test_capability_matrix_does_not_turn_unknown_into_success():
    text = (ROOT / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    for status in ("UNKNOWN", "PARTIAL", "PENDING_PAYMENT", "PENDING_APPROVAL",
                   "BLOCKED_SOURCE_EVIDENCE", "ADAPTER_REQUIRED"):
        assert status in text, status
    assert "都不是成功" in text
    assert "安装成功、模拟测试通过、HTTP 200" in text


def test_tools_doc_operation_names_match_the_registry():
    text = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    registered = {(entry.service_id, entry.operation) for entry in default_registrations()}
    single_service = re.compile(r"^`(?P<service>[a-z0-9-]+)`$")
    operation = re.compile(r"`(?P<operation>[a-z_]+)`")
    checked = 0
    in_operations = False
    for line in text.splitlines():
        if line.startswith("### 3."):
            in_operations = True
            continue
        if line.startswith("## "):
            in_operations = False
            continue
        if not in_operations or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        service = single_service.match(cells[0])
        if not service:
            continue
        names = operation.findall(cells[1])
        for name in names:
            assert (service.group("service"), name) in registered, (
                f"docs/TOOLS.md names an unregistered operation: "
                f"{service.group('service')}.{name}")
            checked += 1
    assert checked >= 30


def test_user_guide_shows_verified_example_states():
    text = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    for code in ("SECURE_KEY_NOT_CONFIGURED", "EXPORT_SOURCE_INCOMPLETE",
                 "EXPORT_SCOPE_REQUIRED", "LOCAL_API_SESSION_REQUIRED",
                 "EVENT_CURSOR_EXPIRED", "MODEL_IMAGE_PROTOCOL_UNVERIFIED"):
        assert code in text, code
    assert '"status": "LISTENING"' in text
    assert '"status": "PARTIAL"' in text


def test_tools_doc_marks_blocked_and_limited_operations():
    text = (ROOT / "docs" / "TOOLS.md").read_text(encoding="utf-8")
    assert "submit_assignment" in text and "阻塞" in text
    assert "renew_loan" in text and "官方页面" in text
    assert "MODEL_IMAGE_INPUT_UNAVAILABLE" in text
    assert "ADAPTER_REQUIRED" in text


def test_no_table_cell_contains_an_unescaped_pipe():
    def spans(line: str):
        found, index = [], 0
        while True:
            start = line.find("`", index)
            if start < 0:
                break
            end = line.find("`", start + 1)
            if end < 0:
                break
            found.append(line[start + 1:end])
            index = end + 1
        return found

    for name in ("README.md", "CHANGELOG.md", "docs/USER_GUIDE.md",
                 "docs/TOOLS.md", "docs/CAPABILITIES.md", "docs/UPGRADE.md",
                 "docs/INSTALL.md"):
        for number, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1):
            if not line.startswith("|"):
                continue
            for span in spans(line):
                assert "|" not in span, f"{name}:{number} has a pipe inside a table code span"


def test_user_guide_json_examples_parse():
    text = (ROOT / "docs" / "USER_GUIDE.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert len(blocks) >= 3
    for block in blocks:
        value = json.loads(block)
        assert isinstance(value, dict)
    parameters = re.search(r'"semester":"([^"]+)"', text)
    assert parameters is None or re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", parameters.group(1))


def test_public_claims_keep_download_and_campus_service_separate():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    capabilities = (ROOT / "docs" / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "所有人下载" in readme
    assert "所有学校服务已接通" not in readme
    assert "所有学校服务已接通" not in capabilities
    assert "不代表" in capabilities


def test_plugin_metadata_shares_the_product_version():
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from thu_assistant.version import __version__
    finally:
        sys.path.pop(0)
    manifest = json.loads((ROOT / "integrations" / "codex" / ".codex-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))
    assert manifest["version"] == __version__


def test_guide_and_capability_matrix_are_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in ("docs/USER_GUIDE.md", "docs/CAPABILITIES.md", "docs/TOOLS.md",
                   "docs/UPGRADE.md", "docs/INSTALL.md"):
        assert f"({target})" in readme, target
