# 校园来源矩阵

数据源：[contracts/v0.2/sources/source-registry.json](../../contracts/v0.2/sources/source-registry.json)。本表是摘要，字段、分页、错误码和未决项以登记表为准。

## 读来源

| 来源 | 访问边界 | 代码状态 | 证据 | 规范落点或限制 |
|---|---|---|---|---|
| `info.directory` | 统一认证后的信息门户 | 已实现 | `OFFLINE_CONTRACT` | 目录条目映射到资源；目录命中不代表业务权限 |
| `learn.semesters` | 网络学堂会话 | 已实现 | `OFFLINE_CONTRACT` | 学期作为课程身份的一部分；空元素按上游兼容跳过 |
| `learn.courses` | 网络学堂会话 | 已实现 | `OFFLINE_CONTRACT` | `course-v1`；同名课程按稳定 ID 区分 |
| `learn.notices` | 网络学堂会话 | 已实现 | `OFFLINE_CONTRACT` | 暂无规范通知模型，正文和附件提示保留来源字段 |
| `learn.assignments` | 网络学堂会话 | 已实现 | `OFFLINE_CONTRACT` | `assignment-v1`；未提交/已提交/已批阅分列表读取 |
| `learn.course_files` | 网络学堂会话 | 已实现 | `OFFLINE_CONTRACT` | `resource-v1`、`attachment-v1`；未分类列表 200 行封顶 |
| `learn.calendar` | 网络学堂 SSO 到教务日历 | 已实现 | `OFFLINE_CONTRACT` | `calendar-event-v1`；仅选定的教学日历，不是完整课表或考试表 |
| `library.catalog_search` | 公开访客 Primo | 已实现 | `OFFLINE_CONTRACT` | `resource-v1`；结果是请求窗口，不是全馆完整目录 |
| `library.catalog_detail` | 公开访客 Primo | 已实现 | `OFFLINE_CONTRACT` | 馆藏/单册暂无规范模型；可见单册不等于可借 |
| `library.database_links` | 公开数据库导航 | 已实现 | `OFFLINE_CONTRACT` | `resource-v1`；入口登记不等于订阅或全文权限 |
| `library.loans` | 已认证 Primo 浏览器 | 已实现 | `OFFLINE_CONTRACT` | `order-v1`（`LIBRARY_LOAN`）；续借只有资格提示 |
| `library.seat_availability` | 已认证座位浏览器 | 已实现 | `OFFLINE_CONTRACT` | 座位目录/时段暂无规范模型；空闲不等于可预约 |
| `library.room_availability` | 已认证研讨间浏览器 | 已实现 | `OFFLINE_CONTRACT` | 房间规则是平台提示；仍需账号和官方页面核验 |
| `sports.scenes` | 体育官方前端会话 | 已实现 | `OFFLINE_CONTRACT` | 场景目录不是余量或订单 |
| `sports.availability` | 体育官方前端会话 | 已实现 | `OFFLINE_CONTRACT` | 场次/费用是报价，不是预约或付款回执 |
| `sports.orders` | 体育官方前端会话 | 已实现 | `OFFLINE_CONTRACT` | `order-v1`；审批、付款、退款分轴 |
| `madmodel.catalog` | 公开官方前端 | 已实现 | `OFFLINE_CONTRACT` | 模型目录；`supportImage` 只是目录标记 |
| `madmodel.generate` | 已认证上游代理 | 已实现 | `OFFLINE_CONTRACT` | 当前仅文本输入；图片协议未核对 |

## 写来源与边界

| 来源 | 访问边界 | 代码状态 | 证据 | 限制 |
|---|---|---|---|---|
| `library.seat_booking` | 已认证座位浏览器 | 已实现专用适配 | `SYNTHETIC_TRANSPORT` | 一次计划、一次派发、独立核对；真实事项需具体授权 |
| `library.room_booking` | 已认证研讨间浏览器 | 已实现专用适配 | `SYNTHETIC_TRANSPORT` | 成员和规则可能要求官方页面接手 |
| `sports.booking` | 体育官方前端 | 已实现专用适配 | `SYNTHETIC_TRANSPORT` | 签名 URL 绑定一次派发；付款以结算页为准 |
| `sports.cancel` | 体育官方前端 | 已实现专用适配 | `SYNTHETIC_TRANSPORT` | 取消、退款、审批分别核对 |

## 计划中或执行阻塞

