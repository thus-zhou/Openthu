"""Local interactive consent entry for campus actions.

Run this from a real terminal on the machine that owns the protected state::

    python -m thu_assistant.consent --plan <operation_id>

The command decrypts one prepared plan, shows every business field (including
the exact seat, room or sports resource) and asks for an explicit ``CONFIRM``.
Only then does it sign a single-use grant with the local host key and leave it
for the action engine.  It refuses to run without an interactive terminal, so
MCP clients, model output, web content and environment variables cannot drive
it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from ..bridge import CampusError
from .protocol import VERSION, new_challenge
from .store import HostConsent

CONFIRMATION = "CONFIRM"
_VALUE_LIMIT = 1200
_SECRET_KEY = re.compile(
    r"password|passwd|token|cookie|csrf|xsrf|secret|authorization|signature|ticket|"
    r"session|otp|vericode|nonce|relaystate|saml",
    re.I,
)
_LABELS = {
    "target": "对象",
    "system": "系统",
    "location": "位置",
    "area_id": "区域编号",
    "seat_id": "座位编号",
    "kind_id": "房间类型编号",
    "room_id": "房间编号",
    "scene_id": "场馆编号",
    "site_id": "场地编号",
    "session_id": "场次编号",
    "mode": "参与方式",
    "date": "日期",
    "start_time": "开始",
    "end_time": "结束",
    "members": "参加者",
    "headcount": "人数",
    "purpose": "用途",
    "fee": "费用",
    "expected_total_fen": "指定总额（分）",
    "payment_method": "付款方式",
    "record_id": "记录号",
    "cancel_rule": "取消条件",
    "rules_summary": "平台规则摘要",
    "official_url": "官方页面",
    "handoff": "需在官方页面处理",
    "proof": "资格证明",
    "prior_ids": "同时间段已有记录",
    "attachments": "附件",
    "resolved_members": "已解析参加者",
}
_ORDER = list(_LABELS)


def _text(value) -> str:
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    return text[:_VALUE_LIMIT] + ("…（截断）" if len(text) > _VALUE_LIMIT else "")


def summary(record: dict) -> str:
    """Full plan summary for the local terminal; never writes or logs it."""
    business = record.get("business") if isinstance(record.get("business"), dict) else {}
    lines = [
        "计划 " + str(record.get("operation_id")),
        "服务 " + str(record.get("service_id")) + " / " + str(record.get("operation")),
        "适配器 " + str(record.get("adapter_version")),
        "业务摘要 " + str(record.get("digest", ""))[:12],
    ]
    keys = [key for key in _ORDER if key in business]
    keys += sorted(key for key in business if key not in _LABELS)
    shown = 0
    for key in keys:
        value = business.get(key)
        if value is None or value == "" or _SECRET_KEY.search(str(key)):
            continue
        lines.append(_LABELS.get(key, key) + " " + _text(value))
        shown += 1
    if not shown:
        lines.append("（计划没有可显示的业务字段）")
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
    if not record.get("challenge"):
        # Records created before host-authorization-v1 have no challenge.
        # Add one locally before signing; the old runtime ignores the field.
        record = store.update(
            plan_id,
            lambda row: row.update(challenge=new_challenge(), consent_protocol=VERSION),
            record["revision"],
        )
    host = HostConsent(store)
    prompt = summary(record) + "\n输入 " + CONFIRMATION + " 确认本次办理（其他输入取消）："
    try:
        answer = confirm(prompt)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if str(answer).strip() != CONFIRMATION:
        return {"ok": False, "plan_id": plan_id, "authorization": "DECLINED"}
    grant = host.issue_for(record, now=time.time() if now is None else now)
    replaced = host.put(record, grant)
    return {
        "ok": True,
        "plan_id": plan_id,
        "authorization": "RECORDED",
        "single_use": True,
        "replaced_pending_grant": replaced,
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
    except OSError:
        print(json.dumps({"ok": False, "error": "STATE_WRITE_FAILED"}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"ok": False, "error": "ACTION_LOG_CORRUPT"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
