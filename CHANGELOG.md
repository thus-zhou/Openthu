# 更新记录

## 0.2.0 — 候选（未发布）

状态：完整版本的候选，尚未正式发布。已发布版本仍是 0.1.0。真实校园写入、图片传输和部分来源仍未取得生产证据，能力状态见 [docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

### 用户可见变化

- 新增统一业务门面与命令行子命令：`openthu describe`、`health`、`query`、`agenda`、`dashboard`。旧的 `services`、`discover`、`login`、`read`、`courses`、`model` 调用和参数继续可用。
- 只读查询按登记操作执行：`query --service <id> --operation <op>` 支持参数文件、游标和分页；每次结果都带来源、检查时间、完整性和证据级别。
- 本地只读看板：只绑定回环地址，只提供 `/api/v1/health`、`/services`、`/services/{service_id}`、`/query`、`/todos`、`/calendar`、`/resources`；`dashboard --check` 可先验证启动，再常驻。
- 统一日程：按需汇总作业、借阅、图书馆预约和体育订单，支持日期窗口、来源筛选、已完成/逾期视图与冲突提示；日期级与精确时刻分开建模。
- 日程增量：`agenda_changes` 输出新增、修改、取消、删除、来源不可用和来源不完整；来源失败时保留历史事件，不产生假删除。
- 日历导出：`openthu agenda --export` 写出 ICS、Markdown 或 JSON，必须使用绝对路径；默认拒绝覆盖，结果不完整时默认拒绝导出。
- 课程资料中心：按账号与学期隔离的本地加密索引，支持课程文件、公告附件、作业附件的跨课程检索、批量预览下载和 JSON 摘要导出；归档路径按 UTF-8 字节预算截断，不覆盖已有文件。
- 图书馆与体育办理：座位、研讨间、体育的创建与取消使用“预览 → 授权执行 → 结果核对”四步；续借只能准备并移交官方页面。
- 统一能力登记：`openthu describe --service <id>` 给出逐操作的可用、关闭、阻塞或缺失状态与原因；`openthu health` 给出服务级计数和阻塞列表，不再用一张“支持”表隐藏限制。
- 安装自检：源码检出用 `python scripts/dev.py doctor`，wheel/sdist 用 `openthu-doctor`；分类报告 Python、Node、浏览器、生产认证、打包与配置状态。
- Linux 本地加密存储：外部 `0600` 密钥文件与数据目录分离，使用 AES-256-GCM；Windows 继续使用当前用户 DPAPI 并保留旧记录读取。
- 错误对象新增 `category`、`retryable`、`automatic_replay_allowed`、`user_action`；`UNKNOWN`、`PARTIAL`、`PENDING_PAYMENT`、`PENDING_APPROVAL` 保持原义。

### 兼容性

- 29 个 MCP 工具名称、schema 字段与含义保持不变；新增字段一律可选。协议标识继续使用 `tsinghua-assistant`。
- 6 个旧入口保持兼容：`openthu`、`openthu-mcp`、`learn-assistant`、`learn-assistant-mcp`、`tsinghua-assistant`、`tsinghua-assistant-mcp`；新增 `openthu-doctor`。`contracts/v0.2/compatibility-baseline.json` 的 `entry_points` 仍记录 v0.1 的 6 个，新增入口不改变旧入口语义。
- Python 导入路径 `thu_assistant`、`learn_assistant` 不变；旧命令行参数继续解析。
- 最低 Python 提升到 3.12（CI 覆盖 3.12 与 3.13）；仍使用 3.10/3.11 的用户保留 v0.1。
- Windows 默认状态目录仍是 `%LOCALAPPDATA%\TsinghuaAssistant`，默认迁移预览为成功空操作；迁移是显式动作，只复制、不移动、不删除旧目录。
- 配置文件、环境变量与旧别名（`THU_ASSISTANT_STATE_DIR`、`THU_NODE`、`THU_PYTHON`）继续可用；显式配置无效时不会静默换目录。
- 旧服务名下的 `create`/`cancel` 契约保留但未启用，新调用使用登记操作 `create_seat_reservation`、`create_room_reservation`、`cancel_reservation`、`create_reservation`、`cancel_reservation`。

### 安装资源

- `build_support/distribution.json` 是唯一发行资源清单；wheel、sdist 和源码归档由它生成。
- wheel 内含 `thu_assistant/_bundled/`（Windows 认证辅助、固定上游 `madmodel-proxy`、tokenizer 与许可证），不含 `node_modules` 与离线测试；需要时在包目录执行 `npm ci --ignore-scripts`。
- 运行时依赖固定 `cryptography==50.0.2`、`Pillow==13.0.0`、`requests==2.33.0`；Playwright 是可选 `browser` extra；`pytest`、`build` 等只在开发依赖。
- `python scripts/package_release.py build|verify|install-check` 从已提交的 Git 内容生成确定性产物，`verify` 绑定提交、元数据、入口点与许可证，`install-check` 在仓库外安装 wheel 并确认用户状态未改写。
- 候选清单 `release-manifest.json` 记录每个随包文件的位置与哈希；产物状态是 `candidate-not-released`。

### 已知限制

- 真实校园认证依赖 Windows DPAPI；Linux 只有本地加密存储，没有生产级校园登录，不能用明文凭据替代。
- 图书馆与体育的真实创建/取消仍需按具体事项授权与人工验收；合成传输通过不等于真实办理成功。
- 图书馆续借没有已验证的请求端点，只能准备并向官方页面移交。
- 网络学堂作业上传、草稿与正式提交没有已验证接口，`submit_assignment` 阻塞；`prepare_submission` 只做本地校验。
- 个人课表、考试、成绩没有已验证来源，三个学业操作均为 `BLOCKED_SOURCE_EVIDENCE`；教学日历不能代替。
- 模型图片输入已实现本地检查和请求管线，但协议注册表为空，当前返回 `MODEL_IMAGE_INPUT_UNAVAILABLE`；目录的 `supportImage` 只是标注。
- 课程资料中心的公共 CLI/MCP/默认看板未注入资料库提供者，`local.resources` 为 `RESOURCE_PROVIDER_UNAVAILABLE`；导出与媒体登记只在宿主 API 可用。
- 亲友入校只有公开入口，没有当前表单与权限契约。
- 本地看板只读；没有写入、付款、密钥配置或模型生成入口。HTML 外壳缺失时 `/` 返回 `LOCAL_API_UI_NOT_INSTALLED`，JSON 路由仍可用。
- 日程增量游标是进程内游标（600 秒）；跨 CLI 进程复用会得到 `EVENT_CURSOR_EXPIRED`。
- Markdown 日程导出不显示来源完整性；必须同时看命令 JSON 的 `source_complete`。ICS 用 `X-OPENTHU-SOURCE-COMPLETE` 记录。
- 不解析 PDF、图片、Office 或录音内容；不提供后台轮询、消息推送或自动模型接力。

### 未完成事项

- Windows 本机与真实账号下的创建、取消、续借、付款和作业提交验收。
- 个人课表、考试、成绩的可信来源与适配。
- 学校模型服务的图片协议核对。
- 亲友入校的当前表单、权限与审批语义。
- Linux 生产级校园认证与桌面密钥环。
- 正式审查（9.1，Codex 未完成）：版本 PR 仍为 Draft，合并、打标签与发布尚未发生；本记录不声称已发布。

### 实施记录（按模块）

- 2.1 统一配置层 `thu_assistant.configuration`：命令行、`OPENTHU_*` 环境变量、本机 JSON 和默认值按固定优先级合并；未知/重复键、非法类型、不可读文件和秘密字段返回稳定错误，公开摘要不含路径或秘密。
- 2.1 统一路径解析 `thu_assistant.paths`：资源、状态、下载、临时和日志使用同一套描述；源码与包安装共享逻辑资源标识，缺少资源直接报告而不搜索旧安装；状态目录默认在代码库之外，拒绝覆盖程序资源、相对路径和密钥目录。
- 2.1 `doctor` 改为 `development`、`readonly`、`browser`、`production-auth`、`packaging`、`config` 分类，状态为通过/警告/失败/不适用；缺少 Chromium 只影响浏览器能力，Linux 生产认证为不适用，Python 最低版本读取项目声明。
- 2.1 诊断中的命令调用固定参数、超时和输出上限，版本字符串必须匹配格式，原始命令输出不进入报告；新增配置示例与合成测试。
- 2.2 新增 `secure-storage-header-v1`：AES-256-GCM、按用途派生密钥、账号引用用 HMAC 假名、记录 ID 绑定进 AAD；Windows 用当前用户 DPAPI，Linux 用外部 `0600` 密钥文件，缺密钥拒绝写入。
- 2.2 密钥轮换、旧密钥文件读取、健康检查和记录大小上限；不把密钥放进数据目录，不允许符号链接或过宽权限。
- 2.3 冻结来源登记表、数据模型、证据等级、错误分类与适配启动结论；后续模块按登记来源开发，不从第三方教程猜接口。
- 3.1 会话与网关迁移：按站点会话、锁、账号绑定与认证证据；关闭浏览器或进程崩溃后不重放已发出的请求。
- 3.2 统一查询注册、分页、快照游标、完整性字段和规范化数据模型；空结果必须同时满足 `source_complete=true` 与 `complete=true`。
- 3.3 本地图片与附件隐私处理：内容签名、大小与像素限制、元数据清除、按账号/任务/用途/时限绑定的媒体引用；测试编码器与生产密钥后端可辨别。
- 4.1 学业只读适配的模型、规则、敏感度与失败路径；个人课表、考试、成绩均因来源证据不足而阻塞。
- 4.2 课程资料中心：稳定资源标识、加密索引、单调版本合并、检索、清单、批量预览/执行下载、JSON 导出、缓存删除；不新增下载实现。
- 4.3 图书馆与体育只读一致性：检索窗口、`borrowable` 三态、座位/研讨间余量、规则与订单分轴；分页退化保留缺页。
- 4.4 统一办理框架与跨平台授权凭据：计划绑定、一次派发、未决恢复、可信宿主授权与本地取消。
- 4.5 模型图片输入：目录标注、协议证据、能力结论三条独立轴；出厂协议注册表为空，未验证时失败关闭。
- 5.1 图书馆座位/研讨间创建与取消、借阅资格读取、续借计划与官方页面移交；续借执行因无已验证端点而阻塞。
- 5.2 体育预约、取消与付款/退款/审批状态闭环；签名 URL 绑定一次派发。
- 5.3 作业提交边界：本地文件校验、加密计划与事务日志；生产上传与正式提交因无来源契约而阻塞，写开关默认关闭。
- 5.4 统一日程、增量快照与日历导出：`agenda-events-v1`、日期/时刻语义、冲突规则、`evc1` 进程内游标、ICS/Markdown/JSON 导出。
- 6.1 公共业务门面与 MCP/CLI 集成：六个门面方法、能力登记表、统一错误分类、`openthu describe/health/query/agenda`，保留 29 个工具。
- 6.2 本地看板读取 API 与进程边界：回环绑定、Host/Origin 校验、一次性引导令牌、本地会话、限流与分页、只读路由与静态外壳边界。
- 6.3 完整发行布局与安装升级迁移：单一版本源、`_bundled` 资源、确定性构建、`verify`/`install-check`、`openthu-doctor`、旧状态迁移与回退。
- 6.3 审计修复：迁移按真实状态目录分类，未决事务与待提交计划标为 `REVIEW_REQUIRED`，主机授权密钥与 grant 标为 `EXCLUDED_SENSITIVE` 不复制；DPAPI 密文即使目标同名并选择 `replace` 也不会跨平台复制；计划文件按不可信输入校验；`apply` 增加 `complete` 与未决冲突计数。
- 6.3 审计修复：`package_release verify` 绑定产物提交与 `HEAD`，核对 `Requires-Python`、`Requires-Dist`、全部入口点、许可证精确路径与 `_bundled` 只读模式；构建先写临时目录，失败保留上一份候选。
- 6.3 审计修复：`materialize` 拒绝越界路径并重建 `_bundled`；`learn-assistant-mcp` 使用单一版本源；CI 增加 3.13 job。
- 7.3 文档交付：用户手册、能力矩阵、工具对照、升级与回退、贡献与批次纪律、候选说明；文档一致性测试校验链接、工具数量、登记状态与隐私措辞。

## 0.1.0 — 2026-11-16

- 首次以 Openthu 发布独立公共源码，提供 MCP、命令行、校园读取和事务适配代码。
- 将原有分散依赖整理为仓库布局，移除私人环境路径，保留兼容命令与工具 schema。
- 提供 Windows/Linux 离线开发入口、合成测试、配置生成器、隐私检查和可校验发布包。
- 明确 Hermes/Codex 人工接力、维护管理、更新与 Release 责任。
- 实时认证仍依赖 Windows；图书馆/体育真实写入和亲友专用办理的限制见能力矩阵。
