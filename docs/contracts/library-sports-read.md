# 图书馆与体育只读操作矩阵

状态：4.3 模块交付（2026-11-17）。适用于 v0.2.0 候选。证据等级是 `OFFLINE_CONTRACT` 与
`SYNTHETIC_TRANSPORT`，不包含真实校园读取、预约、付款或退款结果。

本文件说明现有 `library`、`spaces`、`sports` 只读适配器在 v0.2 中的统一预检对象、分页边界
和退化行为。旧操作名、参数和结果字段保持兼容；下列新增字段都是可选的，旧客户端可以忽略。

## 1. 只读操作矩阵

| 服务 | 操作 | 选择器 | 实际来源范围 | 分页/窗口 |
|---|---|---|---|---|
| library | `catalog_search` | `query` + `field` + `scope` | Primo 公共检索 `listData` | 上游 `offset`/`limit`；`next_offset` 是下一来源窗口 |
| library | `catalog_detail` | `record_id` | Primo 详情、馆藏、单册 | 一次详情；馆藏/单册有读取预算 |
| library | `database_links` | `query` 或 `database_id`，二者不可混用 | 数据库导航 | `offset`/`limit`；无选择器时返回已登记入口 |
| library | `loans` | 本人会话 | 已认证借阅页 + 借阅接口 | 固定 10 条/请求；`showmore` 表示还有下一页 |
| library | `seat_areas` | `parent_id`/`query` | 座位区域树 | 全目录快照 |
| library | `seat_availability` | `area_id`（优先）或唯一 `name` | 座位目录、可选日期、平台完整时段、座位状态 | 只接受平台给出的完整时段 |
| library | `room_types` | `query` | 研讨间分类 | 全目录快照 |
| library | `room_availability` | `kind_id`（优先）或唯一 `name` | 分类下房间列表、开放时段与占用交集 | 上游 `page`/`limit`；`next_page` 是下一来源页 |
| library | `reservations` | `system=seat|room` | 本人座位表或研讨间记录 | 座位固定 20/页；研讨间默认状态位 6，页大小可设 |
| sports | `scenes` | `query` | 体育项目目录 | 全目录快照 |
| sports | `availability` | `scene_id`/唯一 `name`，再 `room_id`，再 `page`/`limit` | 场景→地点树→房间→场地→场次 | 每次只读一个房间、一个日期、一种模式的一页场次 |
| sports | `orders` | 本人会话 | 本人预约记录 | `page`/`limit`；`next_page` 是下一来源页 |
| sports | `order_detail` | `record_id` | 全部本人记录页 + 详情 | 逐页读取，最多 50 页，每页 50 条 |

`name` 只用于选择唯一资源。同名资源、同名区域或同名项目返回候选，不按页面顺序或标题
相似度自动选择。稳定编号优先，所有候选只包含编号、名称、路径和容量等公开字段。

## 2. 统一预检对象

每个 `library`/`sports` 只读结果新增顶层 `precheck`（版本 `library-sports-precheck-v1`）。列表
型操作同时为相关条目附加 `item.precheck`（座位、房间、场地、场次、订单、借阅、单册）。
它把下面五组事实分开，任何一组未知都保留 `UNKNOWN`，不会用另一组的成功值补齐。

