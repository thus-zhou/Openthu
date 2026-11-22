# 能力与验证状态

v0.2.0 候选（未发布）｜编制：7.3｜事实来源：`business-facade-v1` 能力登记表、`contracts/v0.2/`、各模块交付记录。

本表以**登记操作**为主键，回答四个不同的问题，不合并成一句“支持”：

| 维度 | 本表列 | 含义 |
|---|---|---|
| 代码已实现 | 实现与限制 | 仓库里有对应实现；合成测试通过只证明这条路径 |
| 环境已就绪 | 环境就绪 | 当前平台的密钥、会话、Node、浏览器与看板条件 |
| 协议有依据 | 状态 + 登记证据 | 参数、分页、错误语义已登记；不代表站点当前接受 |
| 真实业务有证据 | 真实业务证据 | 只在明确账号、日期和事项下成立，不外推 |

状态列直接使用运行中登记表的取值：

- `AVAILABLE`：当前默认条件下已登记且可调用。
- `DISABLED`：实现存在，但开关关闭。
- `BLOCKED`：实现或部分实现存在，来源契约/证据不足，不能执行。
- `MISSING`：没有提供者、没有公开入口或没有适配。

登记证据取值：`REGISTERED_LOCAL_QUERY`（本地登记的只读分派）、`VERIFIED_ADAPTER`（已注册的专用适配，合成传输验证）、`CONTRACT_ONLY`（只有契约或准备路径）、`BLOCKED_SOURCE_EVIDENCE`（来源证据阻塞）、`NO_VERIFIED_PROTOCOL`（协议未核对）、`NO_PUBLIC_ENTRY`（仅宿主 API）、`NO_PROVIDER`（提供者缺失）、`SWITCHED_OFF`（写开关关闭，对应 `DISABLED`）、`LOCAL_DERIVED_READ`（本地派生）、`LOCAL_INDEX_READ`（本地索引）。

`openthu describe` 与 `openthu health` 输出与下表同源；`health` 不探测真实站点，`PARTIAL` 只表示有已登记但被阻塞的操作。

## 1. 校园只读