| 来源 | 目标模块 | 当前结论 | 原因 |
|---|---:|---|---|
| `academics.timetable` | 4.1 | `BLOCKED_SOURCE_EVIDENCE` | 没有已验证的个人课表来源；教学日历不能代替 |
| `academics.exams` | 4.1 | `BLOCKED_SOURCE_EVIDENCE` | 没有考试安排来源；不能从教学日历推导 |
| `academics.grades` | 4.1 | `BLOCKED_SOURCE_EVIDENCE` | 没有课程成绩来源；作业成绩不是课程成绩 |
| `learn.submission` | 5.3 | `BLOCKED_SOURCE_EVIDENCE` | 上传、草稿、正式提交和结果查询均无已验证接口 |
| `library.renewal` | 5.1 | `PARTIAL`；5.1 执行 `BLOCKED_SOURCE_EVIDENCE` | 资格读取、单笔绑定、官方页面移交和新到期核对已实现；没有已验证的请求端点与回执 |
| `model.image_input` | 4.5 | `BLOCKED_SOURCE_EVIDENCE` | 目录标记支持图片，但当前代理的图片协议未验证 |
| `visitors.form` | 4.4 | `BLOCKED_SOURCE_EVIDENCE` | 只有公开入口，没有当前表单和权限契约 |

说明：`library.renewal` 的资格提示是 `PARTIAL/LIMITED`，上表的阻塞结论只针对 5.1 的续借执行，不是整个来源不可用；计数时应把“部分可用”和“完全阻塞”分开。

## 已澄清与仍保留的说明/实现差异

以下条目记录旧说明与当前代码的差异；已处理项写明确认结果，未处理项保留为候选边界。不能把旧说明当成当前能力结论。

| 位置 | 旧说明或表面行为 | 当前代码事实 | 状态 |
|---|---|---|---|
| `src/thu_assistant/actions.py` 顶部 | 早期 docstring 写“No campus write adapter is enabled in block 5” | docstring 已改为已注册的图书馆座位/研讨间与体育创建、取消，并保留“真实事项需当次授权”的边界 | 8.1 已更正 |
| `src/learn_assistant/core.py` 的 `prepare_submission` | 2.3 的 `prepare` 边界写“只写本地加密计划” | 5.3 的门面路径使用受控的提交计划；旧 v0.1 兼容方法只在 `system_data_root/submission-previews/` 写用户本人可读（目录 0700、文件 0600）的明文摘要，返回值只含文件 `name`/`bytes`/`sha256` 与相对 `preview` 名，不含本地绝对路径；不上传、不提交 | 已按总审计 P1-4 收紧：路径不外泄、权限仅本人 |
| `learn.calendar` 的 `fl` | 旧事件字典把 `fl` 放进 `status` | 统一事件按 `BUSINESS_STATUS` 映射，`fl` 不作为日历状态 | 5.4 已按来源字段处理 |
| `library.catalog_detail` 的 `borrowable` | 表面是布尔值 | 不可借时为 `False`，否则为 `None`，并带 `borrowable_state=UNVERIFIED` | 4.3 保留三态，未写成可借 |
| `sports.availability` 的 `available`/费用 | 表面是“可预约/价格” | 只是余量与报价，不是预约或付款回执 | 4.3/5.2 保留来源证据 |
| `docs/CAPABILITIES.md` 的图书馆/体育办理 | 早期写“已实现专用适配与模拟验证” | 矩阵与工具表按代码实现、运行环境、真实证据分列，真实交易仍未验收 | 7.3 已统一措辞，8.1 复核 |

## 使用规则

1. 表格中的 `OFFLINE_CONTRACT` 只说明代码和结构可核对，不说明真实站点当前可用。
2. 读取来源时保留 `status`、`source`、`checked_at`、`source_complete`、`complete`、`warnings`、`missing_pages`；不得只返回一个数组。
3. 真实空结果必须同时满足 `state=EMPTY`、`source_complete=true`、`complete=true` 且行数组为空。
4. 第一页为空但上游仍有下一页时，保留 `PARTIAL`/`UNKNOWN` 和缺失页，不折叠成空。
5. 登录页 HTTP 200、目录命中、单册可见、座位空闲、费用报价、模型目录标记都不等于业务成功。
6. 需要新增来源时，先登记 `source_id`、访问边界、字段映射、分页规则和未决问题；不得从第三方教程直接写生产适配。
