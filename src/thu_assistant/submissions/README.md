# submissions：作业上传与正式提交边界（5.3）

本包是 v0.2 网络学堂作业提交的受保护边界。开工结论为
`BLOCKED_SOURCE_EVIDENCE`：上传、草稿保存、正式提交和提交记录查询都没有已核对的
来源契约（见 `contracts/v0.2/sources/adapter-readiness.json` 的 5.3 行与
`source-tasks.json` 的 `task.learn.submission`）。因此本模块**没有生产传输**，也不
猜测任何真实路径：请求身份一律使用保留的 `/synthetic/` 前缀，只有显式声明
`synthetic = True` 的传输才会被接受。

已经实现的是任何真实适配器都必需的那一层：本地计划、文件清单冻结、有序单次请求
序列、阶段状态机、结果核对与未知结果恢复。旧的 `prepare_submission` 未改动，仍然
只生成本地校验摘要（`submit_supported = False`）。

## 启用状态

| 能力 | 状态 | 原因 |
|---|---|---|
| 生产上传 | `NOT_IMPLEMENTED` | 没有已核对的上传端点与 multipart/字段契约 |
| 生产草稿保存 | `NOT_IMPLEMENTED` | 草稿与正式提交的响应语义未证明 |
| 生产正式提交 | `NOT_IMPLEMENTED` | 版本覆盖语义与回执字段未证明 |
| 生产结果查询 | `NOT_IMPLEMENTED` | 没有已登记的提交记录读取入口 |
| 合成传输演练 | 可用 | `SyntheticSubmissionTransport`，`synthetic=true` |
| 本地文件校验与计划 | 可用 | 不联网、不上传 |

`Submissions()` 默认使用 `BlockedSubmissionTransport`，任何 `prepare`/`execute` 都
返回 `SUBMISSION_PRODUCTION_TRANSPORT_BLOCKED`。要启用真实适配器，必须先取得脱敏
来源证据并把传输显式改为非合成——这是需要记录的有意决定，而不是默认回退。

## 两个状态轴

本模块刻意保留两个互不替代的轴：

- `stage`：本地流水线，`PREPARED_LOCAL → UPLOADING → UPLOADED_STAGING →
  DRAFT_SAVED → SUBMIT_SENT_UNVERIFIED → VERIFIED`，失败或结果未知时为
  `FAILED`/`UNKNOWN`。
- `submission`：只由真实观测填充的 `submission-v1` 记录，状态取自冻结枚举
  `PREPARED_LOCAL / UPLOAD_STARTED / UPLOADED_STAGING / DRAFT_SAVED / SUBMITTED /
  SUPERSEDED / FAILED / UNKNOWN`。

冻结契约要求 `SUBMITTED` 同时具备 `is_final=true`、`result_observed=true` 和
`remote_submission_id`。因此"正式提交已发出、回执还没回来"只会停留在
`SUBMIT_SENT_UNVERIFIED`，**不会**生成 `SUBMITTED` 记录，也不会输出"已交作业"。
上传成功不等于草稿保存，草稿保存也不等于正式提交。

## 请求序列与授权

一个计划最多 8 个文件，对应 4.4 动作引擎单次授权门的上限 10 个请求：

1. 每个文件一个 `POST .../upload/<upload_ref>`；
2. 一个 `POST .../draft/<draft_ref>`（传输声明 `requires_draft = False` 时跳过）；
3. 一个 `POST .../submit/<submission_ref>`。

4.4 的 `CHECKS` 是冻结的四项，本模块的含义固定为：`availability` 提交窗口是否
开放（截止/补交时间）、`eligibility` 替换策略是否满足、`fees` 文件数量与大小限制
是否仍然满足（本模块没有费用概念，该槽位用于平台限制）、`rules` 作业身份与规则
（完成方式、允许扩展名、大小上限等）是否与计划一致。任一为假都会让引擎以
`PRECONDITIONS_CHANGED_PREPARE_AGAIN` 使计划失效，且不构造请求门。

所有请求体只使用**由计划号与文件哈希确定性派生**的客户端引用。4.4 的授权门要求
请求体在计划冻结时完全确定，服务端回执只用于核对，不会回流进后续请求体。

