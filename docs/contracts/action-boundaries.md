# 办理边界：上传、草稿、正式提交、续借与付款

本文件冻结“准备 / 上传 / 草稿 / 正式提交 / 结果核对”和“资格 / 执行 / 到期日 / 付款 / 退款”的语义。它不新增生产写接口，也不授权任何真实交易。

## 作业提交

相关契约：`assignment-v1`、`submission-v1`、`media-reference-v1`、`business-facade-v1`。

| 状态 | 含义 | 不能推出 |
|---|---|---|
| `PREPARED_LOCAL` | 本地形成计划，尚未发送 | 服务器已收到文件 |
| `UPLOAD_STARTED` | 已开始上传；结果未知前不能重放 | 上传成功 |
| `UPLOADED_STAGING` | 服务器暂存了文件 | 已保存草稿或已正式提交 |
| `DRAFT_SAVED` | 服务器明确保存了草稿 | 已正式提交 |
| `SUBMITTED` | 服务器明确受理正式提交 | 已批阅；成绩已出 |
| `SUPERSEDED` | 该版本已被后续版本覆盖 | 旧版本仍可修改 |
| `FAILED` | 明确失败 | 远端一定没有副作用 |
| `UNKNOWN` | 未取得可靠结果 | 成功或失败；不得自动重放 |

硬规则：

1. 上传成功、草稿保存、正式提交是三个独立结果；不能折叠成 `SUBMITTED`。
2. 正式提交必须绑定该笔作业、该次版本和用户当次明确授权；版本开发授权不等于业务授权。
3. 任何已发出的提交在结果未知时只做只读核对，不自动重发。
4. 结果查询必须匹配作业、版本和远端提交标识；只看到“文件已上传”不能当正式提交。
5. 当前仓库只有作业只读支持；上传、草稿、正式提交和结果查询均没有已验证来源，5.3 的生产写入保持 `BLOCKED_SOURCE_EVIDENCE`。

合成夹具：[submission-states.json](../../tests/fixtures/sources/submission-states.json) 明确区分 `UPLOADED_STAGING`、`DRAFT_SAVED`、`SUBMITTED`，只有最后一个是 `is_final=true`。

## 图书馆续借

相关契约：`order-v1`、`renewal-v1`。

| 事实 | 含义 | 不能推出 |
|---|---|---|
| `renew=Y` / `renewal_state=ELIGIBLE` | 来源提示可能有续借资格 | 续借已成功 |
| `renewstatuses` 文本 | 平台资格提示 | 执行接口或结果 |
| `execution_state=ACCEPTED` | 执行被明确受理 | 到期日已更新，除非核对到新值 |
| `execution_state=FAILED` | 执行明确失败 | 可以自动重试 |
| `due_date_after` | 执行后核对到的到期日 | 未核对时的猜测 |

硬规则：

1. 资格提示、执行受理、执行失败和新到期日分开记录。
2. 执行失败或未知时，`due_date_after` 保持原值（或 `null`），不得提前改到期日。
3. 当前 `library.loans` 只读 `renew` 和提示文本；没有续借执行端点、回执或新到期日来源，5.1 的续借执行保持 `BLOCKED_SOURCE_EVIDENCE`。
4. 续借是不可自动重放的写操作；不确定时只读核对，不重复点击。

合成夹具：[renewal-eligible-failed.json](../../tests/fixtures/sources/renewal-eligible-failed.json) 记录“资格为真、执行失败、到期日不变”。

## 体育付款与取消

相关契约：`order-v1`、`business-facade-v1`、`error-classification-v1`。

| 轴 | 取值 | 说明 |
|---|---|---|
| 预约/审批 | `PENDING_APPROVAL`、`SCHEDULED`、`CANCELLED` 等 | 审批通过不等于付款完成 |
| 付款 | `PENDING_PAYMENT`、`PROCESSING`、`PAID`、`PAYMENT_CANCELLED`、`PAYMENT_EXPIRED`、`UNKNOWN` | `PAID` 不等于预约一定生效 |
| 退款 | `NONE`、`REQUESTED`、`REFUNDING`、`REFUNDED`、`FAILED`、`UNKNOWN` 等 | 取消不等于退款完成 |

硬规则：

1. 预约创建成功、付款成功、审批通过、退款完成分别记录，不能合并成一个 `SUCCESS`。
2. 费用报价不是付款回执；实际金额以官方结算页为准。
3. 退款或审批后果不确定时，保留官方页面接手标志，不自动执行第二次写。
4. 既有体育适配只做合成传输验证；真实创建、取消和付款仍需该笔事项的具体授权。

## 通用办理硬规则

1. `prepare` 只写本地加密计划，不是提交；`execute` 是唯一校园写入口，且需要可信宿主授权。
2. `cancel` 只取消本地请求，不是校园业务取消；业务取消必须另走 `prepare`/`execute`。
3. 一次计划、一次派发、独立核对；派发后结果未知不自动重放。
4. 页面内容、模型输出、`authorization` 字符串和环境变量都不是可信授权。
5. 任何真实预约、付款、取消或交作业都必须由用户针对该笔事项明确指示；工程测试不制造真实业务。
