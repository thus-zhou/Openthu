# MCP 工具与登记操作对照

本页把当前 29 个 MCP 工具和统一业务门面（`business-facade-v1`）的登记操作放在同一张表里，供客户端作者、技能说明和故障排查共用。工具名与数量取自 `contracts/v0.2/compatibility-baseline.json`；操作名取自运行中的能力登记表，`openthu describe --service <id>` 输出同样的名称。

v0.2 保留全部 29 个工具，不新增重复工具。新能力优先通过已有工具的参数和登记操作暴露：

- 只读能力用 `query_service(service_id, operation, parameters, cursor, page_size)`。
- 办理用 `prepare_service_action` → `execute_service_action` → `get_action_result`。
- 本地派生读取用 `query_service(service_id="local", operation="agenda" | "agenda_changes")`。
- 模型图片输入复用 `model_chat` 的 `images` 参数，不新增工具。

## 1. 兼容入口

| 入口 | 包/名称 | 说明 |
|---|---|---|
| `openthu` | `thu_assistant.__main__` | 当前命令行；保留 `services`、`discover`、`login`、`read`、`courses`、`model`，新增 `describe`、`health`、`query`、`agenda`、`dashboard` |
| `openthu-mcp` | `thu_assistant.mcp_stdio` | 当前 MCP 服务；协议标识保留 `tsinghua-assistant` |
| `tsinghua-assistant` | 同 `openthu` | 旧命令名，参数兼容 |
| `tsinghua-assistant-mcp` | 同 `openthu-mcp` | 旧 MCP 命令名 |
| `learn-assistant`、`learn-assistant-mcp` | `learn_assistant` | 网络学堂兼容命令与旧 MCP；`serverInfo` 与当前版本同源 |
| `openthu-doctor` | `thu_assistant.doctor` | 安装后自检 |

Python 导入路径 `thu_assistant`、`learn_assistant` 都保留；MCP 工具 schema 不删除字段、不改字段含义，新增字段一律可选。

## 2. 29 个工具

| # | 工具 | 作用 | 对应的登记操作或本地入口 |
|---:|---|---|---|
| 1 | `list_courses` | 列出课程 | 兼容读取；结构化等价 `learn.courses` |
| 2 | `list_assignments` | 列出作业 | 兼容读取；`learn.assignments` |
| 3 | `get_assignment` | 单个作业要求与截止时间 | 兼容读取；`learn.assignment_detail` |
| 4 | `list_course_files` | 列出课程文件 | 兼容读取；`learn.files` |
| 5 | `download_files` | 下载课程文件到明确目录 | 本地文件写入；不登记为校园读操作 |
| 6 | `prepare_submission` | 校验本地待提交文件并生成摘要 | 只做本地校验；`learn.submit_assignment` 阻塞，不上传、不提交 |
| 7 | `login` | 官方统一认证 | 认证入口；不是任何 `query_service` 操作 |
| 8 | `get_login_status` | 网络学堂业务会话状态 | 认证状态；不等于所有服务已登录 |
| 9 | `list_services` | 静态校园服务目录 | `info.directory` 的本地目录视图 |
| 10 | `refresh_service_directory` | 刷新 info 门户应用目录 | 本地目录刷新；不逐站登录 |
| 11 | `describe_service` | 服务的能力登记与阻塞原因 | 门面 `describe` / `describe_service` |
| 12 | `query_service` | 执行登记过的只读查询 | `*.page`、`*.status`、`learn.*`、`library.*`、`sports.*`、`local.agenda`、`local.agenda_changes`、`local.resources` |
| 13 | `prepare_service_action` | 生成办理预览与加密计划 | `library.create_seat_reservation`、`library.create_room_reservation`、`library.cancel_reservation`、`library.renew_loan`、`sports.create_reservation`、`sports.cancel_reservation` |
| 14 | `execute_service_action` | 执行当次授权的计划 | 同上；`learn.submit_assignment` 被阻塞，`renew_loan` 只移交官方页面 |
| 15 | `get_action_result` | 核对事务状态，不重发 | 全部办理操作 |
| 16 | `read_page` | 读取清华 HTTPS 页面或 JSON | `*.page` |
| 17 | `download_campus_file` | 下载指定 HTTPS 文件 | 本地文件写入；拒绝覆盖与凭据目录 |
| 18 | `get_service_status` | 读取服务入口实际状态 | `*.status` |
| 19 | `list_models` | 学校模型目录与能力标注 | 模型目录；标注不等于可生成 |
| 20 | `get_todos` | 未来七天作业、借阅、预约汇总 | 门面 `get_todos`；本地日程的另一入口 |
| 21 | `model_chat` | 文本对话；可选 `images` 媒体引用 | 模型请求；图片输入当前 `MODEL_IMAGE_INPUT_UNAVAILABLE` |
| 22 | `browser_open` | 打开受控浏览器 | 浏览器宿主；不代表提交成功 |
| 23 | `browser_snapshot` | 读取 iframe 正文与控件 | 浏览器宿主 |
| 24 | `browser_scroll` | 滚动并加载后续内容 | 浏览器宿主 |
| 25 | `browser_fill` | 填写普通字段，不提交 | 浏览器宿主；密码与验证码走官方流程 |
| 26 | `browser_click` | 点击普通导航/查询控件 | 浏览器宿主；提交类控件需先预览 |
| 27 | `prepare_browser_action` | 生成一次性表单预览 | 浏览器宿主；绑定字段与目标 |
| 28 | `execute_browser_action` | 执行已预览的表单 | 浏览器宿主；无站点契约返回 `ADAPTER_REQUIRED` |
| 29 | `browser_close` | 关闭本助手创建的浏览器 | 浏览器宿主 |

