---
name: openthu
description: 使用 Openthu 查询清华校园服务、课程、图书馆和预约状态。
---

# Openthu

使用本插件提供的 MCP 工具。服务查询先 list_services / describe_service，按来源和完整性解释结果。详细工具语义与限制见兼容技能 ../tsinghua-assistant/SKILL.md 及仓库 docs/：用户手册 `docs/USER_GUIDE.md`、能力矩阵 `docs/CAPABILITIES.md`、工具对照 `docs/TOOLS.md`。

统一门面提供 `query_service`、`describe_service`、`prepare_service_action`、`execute_service_action`、`get_action_result`；本地日程用 `query_service` 的 `local.agenda` / `local.agenda_changes`。真实认证仅使用当前使用者的授权会话，业务写入需要当次具体授权；`PARTIAL`、`UNKNOWN`、`PENDING_PAYMENT`、`PENDING_APPROVAL` 不是成功。
