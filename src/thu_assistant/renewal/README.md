# 图书馆借阅续借（renewal）

本目录实现 v0.2.0 模块 5.1 的续借读侧、绑定与结果核对。与座位/研讨间共用
`library-space-action-v1` 适配器版本和统一事务引擎，但不改变原有三项操作。

## 当前能力与阻塞

| 能力 | 状态 |
|---|---|
| 读取本人借阅与续借资格（`renew` / `renewstatuses`） | 已实现 |
| 绑定单条借阅记录、当前到期时间与资格状态 | 已实现 |
| 无来源契约时给出官方 MyLib 页面移交（`AWAITING_OFFICIAL`） | 已实现 |
| 精确单请求派发与一次性请求门禁 | 已实现，仅显式契约可用 |
| 新到期时间核对 | 已实现 |
| 生产续借执行端点 | **阻塞**：来源注册表 `library.renewal` 仍为 PARTIAL，没有已验证的请求契约 |

`library_contracts.RENEW_LOAN` 的契约 `enabled` 为 `false`：facade 不得把续借
描述为可直接执行的写操作。`contract.REGISTERED` 在生产默认为空，只有组合根或测试
显式注入 `SourceContract` 时才可能派发；测试使用 `evidence_state="SYNTHETIC"`，
与未来已验证契约可辨别。6.1 负责把适配器接入统一入口；接入后若无已验证契约，动作
仍只产生官方页面移交。

## 状态映射

| 观察 | 计划状态 |
|---|---|
| 资格 `Y`、到期时间可解析、来源契约未验证 | `PREPARED` + `RENEWAL_SOURCE_CONTRACT_UNVERIFIED`，执行后 `AWAITING_OFFICIAL` |
| 资格 `N` | 准备阶段拒绝 `RENEWAL_NOT_ELIGIBLE` |
| 已逾期 | 准备阶段拒绝 `RENEWAL_OVERDUE` |
| 资格缺失/未知 | 官方页面移交 `RENEWAL_ELIGIBILITY_UNVERIFIED` |
| 请求已发出但响应丢失 | `UNKNOWN`，只核对不重放 |
| 同一记录出现更晚的到期时间 | `SUCCEEDED`，新值只放在 `platform.new_due_at` |
| 同一记录到期时间不变或记录消失 | 保持 `UNKNOWN`，不本地改写 `due_at` |

## 失败恢复

- 借阅分页不完整、重复或与官方表格不一致时，准备与核对都失败关闭，不使用部分列表。
- 预检精确比较业务摘要（含借阅标识、到期时间、资格提示、Primo 账号）；变化即
  `PRECONDITIONS_CHANGED_PREPARE_AGAIN`。
- 续借没有反向操作：平台不支持撤销时保持“不可回滚”，不虚构取消接口。
- `THU_LIBRARY_RENEW=0` 关闭新派发，但已存在的 `UNKNOWN` 计划仍可只读核对。

## 打包

本 README 目前只随源码树提供；发行包 `package-data` 由 6.3 统一补齐，5.1 不修改
`pyproject.toml`。
