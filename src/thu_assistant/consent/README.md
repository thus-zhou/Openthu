# 可信宿主授权与统一办理（4.4）

本目录实现 `host-authorization-v1`，为 `actions.py` 的一次计划、一次派发和
事后核对提供跨平台的本机授权凭据。它不包含任何学校站点适配器，也不发送请求。

## 授权对象

一次授权（grant）只绑定一笔计划：

| 字段 | 含义 | 规则 |
|---|---|---|
| `version` | `host-authorization-v1` | 未知版本一律拒绝 |
| `plan_id` | 计划 ID | 必须等于目标计划 |
| `account` | 当前账号作用域 | 与计划账号不一致即拒绝 |
| `digest` | 业务字段摘要 | 任一业务字段、附件哈希或页面上下文变化后旧计划失效 |
| `adapter_version` | 站点适配器版本 | 版本变化即拒绝 |
| `challenge` | 计划创建时生成的 64 位十六进制随机值 | 一次性，不出现在公开结果、日志或 URL |
| `source` | 固定为 `human` | 其他来源一律不是授权 |
| `reference` | 本机确认事件编号 | 必填、非空 |
| `issued_at` / `expires_at` | 签发与到期时刻 | 不得晚于计划到期；过期按 `ACTION_PLAN_EXPIRED` 处理 |
| `signature` | HMAC-SHA256（宿主密钥） | 恒定时间比较；失败即无有效证据 |

签名只证明“本机受控签发路径产生过这条授权”，不证明学校接受、不证明付款或
审批结果，也不是平台幂等键。业务状态必须另行核对。

## 签发路径

- 通用本机入口：在拥有受保护状态的机器上，从真实终端运行
  `python -m thu_assistant.consent --plan <operation_id>`。命令显示计划业务字段，
  只有输入大写的 `CONFIRM` 才签发；没有交互终端时返回
  `SPECIFIC_HUMAN_AUTHORIZATION_REQUIRED`，不生成任何授权。MCP 参数、网页文本、
  模型输出和普通环境变量都不能驱动该命令。
- Windows 现有确认：`sports_actions.native_grant` 仍使用系统消息框，只有本机人工
  点击“是”才返回确认。该回调目前是兼容的进程内确认，尚未迁移为签名授权；
  5.2 接入时应改为 `HostConsent.issue_for(record)`，从而同时绑定挑战值。
- 授权文件以 `THU-CONSENT-1` 容器 + 与事务日志相同的保护函数加密；宿主密钥
  以 `THU-CONSENT-KEY-1` 容器保存，POSIX 权限 0600，缺失时失败关闭。
  读取授权文件后立即删除，因此同一授权只能使用一次。

## 拒绝语义

| 情况 | 结果 |
|---|---|
| 没有授权 / 签名无效 / 计划或摘要或挑战值不匹配 | `TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED`，计划保持 `PREPARED` |
| 另一账号 | `ACTION_ACCOUNT_MISMATCH` |
| 适配器版本变化 | `ACTION_ADAPTER_CHANGED` |
| 计划或授权到期 | `ACTION_PLAN_EXPIRED`，计划进入 `FAILED`（预检前） |
| 授权文件损坏 | `ACTION_LOG_CORRUPT`；解密失败 | `ACTION_LOG_DECRYPT_FAILED` |
| 并发执行时未抢到一次性授权 | 安全失败；先到者最多派发一次，重复调用应查询结果 |

错误、公开结果和 URL 只含错误类别，不含授权值、业务原值、签名或挑战值。

## 状态与进入条件

| 状态 | 进入条件 | 授权 | 远端写入 |
|---|---|---|---|
| `PREPARED` | 业务字段完整且有已登记适配器 | 未消费 | 无 |
| `NEEDS_USER_ACTION` | 缺字段或没有适配器 | 不适用 | 无 |
| `AWAITING_OFFICIAL` | 预检把步骤交给官方页面，未发请求 | 仅记录，供人工办理 | 无 |
| `DISPATCHING` | 原子写入授权、派发意图和进程号后 | 已消费 | 可能已开始 |
| `UNKNOWN` | 派发无可靠结果，或核对不能唯一匹配 | 已消费 | 未知 |
| `PENDING_APPROVAL` | 恰好一条匹配记录显示待审批 | 已消费 | 已受理 |
| `PENDING_PAYMENT` | 恰好一条匹配记录显示待付款 | 已消费 | 已受理 |
| `SUCCEEDED` | 恰好一条匹配记录为终态成功 | 已消费 | 已受理 |
| `FAILED` | 过期、确定未发出、前置变化或一条明确失败 | 不可再用 | 无或失败 |

`DISPATCHING` 必须在适配器拿到请求能力之前落盘；落盘失败时不发出任何请求。
`UNKNOWN`、`PENDING_*` 只能核对，不能自动重发。终态不因迟到的派发返回而降级重发。

## 精确请求门禁

`RequestGate` 对每个请求精确匹配 URL（含参数）、方法、规范化请求体、来源 frame，
按顺序单次消费，最多 10 步；JSON 重复键拒绝，form 重复参数保留。认证令牌可以
在适配器构造请求时正常刷新，但业务字段变化必须重新准备。浏览器路由在派发阶段
拒绝任何未匹配的写请求，不恢复同域整体放行或时间窗口。

## 多阶段副作用

`SideEffectLedger` 由声明 `accepts_ledger = True` 的适配器使用，逐步骤记录
`NOT_SENT` / `SENT` / `UNKNOWN`，状态只能前进：`NOT_SENT → SENT → UNKNOWN`。
超时后不得自动重复上传；关闭页面不撤销远端草稿；平台清理必须作为单独登记动作
重新准备和授权。`NotDispatched` 只有在请求计数为零且副作用清单没有 `SENT`/`UNKNOWN`
时才归为“确定未发出”。

## 回退与离线盘点

`python -m thu_assistant.action_recovery --state-dir <actions-v1>` 只读列出操作 ID、
状态、修订号、适配器版本、步骤状态和是否有未决事务；不访问网络、不解密打印业务
原值、不发起取消。代码回退和本地日志清理都不能代替真实业务取消；取消、撤回、
删除附件和退款必须走各自平台的登记动作和该笔授权。回退时保留能识别新格式的
运行时、宿主密钥和未决日志。

## 跨模块接线

- 5.2：把 Windows 消息框确认改为签名授权；`sports_actions.py` 不在本模块修改范围。
- 5.3：上传/草稿/提交适配器使用 `accepts_ledger = True` 与 `SideEffectLedger`，
  不自行实现重复上传或“关闭页面即撤销”的假设。
- 6.1：把 `TRUSTED_AUTHORIZATION_EVIDENCE_REQUIRED` 映射为“请在本机确认”，并在
  业务门面暴露 `next_actions` 与 `local_confirmation_command`；不要把授权值放入日志。
- 1.1/集成人：如需为宿主授权的细分拒绝原因新增错误码，应在
  `contracts/v0.2/error-classification.json` 登记；本模块暂用现有错误码，
  细分原因保留在计划状态与测试中。