| 字段 | 含义 |
|---|---|
| `selection` | `state`（`UNIQUE`/`AMBIGUOUS`/`NEEDS_USER_ACTION`/`NOT_FOUND`/`CANDIDATES`/`ALL`）、`by`（`ID`/`NAME`/`NONE`）、`candidate_count`/`candidate_total`/`candidates_truncated` 和候选摘要；按稳定 ID 读取的单记录为 `UNIQUE` |
| `resource` | 平台稳定编号、名称、路径；服务级稳定 UID（`library.catalog`/`library.spaces`/`library.loans`/`sports.venue`/`sports.orders`）和 `identity_state`；容量对象 |
| `window` | 日期、开始/结束时刻、时区；`precision` 为 `MINUTE`/`DATE`/`UNKNOWN`，日期级值不补午夜；条目预检继承摘要窗口 |
| `availability` | `state`、`free_count`、`capacity`、`capacity_scope`、`capacity_is_slot_availability`、`source_complete`；座位另含 `row_count`/`mapped_seat_count`/`unknown_seat_count` |
| `eligibility` | `state`（`VERIFIED`/`UNVERIFIED`/`INELIGIBLE`/`UNKNOWN`；借阅续借为 `ELIGIBLE`/`NOT_ELIGIBLE`/`UNKNOWN`）、原因；续借时另含 `renewal` |
| `fees` | `state`（`UNKNOWN`/`QUOTED`/`PAID`/`SEE_SESSIONS`/`NOT_APPLICABLE`）与 `items`（分、展示值、币种、原字段、来源、检查时间） |
| `rules` | `state`（`DECLARED`/`MISSING`/`PARTIAL`/`PER_SESSION`/`NOT_APPLICABLE`）与逐字段规则项（原字段、值、单位、来源、检查时间） |
| `unknowns` | 后续准备仍需补查的项目，例如 `ACCOUNT_ELIGIBILITY`、`FEE_STATE` |
| `preparation_state` | `READY_FOR_CONFIRMATION`/`NEEDS_ELIGIBILITY_CHECK`/`NEEDS_RULE_CHECK`/`NEEDS_USER_ACTION`/`NEEDS_OFFICIAL_PAGE`/`NOT_PREPARABLE`/`UNKNOWN`/`NOT_APPLICABLE`/`SEE_ITEMS` |

`availability.capacity_is_slot_availability` 明确区分两种数值：

- 座位区域 `capacity` 来自 `seatinfo.TotalCount`，是区域总座位数，`capacity_scope=AREA_TOTAL_SEATS`；
  `free_count` 只统计本次时段中**已映射为可用**的座位。区域有 300 个座位而目标时段全满时，结果是
  `capacity=300, free_count=0, state=FULL`，不能读成“还有 300 个可用”。座位列表为空时是
  `state=UNKNOWN`/`reason=NO_SEAT_ROWS`，未知状态码时是 `PARTIAL`/`UNKNOWN_SEAT_STATUS`，
  都不会被报成 `FULL` 或 `NOT_PREPARABLE`。
- 体育场馆没有场次级容量时，顶层容量是 `null`，`capacity_scope=SITE_SESSION_LIST_NOT_VENUE_CAPACITY`；
  只有场次对象的 `capacity`（`allowUserNum`，单位 `PERSON`）才可能表示该场次人数上限。
- 研讨间的 `people_limit` 是平台 `minUser`/`maxUser` 的规则值，不是空闲容量；空闲与人数不符
  可以同时成立：`availability.state=FREE`、`eligibility.state=INELIGIBLE`、
  `preparation_state=NOT_PREPARABLE`。

## 3. 规则单位

| 来源字段 | 单位 | 说明 |
|---|---|---|
| `minResvTime`、`maxResvTime`、`earliestResvTime`、`latestResvTime`、`timeInterval` | `MINUTE` | 研讨间时长与窗口 |
| `cancelTime`（研讨间） | `MINUTE` | 平台取消提前量 |
| `cancelBeforeStart`（体育） | `MINUTE` | 体育取消提前量 |
| `cancelTime`、`deadlineTime`（体育） | `UNKNOWN` | 可能是时刻或分钟，字段含义不唯一，不推算 |
| `chargingUnitPrice`、`payableAmount`、`paidAmount`、`totalAmount` | `FEN` | 只接受非负整数；展示值用整数除法精确到分 |
| `allowUserNum` | `PERSON` | 场次人数上限 |
| `minUser`、`maxUser` | `PERSON` | 研讨间人数范围 |
| `limit` | `COUNT` | 平台次数限制 |
| `renew` | `FLAG` | 仅续借资格提示，不是执行结果 |
| `ska.bookRule`、`renewstatuses.renewstatus`、`policy` | `TEXT` | 原文保留，不解析成期限常量 |

缺失规则字段是 `state=MISSING`，不是 0、1440 或“无限制”。类型不符是 `TYPE_MISMATCH`；
单位未登记或含糊是 `UNIT_UNREGISTERED`/`UNIT_AMBIGUOUS`，值保持 `null`。每条规则都带
`source` 和 `checked_at`，只表示在该时刻读到该声明，不表示学校政策已经解释完成。

