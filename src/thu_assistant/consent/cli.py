"""Local interactive consent entry for campus actions.

Run this from a real terminal on the machine that owns the protected state::

    python -m thu_assistant.consent --plan <operation_id>

The command decrypts one prepared plan, shows its business fields and asks for
an explicit ``CONFIRM``.  Only then does it sign a single-use grant with the
local host key and leave it for the action engine.  It refuses to run without
an interactive terminal, so MCP clients, model output, web content and
environment variables cannot drive it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from ..bridge import CampusError
from .store import HostConsent

CONFIRMATION = "CONFIRM"
_FIELDS = (
    ("target", "对象"),
    ("date", "日期"),
    ("start_time", "开始"),
    ("end_time", "结束"),
    ("members", "参加者"),
    ("headcount", "人数"),
    ("fee", "费用"),
    ("payment_method", "付款方式"),
    ("record_id", "记录号"),
    ("purpose", "用途"),
    ("cancel_rule", "取消条件"),
)


def summary(record: dict) -> str:
    """Human-readable plan summary; local terminal output only."""
    business = record.get("business") if isinstance(record.get("business"), dict) else {}
    lines = [
        "计划 " + str(record.get("operation_id")),
        "服务 " + str(record.get("service_id")) + " / " + str(record.get("operation")),
        "适配器 " + str(record.get("adapter_version")),
    ]
    for key, label in _FIELDS:
        value = business.get(key)
        if value is None or value == "":
            continue
        text = " ".join(str(value).split())
        lines.append(label + " " + text[:600])
    return "\n".join(lines)


def authorize(store, plan_id: str, *, confirm=input, isatty=None, now: float | None = None) -> dict:
    """Issue one signed grant after an explicit local confirmation.

    ``confirm`` and ``isatty`` are injected only by tests.  The production call
    site reads the real terminal and never receives them from a model or tool
    argument.
    """
    if isatty is not None and not isatty():
        raise CampusError("SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED")
    record = store.read(plan_id)
    if record.get("state") != "PREPARED":
        raise CampusError("INVALID_ACTION_TRANSITION")
    if record.get("reason") == "ADAPTER_REQUIRED":
        raise CampusError("ADAPTER_REQUIRED")
    host = HostConsent(store)
    prompt = summary(record) + "\n输入 " + CONFIRMATION + " 确认本次办理（其他输入取消）："
    try:
        answer = confirm(prompt)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if str(answer).strip() != CONFIRMATION:
        return {"ok": False, "plan_id": plan_id, "authorization": "DECLINED"}
    grant = host.issue_for(record, now=time.time() if now is None else now)
    host.put(record, grant)
    return {
        "ok": True,
        "plan_id": plan_id,
        "authorization": "RECORDED",
        "single_use": True,
        "expires_at": grant["expires_at"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local trusted-host confirmation for one prepared campus action.")
    parser.add_argument("--plan", required=True, help="Exact operation id returned by the prepare step")
    parser.add_argument("--state-dir", help="Exact actions-v1 directory; defaults to current application state")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print(json.dumps({"ok": False, "error": "SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED"}, ensure_ascii=False))
        return 3
    from ..action_store import ActionStore
    store = ActionStore(args.state_dir) if args.state_dir else ActionStore()
    try:
        result = authorize(store, args.plan, confirm=input)
    except CampusError as error:
        print(json.dumps({"ok": False, "error": error.code}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "ACTION_NOT_FOUND"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