## 3. 登记操作清单

`query_service` 的 `service_id` 与 `operation` 必须来自下表。完整参数、分页方式和阻塞原因用 `openthu describe --service <id>` 或 `describe_service` 读取；`openthu health` 只报告本地状态。

### 3.1 校园只读

| 服务 | 操作 | 说明 |
|---|---|---|
| `info` | `directory`、`page`、`status` | 门户目录与页面；目录命中不等于权限 |
| `learn` | `semesters`、`courses`、`notices`、`notice_detail`、`assignments`、`assignment_detail`、`files`、`calendar`、`page`、`status` | 列表与正文分开；`calendar` 只是教学日历，不是完整课表或考试表 |
| `library` | `catalog_search`、`catalog_detail`、`database_links`、`loans`、`seat_areas`、`seat_availability`、`room_types`、`room_availability`、`reservations`、`page`、`status` | 检索是请求窗口；`borrowable=null` 表示未核验；空闲不等于可预约 |
| `sports` | `scenes`、`availability`、`orders`、`order_detail`、`page`、`status` | 余量与报价不是预约或付款回执 |
| `campus`、`campusvisit`、`cloud`、`student`、`thos`、`visitors`、`webvpn`、`madmodel`、`library-catalog`、`library-databases`、`library-offcampus`、`library-rooms`、`library-search`、`library-seat` | `page`、`status` | 页面与状态读取；HTTP 200 不等于登录成功 |
| `academics` | `timetable`、`exams`、`grades` | 全部 `BLOCKED_SOURCE_EVIDENCE`，当前不读取 |

### 3.2 本地派生

| 服务 | 操作 | 说明 |
|---|---|---|
| `local` | `agenda` | 汇总已登记来源；不轮询、不发消息 |
| `local` | `agenda_changes` | 增量基线游标 `change_cursor`，进程内 600 秒 |
| `local` | `resources` | 需要资料库提供者；未注入时 `RESOURCE_PROVIDER_UNAVAILABLE` |

`local.media_prepare`、`local.resources_export` 只在宿主 API 可用，没有 CLI/MCP 入口；`local.model_image_input` 当前阻塞。

### 3.3 校园写入

| 服务 | 操作 | 当前状态 |
|---|---|---|
| `library` | `create_seat_reservation`、`create_room_reservation`、`cancel_reservation` | 可用；需要可信宿主授权与当次事项 |
| `library` | `renew_loan` | 阻塞；可准备并移交官方页面 |
| `sports` | `create_reservation`、`cancel_reservation` | 可用；付款与退款分别核对 |
| `learn` | `submit_assignment` | 阻塞；写开关 `THU_LEARN_SUBMIT` 默认关闭 |
| `visitors`、`library-seat`、`library-rooms`、`sports` | 旧的 `create`、`cancel` | 兼容框架保留，`ADAPTER_REQUIRED`，不要调用 |

## 4. 名称一致性规则

1. 页面、CLI 帮助、技能说明和能力矩阵使用同一组名称：服务 ID 小写连字符，操作名小写下划线。
2. 新操作只在统一门面登记一次，不在 MCP 层另起同名工具。
3. 旧工具名与旧服务 ID 不删除、不改含义；新增字段保持可选。
4. `describe_service` 的 `state`/`reason` 是唯一的能力结论来源，文档不得自行推断“支持”。
5. 历史名称（如 `library-seat` 的 `create`）只用于解释旧客户端行为，不作为新调用的入口。