## 4. 分页与完整性

- 新查询每次都读取当前来源；`retrieval.freshness=LIVE_AT_CHECKED_AT`，`policy` 为
  `LIVE_ONLY`/`LIVE_READ_ONLY`/`HISTORY_FALLBACK_ALLOWED`。游标续页只返回原快照，
  顶层和 `retrieval` 的 `freshness` 都是 `SNAPSHOT_ONLY_NOT_LIVE`，`current_source_read=false`、
  `reused_snapshot=true`，`checked_at` 不变。无选择器的数据库入口是 `STATIC_ENTRY_LIST`，
  `current_source_read=false`；`order_detail` 是实时逐页读取，策略为 `LIVE_ONLY`。
- 体育订单和本人记录逐页核对 `upstream_total`。重复页、缺页或总数变化时保留已读行，
  `complete=false`，并在 `next_page` 给出应继续（或重读）的页号；不会把部分结果当成完整列表。
  体育余量页也使用同一宽松分页，短页/重复页返回部分结果而不是直接报错。
- 座位历史固定 20 条/页；研讨间默认 `needStatus=6`（待审核/生效）；体育订单页大小最大 50。
  这些窗口都写入结果的 `pagination`，并同步到 `read_scope` 的 `offset`/`limit`/`pages_read`。
- 真实空结果只在 `status=EMPTY` 且 `source_complete=true`、`complete=true` 时成立。登录页、
  维护、限流、缺页、重复页和未知业务状态都不会变成空数组。

## 5. 退化与兼容

| 情况 | 行为 |
|---|---|
| 401/403 | `AUTH_REQUIRED`/`FORBIDDEN`，不返回空列表 |
| 429 | `RATE_LIMITED`，有 `Retry-After` 时附 `retry_after_ms` |
| 503 或明确的维护文案 | `MAINTENANCE`，与解析失败区分 |
| 结构改版/字段缺失 | 对应的 `*_RESPONSE_INVALID`/`*_PAGINATION_CHANGED`，不猜字段 |
| 座位 jQuery `parsererror` | `SPACE_RESPONSE_INVALID`，保留 `BROWSER_REQUIRED` 官方页面降级 |
| 研讨间时长规则缺失 | `interval_state=RULE_UNVERIFIED`，准备阶段得到 `ROOM_RULE_UNVERIFIED`（已登记为 `UPSTREAM_CONTRACT`），不当作无限制或未开放 |
| 同名资源 | `NEEDS_USER_ACTION` + 候选，不自动选择 |
| 规则缺失/单位含糊 | 预检 `MISSING`/`UNKNOWN`，准备状态要求补查 |
| 本人记录部分读取 | 保留已读行、`complete=false`、续页信息 |
| 电子资源 | 只证明官方入口已登记；机构授权、校外访问和第三方条款保持 `UNVERIFIED` |
| 其他人物业 | 公开预览只保留 `start_time`/`end_time`/`platform_status`，去除身份和主题 |

回退时可以按服务关闭新版解析并保留旧只读入口；已经发现的错误映射不得恢复为默认。
历史快照保留原检查时间和版本，回退后不能把上次余量展示成当前可用。

## 6. 交给第五批的契约

第五批办理模块可以只消费 `precheck`，不必重新解释平台字段：

1. 用 `selection` 确认唯一资源；`AMBIGUOUS` 必须回到用户选择。
2. 用 `availability` 判断时段状态，用 `eligibility` 判断账号/人数/资格，两者都满足才准备。
3. 用 `fees.items[].amount_fen` 和原始字段核对费用，禁止用展示字符串反推金额。
4. 用 `rules.items` 的字段、单位和检查时间重新核对取消期限；`MISSING` 不等于无限制。
5. 用 `retrieval` 区分本次读取与旧快照；旧快照不能作为办理前最后核对。
6. `preparation_state` 不是授权，也不代表平台会接受请求；真实写入仍需独立计划和授权。