派发前动作引擎已把 `DISPATCHING`、授权证据和 owner 进程写入加密日志；每一步先在
`SideEffectLedger` 中标记 `NOT_SENT → SENT/UNKNOWN`，再交给传输。没有任何自动重试：
响应丢失后引擎进入 `UNKNOWN`，之后只允许读取核对。

`preflight` 和 `reconcile` **不写动作日志**：引擎在调用 `preflight` 前捕获计划
修订号、在 `reconcile` 前捕获结果修订号，从这两个钩子写同一日志会变成静默空操作。
因此上传回执、草稿引用、观测结果和核对结论写入本模块自己的加密提交日志
（`submissions-v1/plans`），文件路径与哈希写入 `submissions-v1/files`。两处都使用与
`ActionStore` 相同的保护编码器（2.2 信封或 Windows DPAPI），POSIX 权限 0600。

## 文件变化保护

准备阶段只读取、计算哈希并校验扩展名、大小、数量、重复路径、重名和符号链接；
真正的路径只留在受保护的本地记录里，业务对象只含 `name/size/sha256`。执行前
`preflight` 重新读取每个文件并逐项比较；任一文件被修改、移动或替换都会让计划
失效（`PRECONDITIONS_CHANGED_PREPARE_AGAIN`），并且**零请求**发出。上传前还会再读
一次字节，只有哈希完全一致的内容才会进入传输。

## 替换策略

`replacement_policy` 是必填字段：

- `NO_OVERWRITE`：已有提交或提交状态未知时拒绝（`SUBMISSION_EXISTING_SUBMISSION`
  或 `SUBMISSION_REPLACEMENT_UNVERIFIED`），不默认覆盖。
- `REPLACE_EXISTING`：必须同时给出要替换的版本号；平台报告的版本必须与之一致，
  否则拒绝。准备后若他端版本变化，`preflight` 使计划失效。

## 结果核对

核对分四个独立结论：`identity_verified`（作业与文件清单）、`content_verified`
（平台确实为每个文件提供哈希且一致）、`version_verified`（平台提供版本，替换时是
新版本）、`receipt_verified`（平台提供提交时间或回执）。`verified` 只要求作业身份、
最终状态和回执成立；平台只给文件名时 `content_verified` 保持 `false`，
`limitations` 写明 `CONTENT_HASH_NOT_AVAILABLE`，绝不声称内容完全一致。

## 恢复路径

| 情况 | 结果 |
|---|---|
| 上传响应丢失 | `UNKNOWN`；已发出的文件保留 `SENT/UNKNOWN` 与已收到回执，重复执行只读取 |
| 第二个文件失败 | 第一个文件的回执保留，第二个标 `UNKNOWN`，后续文件仍是 `PENDING` |
| 只保存了草稿 | `stage=DRAFT_SAVED`，`status=UNKNOWN`，不产生 `SUBMITTED` |
| 正式提交响应丢失 | 通过读取核对；确实提交成功才进入 `VERIFIED`，否则保持 `UNKNOWN` |
| 平台要求声明/官方验证 | `AWAITING_OFFICIAL`，零写入，下一步在官方页完成 |
| 截止时间或规则变化 | 计划 `FAILED`，零请求 |

本地取消（`Submissions.cancel`）只返回 `remote_changes = NONE`：本模块不提供撤回
或重交接口，清理草稿和代码回退都不会改动远端正式作业。

## 与其它模块的接线

- 6.1：`Submissions` 已提供 `prepare/execute/result/cancel/describe`；公共 MCP/CLI
  登记、`action_enabled` 的 `learn` 分支和 `describe_actions` 的开关语义由 6.1 完成。
  本模块在构造时调用 `submissions.contracts.register()` 把
  `('learn','submit_assignment')` 加入动作引擎注册表，并把
  `learn-submission-v1` 加入 `VERIFIED_ADAPTERS`（否则引擎会忽略
  `official_handoff`）。这两个都是附加操作，不修改 `actions.py`。
- 6.2：只读看板只消费 `result()`，不得调用 `execute`。
- 7.3：能力矩阵应继续保持"生产上传/草稿/提交未启用"，直到取得来源证据。
