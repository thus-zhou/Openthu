"""Preview or apply the legacy-to-current state migration.

Examples::

    python scripts/migrate_state.py preview
    python scripts/migrate_state.py preview --json
    python scripts/migrate_state.py apply --dry-run
    python scripts/migrate_state.py apply --replace campus-v2.dpapi --keep config.json

``preview`` never modifies anything.  ``apply`` copies, never moves, and only
overwrites a target after an explicit ``--replace``; every other conflicting
item is reported and left alone.  A failed copy keeps the original file and
the journal under the target state directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from thu_assistant import upgrade  # noqa: E402


def _code(error: upgrade.UpgradeError) -> str:
    return f"迁移不可执行：{error.code}（{error.item}）"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preview", "apply"])
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--dry-run", action="store_true", help="只报告将执行的动作")
    parser.add_argument("--from", dest="legacy_root", metavar="PATH", help="旧状态目录")
    parser.add_argument("--to", dest="target_root", metavar="PATH", help="目标状态目录")
    parser.add_argument("--key-file", metavar="PATH", help="主密钥文件（只检查存在性）")
    parser.add_argument("--replace", action="append", default=[], metavar="ITEM",
                        help="明确允许覆盖的目标项（可重复）")
    parser.add_argument("--keep", action="append", default=[], metavar="ITEM",
                        help="明确保留原目标、不迁移的项（可重复）")
    args = parser.parse_args(argv)
    try:
        plan = upgrade.preview(
            legacy_root=args.legacy_root,
            target_root=args.target_root,
            key_file=args.key_file,
        )
    except upgrade.UpgradeError as error:
        print(_code(error), file=sys.stderr)
        return 1
    if args.command == "preview":
        print(json.dumps(plan, ensure_ascii=False, indent=2) if args.json else upgrade.describe(plan))
        if plan.get("no_op"):
            print("无需迁移：状态目录已经是当前版本的位置。", file=sys.stderr)
        return 0
    decisions = {item: "replace" for item in args.replace}
    decisions.update({item: "keep" for item in args.keep})
    try:
        result = upgrade.apply(plan, decisions=decisions, dry_run=args.dry_run)
    except upgrade.UpgradeError as error:
        print(_code(error), file=sys.stderr)
        return 1
    if not args.json:
        for row in result["results"]:
            print(f"[{row['status']}] {row['item']}")
        print(result["note"])
        if result["pending_decisions"]:
            print(
                f"仍有 {result['pending_decisions']} 项冲突未决定：请显式 --replace 或 --keep。",
                file=sys.stderr,
            )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