| 登记 | 类别 | 归属 | 状态 | 登记证据 | 实现与限制 |
|---|---|---|---|---|---|
| `campus.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；校园入口页面；页面或入口状态不证明登录或权限。 |
| `campusvisit.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；入校参观入口；页面或入口状态不证明登录或权限。 |
| `campusvisit.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；入校参观入口；页面或入口状态不证明登录或权限。 |
| `cloud.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；云盘入口；页面或入口状态不证明登录或权限。 |
| `cloud.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；云盘入口；页面或入口状态不证明登录或权限。 |
| `info.directory` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；门户应用目录；目录命中不代表登录、权限或业务可用。 |
| `info.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；页面读取；HTTP 200 不是登录成功的证据。 |
| `info.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；入口状态；不探测更深权限。 |
| `learn.assignment_detail` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；单个作业要求、截止时间与提交约束；不执行提交。 |
| `learn.assignments` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；作业列表；未提交、已提交、已批阅分列表。 |
| `learn.calendar` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；教学日历事件；不是完整课表或考试表。 |
| `learn.courses` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；课程列表；同名课程按稳定 ID 区分。 |
| `learn.files` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；课程文件元数据；下载是单独入口。 |
| `learn.notice_detail` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；公告正文与附件分别报告读取状态。 |
| `learn.notices` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；公告列表；标题不是全文，附件需单独读取。 |
| `learn.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `learn.semesters` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；学期 ID 与当前学期标记；学期是课程身份的一部分。 |
| `learn.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `library.catalog_detail` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；书目与单册；单册可见不等于可借，`borrowable=null` 表示未核验。 |
| `library.catalog_search` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；访客检索窗口；不是全馆完整目录。 |
| `library.database_links` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；数据库入口登记；不等于订阅或全文权限。 |
| `library.loans` | READ | 5.1 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；本人借阅；续借只有资格提示，不派发续借请求。 |
| `library.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `library.reservations` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；本人预约记录；取消、审批与生效状态分轴。 |
| `library.room_availability` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；占用交集与规则提示；仍需账号与官方页面核验。 |
| `library.room_types` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；空间类别；不是可约状态。 |
| `library.seat_areas` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；馆区/楼层目录；容量不是当前余量。 |
| `library.seat_availability` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；平台时段余量；空闲不等于本人可预约。 |
| `library.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `library-catalog.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；图书馆检索入口；页面或入口状态不证明登录或权限。 |
| `library-catalog.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；图书馆检索入口；页面或入口状态不证明登录或权限。 |
| `library-databases.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；数据库导航入口；页面或入口状态不证明登录或权限。 |
| `library-databases.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；数据库导航入口；页面或入口状态不证明登录或权限。 |
| `library-offcampus.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；校外访问入口；页面或入口状态不证明登录或权限。 |
| `library-offcampus.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；校外访问入口；页面或入口状态不证明登录或权限。 |
| `library-rooms.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；研讨间页面；页面或入口状态不证明登录或权限。 |
| `library-rooms.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；研讨间页面；页面或入口状态不证明登录或权限。 |
| `library-search.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；水木搜索入口；页面或入口状态不证明登录或权限。 |
| `library-search.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；水木搜索入口；页面或入口状态不证明登录或权限。 |
| `library-seat.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；座位页面；页面或入口状态不证明登录或权限。 |
| `library-seat.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；座位页面；页面或入口状态不证明登录或权限。 |
| `madmodel.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；学校模型前端入口；页面或入口状态不证明登录或权限。 |
| `madmodel.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；学校模型前端入口；页面或入口状态不证明登录或权限。 |
| `sports.availability` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；场次与费用报价；不是预约或付款回执。 |
| `sports.order_detail` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；单笔订单详情；读取不改变状态。 |
| `sports.orders` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；本人订单；审批、付款、退款分轴。 |
| `sports.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `sports.scenes` | READ | 4.3 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；场景目录；不是余量或订单。 |
| `sports.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；服务入口；页面或入口状态不证明登录或权限。 |
| `student.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；学生服务入口；页面或入口状态不证明登录或权限。 |
| `student.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；学生服务入口；页面或入口状态不证明登录或权限。 |
| `thos.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；清华在线服务入口；页面或入口状态不证明登录或权限。 |
| `thos.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；清华在线服务入口；页面或入口状态不证明登录或权限。 |
| `visitors.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；亲友入校公开入口；页面或入口状态不证明登录或权限。 |
| `visitors.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；亲友入校公开入口；页面或入口状态不证明登录或权限。 |
| `webvpn.page` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；WebVPN 入口；页面或入口状态不证明登录或权限。 |
| `webvpn.status` | READ | 3.2 | AVAILABLE | REGISTERED_LOCAL_QUERY | 只读查询；WebVPN 入口；页面或入口状态不证明登录或权限。 |

## 2. 学业查询（4.1）

| 登记 | 类别 | 归属 | 状态 | 登记证据 | 实现与限制 |
|---|---|---|---|---|---|
| `academics.exams` | READ | 4.1 | BLOCKED | BLOCKED_SOURCE_EVIDENCE | 模型与失败路径；考试安排；没有已验证来源，不能从教学日历推导。 |
| `academics.grades` | READ | 4.1 | BLOCKED | BLOCKED_SOURCE_EVIDENCE | 模型与失败路径；课程成绩；没有已验证来源，作业成绩不是课程成绩。 |
| `academics.timetable` | READ | 4.1 | BLOCKED | BLOCKED_SOURCE_EVIDENCE | 模型与失败路径；个人课表；没有已验证来源，教学日历不能代替。 |

## 3. 本地派生与模型

| 登记 | 类别 | 归属 | 状态 | 登记证据 | 实现与限制 |
|---|---|---|---|---|---|
| `local.agenda` | READ | 5.4 | AVAILABLE | LOCAL_DERIVED_READ | 本地派生读取；按需汇总已登记来源；不轮询、不发消息、不标记已送达。 |
| `local.agenda_changes` | READ | 5.4 | AVAILABLE | LOCAL_DERIVED_READ | 本地派生读取；增量变化；`change_cursor` 进程内 600 秒，来源不完整不产生假 `DELETED`。 |
| `local.media_prepare` | LOCAL_WRITE | 3.3 | MISSING | NO_PUBLIC_ENTRY | 仅宿主 API；3.3 媒体登记只在宿主 API；没有 CLI/MCP 入口。 |
| `local.model_image_input` | MODEL | 4.5 | BLOCKED | NO_VERIFIED_PROTOCOL | 管线已实现，协议未验证；图片输入；协议注册表为空，当前不可发送。 |
| `local.resources` | READ | 4.2 | MISSING | LOCAL_INDEX_READ | 本地索引读取；本地加密索引；未注入提供者时 `RESOURCE_PROVIDER_UNAVAILABLE`。 |
| `local.resources_export` | LOCAL_WRITE | 4.2 | MISSING | NO_PUBLIC_ENTRY | 仅宿主 API；4.2 导出只在宿主 API；没有 CLI/MCP 入口。 |

## 4. 校园写入

| 登记 | 类别 | 归属 | 状态 | 登记证据 | 实现与限制 |
|---|---|---|---|---|---|
| `learn.submit_assignment` | CAMPUS_WRITE | 5.3 | BLOCKED | BLOCKED_SOURCE_EVIDENCE | 仅本地文件校验；上传、草稿、正式提交与结果查询均无已验证接口；写开关默认关闭。 |
| `library.cancel_reservation` | CAMPUS_WRITE | 5.1 | AVAILABLE | VERIFIED_ADAPTER | 专用适配（合成传输）；取消；`system` 取 `seat` 或 `room`，先读本人记录再核对。 |
| `library.create_room_reservation` | CAMPUS_WRITE | 5.1 | AVAILABLE | VERIFIED_ADAPTER | 专用适配（合成传输）；研讨间预约；成员与规则可能要求在官方页面接手。 |
| `library.create_seat_reservation` | CAMPUS_WRITE | 5.1 | AVAILABLE | VERIFIED_ADAPTER | 专用适配（合成传输）；座位预约；专用适配，一次计划一次派发，需当次授权。 |
| `library.renew_loan` | CAMPUS_WRITE | 5.1 | BLOCKED | CONTRACT_ONLY | 资格读取 + 计划 + 官方页面移交；续借；只能准备并移交官方页面，执行阻塞。 |
| `library-rooms.cancel` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；旧服务名下的兼容契约；新调用用 `library.cancel_reservation`。 |
| `library-rooms.create` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；旧服务名下的兼容契约；新调用用 `library.create_room_reservation`。 |
| `library-seat.cancel` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；旧服务名下的兼容契约；新调用用 `library.cancel_reservation`。 |
| `library-seat.create` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；旧服务名下的兼容契约；新调用用 `library.create_seat_reservation`。 |
| `sports.cancel` | CAMPUS_WRITE | 5.2 | MISSING | NO_PROVIDER | 未注册适配；旧兼容契约，未注册适配（`ADAPTER_REQUIRED`）。 |
| `sports.cancel_reservation` | CAMPUS_WRITE | 5.2 | AVAILABLE | VERIFIED_ADAPTER | 专用适配（合成传输）；体育取消；退款与审批分别核对。 |
| `sports.create` | CAMPUS_WRITE | 5.2 | MISSING | NO_PROVIDER | 未注册适配；旧兼容契约，未注册适配（`ADAPTER_REQUIRED`）。 |
| `sports.create_reservation` | CAMPUS_WRITE | 5.2 | AVAILABLE | VERIFIED_ADAPTER | 专用适配（合成传输）；体育预约；签名 URL 绑定一次派发，付款以结算页为准。 |
| `visitors.cancel` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；亲友入校取消；没有当前表单契约。 |
| `visitors.create` | CAMPUS_WRITE | 4.4 | MISSING | NO_PROVIDER | 未注册适配；亲友入校；只有公开入口，没有当前表单契约。 |

写入的共同规则：`prepare` 只生成十分钟有效的加密计划，`execute` 只允许一次派发且需要可信宿主授权，`result` 只核对不重发，`cancel` 只取消本地排队或执行中的请求，不撤回校园业务。`PENDING_PAYMENT`、`PENDING_APPROVAL`、`UNKNOWN` 都不是成功。

## 5. 环境就绪

| 能力 | Windows 桌面 | 无桌面 Linux | 说明 |
|---|---|---|---|
| 本地加密存储 | 当前用户 DPAPI；旧 `LA-DPAPI-1`、`THU-ACTION-1`、`THU-TODOS-1` 只读保留 | 外部密钥文件 `0600` + 数据目录；两者分离，缺密钥拒绝写入 | 不自动生成密钥，不退化为明文；跨平台迁移预览标 `UNSUPPORTED_PLATFORM`，运行时读取得到 `SECURE_LEGACY_UNAVAILABLE` |
| 生产校园登录 | 官方统一认证 + MFA；再次验证时明确返回 | 未实现 | Linux 不能宣传“无条件全自动登录” |
| 静态服务目录与 CLI | 可用 | 可用 | `openthu services`、`describe`、`health`、`query` 的本地部分 |
| Node 网关 | 需要 Node 24 与锁文件依赖 | 同左 | 缺 Node 时只影响网关相关能力，诊断会指出 |
| 浏览器能力 | Playwright Chromium，可选 | 同左 | 缺 Chromium 只影响浏览器，不影响只读目录 |
| 本地看板 API | 回环 HTTP，可用 | 可用 | 只读路由；本地会话由 `0600` 引导文件建立 |
| 看板 HTML 外壳 | 随包安装，CLI 自动接线 | 同左 | `openthu dashboard` 的 `/` 提供只读页面并加载 `assets/`；静态目录缺失或不可读时为 `LOCAL_API_UI_NOT_INSTALLED`，JSON 路由仍可用 |
| 模型文本 | 需要本人学校额度与上游认证 | 同左（上游代理自带登录流程） | 目录、生成、取消分别记录证据；不批量探测生成 |
| 模型图片 | 管线已实现，协议未验证 | 同左 | 当前 `MODEL_IMAGE_INPUT_UNAVAILABLE`；工程上限见 §7 |

## 6. 真实业务证据

下表只记录历史事实，不自动成为本轮证据，也不证明当前站点仍可用。公开仓库不附账号、主机或原始响应。

| 事项 | 历史证据 | 本轮结论 |
|---|---|---|
| 服务目录、通用 HTTPS 读取 | 历史真实读取 | 只读路径保留；目录命中仍不代表权限 |
| CAS/SAML/WebVPN 与按站点会话 | 历史真实认证（Windows） | 当前用户需自行建立会话；Linux 无生产登录 |
| 课程、学期、公告、作业、课件、日历 | 历史真实只读 | 只读保留；作业提交未实现 |
| 馆藏、书目、单册、借阅、数据库说明 | 历史真实只读 | 只读保留；`borrowable` 三态 |
| 座位/研讨间余量与本人记录 | 历史真实查询 | 余量是当次窗口；空闲不等于可约 |
| 图书馆座位/研讨间创建与取消 | 专用适配 + 合成传输 | 真实事项待当次授权与验收 |
| 图书馆续借 | 资格读取、单笔计划、官方页面移交 | 执行阻塞；无已验证请求端点 |
| 体育场景、余量、订单、详情 | 历史真实只读 | 报价不是预约或付款回执 |
| 体育创建、取消 | 专用适配 + 合成传输 | 付款、退款、审批分别核对 |
| 模型目录、生成、取消 | 历史目录与少量生成/取消 | 本轮发行不调用模型；图片未接通 |
| 统一待办 | 历史真实比对 | 来源失败保留 PARTIAL |
| 本地图片与附件处理 | 离线合成验证 | 只处理本地文件；真实模型传输未接入 |
| 亲友入校 | 无 | 只有公开入口，无当前表单契约 |

## 7. 未登记或仅宿主 API 的能力

| 能力 | 状态 | 原因 | 登记证据 | 下一步 |
|---|---|---|---|---|
| `local.resources_export`（4.2 导出） | `MISSING` | `NO_PUBLIC_ENTRY` | `NO_PUBLIC_ENTRY` | 只在宿主 API 可用；没有 CLI/MCP 入口，不进入默认可用表 |
| `local.media_prepare`（3.3 媒体登记） | `MISSING` | `NO_PUBLIC_ENTRY` | `NO_PUBLIC_ENTRY` | 只在宿主 API 可用；没有 CLI/MCP 入口 |
| `local.model_image_input`（4.5） | `BLOCKED` | `MODEL_IMAGE_INPUT_UNAVAILABLE` | `NO_VERIFIED_PROTOCOL` | 公共路径未注入媒体管道；协议注册表为空；目录 `supportImage` 只是标注 |
| `local.resources`（4.2 索引） | `MISSING` | `RESOURCE_PROVIDER_UNAVAILABLE` | `LOCAL_INDEX_READ` | CLI/MCP/默认看板未注入提供者；宿主注入后可用 |
| `academics.timetable` / `exams` / `grades` | `BLOCKED` | `BLOCKED_SOURCE_EVIDENCE` | `BLOCKED_SOURCE_EVIDENCE` | 没有已验证的个人课表、考试、成绩来源；教学日历不能代替 |
| `learn.submit_assignment` | `BLOCKED` | `BLOCKED_SOURCE_EVIDENCE` | `BLOCKED_SOURCE_EVIDENCE` | 上传、草稿、正式提交、结果查询均无已验证接口；写开关 `THU_LEARN_SUBMIT` 默认关闭 |
| `visitors.create` / `cancel` | `MISSING` | `ADAPTER_REQUIRED` | `NO_PROVIDER` | 只有公开入口；通用浏览器看到表单不代表业务接通 |
| `sports.create` / `cancel`、`library-seat.create` / `cancel`、`library-rooms.create` / `cancel` | `MISSING` | `ADAPTER_REQUIRED` | `NO_PROVIDER` | 旧兼容契约保留；新调用使用 `sports.create_reservation` 等登记操作 |

图片与资料边界：单图 8 MiB、每次 4 图、合计 16 MiB、单图 1600 万像素（另有单边 20000 像素工程保护），只接受 JPEG/PNG/WebP；平台契约更严格时取较小值。已登记协议档案默认更保守（1 图 / 1 MiB / 合计 4 MiB / 400 万像素），要用满上限必须显式声明。任何路径都不会隐式发送未选择的图片或用户文件。

## 8. 文字规则

| 状态 | 可以写 | 不能写 |
|---|---|---|
| `OK` | 本次范围读取完成，附来源与时间 | “全部数据已获取” |
| `EMPTY` | 已证明完整且为空，附范围 | “没有作业/没有借阅”而不给范围 |
| `PARTIAL` | 已读部分、缺失页与原因 | “为空”“已完成”“稍后自动补齐” |
| `UNKNOWN` | 结果未知，需要核对本人记录 | 成功、失败或已取消 |
| `PENDING_PAYMENT` | 待付款，以官方结算页为准 | 已预约成功 |
| `PENDING_APPROVAL` | 待审批/待成员同意 | 已通过 |
| `BLOCKED_SOURCE_EVIDENCE` | 来源证据不足，保持只读 | “已支持，等待上线” |
| `ADAPTER_REQUIRED` | 没有登记的站点契约 | “通用表单可以提交” |

安装成功、模拟测试通过、HTTP 200、目录命中、单册可见、座位空闲、费用报价、模型目录标注，都不提升为业务成功。

## 9. 维护方式

本表的状态与证据列由 `tests/test_docs_consistency.py` 与运行中登记表逐项比对：新增、删除或改名一个登记操作而不同步本表，测试失败。修改能力措辞时保留历史证据的时间与语境，不用新结论改写旧记录。
