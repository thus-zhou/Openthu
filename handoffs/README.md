# 人工接力记录

按 TEMPLATE.md 为每个任务建立 `<task-id>.md`。代码提交后运行 `python scripts/handoff.py <task-id> --to codex` 或 `--to hermes`；提交生成的 `.json` 与文档再推送分支。详见 docs/MANUAL_RELAY.md。

这里不运行任何后台调度，也不保存私人日志。
