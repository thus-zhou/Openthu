# Openthu 用户手册（v0.2.0 候选）

本手册面向第一次使用 Openthu 的读者，按“安装 → 诊断 → 建立本人认证 → 只读查询 → 看板 → 明确授权的办理”顺序说明。所有示例使用合成账号、合成学期和空状态目录，不包含任何人的课程、成绩、主机或凭据。

版本状态：已发布版本是 0.1.0；仓库当前打包 0.2.0，属于**未发布候选**。候选版本的真实校园业务证据与 0.1.0 相同或更少，不能把本手册的命令可执行当作校园业务已验收。能力逐项状态见[能力与验证状态](CAPABILITIES.md)，工具对照见[MCP 工具与登记操作](TOOLS.md)。

## 1. 能力边界先读

Openthu 是独立社区项目，与清华大学官方无隶属关系。它只使用使用者自己的账号、资格和官方页面，不代注册、不代付款、不绕过审批。

四件事必须分开理解：

| 维度 | 含义 | 不等于 |
|---|---|---|
| 代码已实现 | 仓库里有实现和合成测试 | 当前环境可运行 |
| 环境已就绪 | Python、Node、浏览器、密钥和会话都可用 | 学校来源可读 |
| 协议有依据 | 已登记参数、分页和错误语义 | 站点当前仍接受该请求 |
| 真实业务有证据 | 在明确账号和日期下完成过该次读取或写入 | 其他人、其他时间也可用 |

任何页面返回 HTTP 200、目录命中、单次可见、座位空闲、费用报价、模型目录标注，都不等于业务成功。`UNKNOWN`、`PARTIAL`、`PENDING_PAYMENT`、`PENDING_APPROVAL` 必须原样报告，不能改写成成功。

## 2. 首次使用

### 2.1 完整安装（源码包）

需要 Python 3.12 或 3.13、Node.js 24、Git。Windows 与 Linux 都可完成离线开发；真实校园认证目前仍依赖 Windows 的当前用户 DPAPI。

```sh
git clone https://github.com/thus-zhou/Openthu.git
cd Openthu
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# PowerShell
# .venv/Scripts/Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python scripts/dev.py setup-node
python scripts/dev.py doctor
```

`setup-node` 按 `src/thu_assistant/package-lock.json` 安装固定版本的 `tough-cookie`，不执行安装脚本。若使用 wheel 或 sdist，安装后运行 `openthu-doctor`（等价于 `python -m thu_assistant.doctor`）；它检查版本、包内 `_bundled` 布局、认证资源、配置与能力状态，不登录、不读取凭据、不写状态。

`python scripts/dev.py doctor` 按 `development`、`readonly`、`browser`、`production-auth`、`packaging`、`config` 分类检查。条目状态为“通过 / 警告 / 失败 / 不适用”，只有标记为阻塞的失败才阻止开始：

- Linux 上 `production-auth` 为“不适用”，不会因为缺少 Windows 辅助而把合法安装判成损坏。
- 缺少 Chromium 只影响浏览器能力，静态目录、只读查询和命令行仍可用；但离线全量测试中的真实浏览器用例需要 Chromium，未安装时 `python scripts/dev.py test` 不会通过。
- 未安装 `pytest`、`build`、`playwright` 或 `openthu` 会使 `development` 阻塞，按输出的修复命令安装。
- 显式配置无效时用 `--config PATH` 重新指定，不会静默回退到旧目录或另一账号。

### 2.2 建立本人认证

| 环境 | 会话与密钥 | 当前能做到 |
|---|---|---|
| Windows（桌面） | 当前 Windows 用户的 DPAPI；会话密文只能由同一用户读取 | 官方统一认证、MFA、真实只读与按事项授权的办理 |
| 无桌面 Linux | 仅本地加密存储：外部密钥文件 `0600`、与数据目录分离；缺少密钥时拒绝写入，不生成密钥、不退化为明文 | 离线开发、合成测试、静态目录、本地派生读取、看板 API；**没有生产级校园登录** |
| 云端开发机 | 与 Linux 相同，且不应导入任何人的 Windows 凭据 | 开发、检查、测试；不访问校园账户 |

Windows 首次认证按官方流程完成，需要再次 MFA 时会明确返回，不会索要或输出密码。Linux 的生产登录没有实现，不能用“明文 Cookie”或“把 DPAPI 文件复制过来”绕过。跨平台复制时，迁移预览把 `.dpapi` 密文标为 `UNSUPPORTED_PLATFORM`；如果用 `LegacyDpapiReader` 在当前平台直接读取，运行时会得到 `SECURE_LEGACY_UNAVAILABLE`。两个标记都不是成功，需要重新认证。

Linux 本地加密存储使用以下环境变量。密钥文件不是任意 32 字节随机文件：它必须包含 `THU-KEY-1\0` 文件头和 32 字节随机主密钥，权限 `0600`。用模块提供的 `create_key_file()` 创建，不能手工写随机字节：

```sh
python - <<'PY'
from thu_assistant.secure_store import create_key_file
create_key_file("/absolute/owner-only/master.key", "file-key")
PY
export THU_SECURE_KEY_FILE=/absolute/owner-only/master.key
export THU_SECURE_DATA_DIR=/absolute/user/state
```

`create_key_file()` 拒绝覆盖已有文件，父目录不存在时以 `0700` 创建，随后仍然要求密钥文件是当前用户所有、`0600`、普通文件且不是符号链接。格式或权限不符时得到 `SECURE_KEY_FORMAT` 或权限错误，不会自动修复。

`THU_SECURE_BACKEND` 选择提供者；`THU_SECURE_RETIRED_KEY_FILES` 用于轮换后的旧记录读取。把密钥放在数据目录内、放宽权限或使用符号链接都会被拒绝。密钥和状态目录不要提交到 Git，也不要贴到聊天或 Issue。

### 2.3 只读查询

先看静态目录与能力登记，不接触校园站点：

```sh
openthu services --query 图书馆
openthu describe --service library
openthu health --service learn
```

`describe` 列出已登记的只读操作、参数、分页、写能力与阻塞原因；`health` 只报告本地登记状态，不探测真实站点。`health` 返回 `PARTIAL` 是正常结果：它表示该服务有已登记但被阻塞的操作，不是命令失败。

执行一个已登记的只读查询：

```sh
openthu query --service learn --operation courses
openthu query --service library --operation seat_areas --page-size 20
openthu query --service local --operation agenda \
  --parameters '{"start_date":"2026-11-17","end_date":"2026-11-23","services":["learn"]}'
```

参数只接受登记字段。`--parameters` 可以是 JSON 字符串，也可以是 `@file.json`；不接受 Cookie、票据、任意接口、请求头或 HTTP 方法。`--cursor` 只在同一次快照内续读，默认 600 秒失效；跨进程或过期后重新查询，不拼接不同快照。

空状态目录下的真实失败示例（Linux，未配置密钥）：

```text
openthu query --service learn --operation courses
{"ok": false, "command": "query",
 "error": {"code": "SECURE_KEY_NOT_CONFIGURED", "category": "INTERNAL",
           "retryable": false, "automatic_replay_allowed": false,
           "user_action": "CONTACT_SUPPORT"}}
```

这条错误说明本机没有可用的加密存储，不是“没有课程”。先配置密钥或换到有认证的 Windows 环境。

### 2.4 本地看板

```sh
openthu dashboard --check                 # 启动一次，打印地址后立即停止
openthu dashboard --port 8765             # 常驻；先打印地址与实例号
```

- 只允许回环地址：`127.0.0.1`、`localhost`、`::1`。公网绑定会被拒绝。
- 只提供只读路由：`/api/v1/health`、`/services`、`/services/{id}`、`/query/{service}/{operation}`、`/todos`、`/calendar`、`/resources`。没有写入、付款、密钥配置或模型生成入口。
- 本地会话由实例以 `0600` 文件交付一次性引导令牌；直接用 `curl`（不带令牌）会得到 `LOCAL_API_SESSION_REQUIRED`，这不是学校登录失败。浏览器外壳负责完成这一步；没有外壳时先用 JSON 路由。
- 静态 HTML 外壳随包安装（`src/thu_assistant/web_assets/`），`openthu dashboard` 启动时把该目录交给本地 API，`/` 直接提供只读页面；页面只通过同源 `/api/v1/...` 读取数据。静态目录缺失或不可读时，`/` 返回 `LOCAL_API_UI_NOT_INSTALLED`，JSON 路由与 `--check` 不受影响。
- 工程限制：请求体 64 KiB、默认每页 50（上限 200）、UI 等待 30 秒、同一读取刷新最短间隔 10 秒、最多 16 个本地会话。它们是工程边界，不是学校规定。

`--check` 的已验证输出（端口由系统分配）：

```json
{"ok": true, "command": "dashboard",
 "result": {"status": "LISTENING", "mode": "CHECK",
            "url": "http://127.0.0.1:34967", "port": 34967,
            "instance_id": "195c6c75a8d0721f545b0f20e3ac11b1"}}
```

### 2.5 明确授权的办理

办理分四步，任何一步都不能跳过：

1. `prepare_service_action` 生成预览和加密计划，计划默认 600 秒失效。准备不等于提交。
2. 使用者在可信宿主中确认这笔具体事项（资源、时间、成员、费用、取消规则）。
3. `execute_service_action` 执行；只允许一次派发。`authorization` 文本不是授权证据。
4. `get_action_result` 读取并核对远端记录；未知结果只核对，禁止自动重发。

当前可用与阻塞的办理：

| 事项 | 登记操作 | 当前状态 |
|---|---|---|
| 图书馆座位预约 | `library.create_seat_reservation` | 已实现专用适配；真实事项需当次授权 |
| 图书馆研讨间预约 | `library.create_room_reservation` | 已实现专用适配；成员与规则可能要求在官方页面完成 |
| 图书馆取消 | `library.cancel_reservation`（`system` 取 `seat` 或 `room`） | 已实现专用适配；取消与审批分别核对 |
| 体育预约/取消 | `sports.create_reservation`、`sports.cancel_reservation` | 已实现专用适配；付款以结算页为准 |
| 图书馆续借 | `library.renew_loan` | 仅能准备与移交官方页面，执行阻塞（无已验证请求端点） |
| 网络学堂交作业 | `learn.submit_assignment` | 阻塞；没有已验证的上传、草稿或正式提交接口 |
| 亲友入校 | `visitors.create` / `visitors.cancel` | 适配缺失（`ADAPTER_REQUIRED`），不要用通用浏览器提交代替 |

旧服务名上的 `create`/`cancel`（`sports`、`library-seat`、`library-rooms`、`visitors`）仍保留在兼容框架中，但未启用；新调用应使用上表的登记操作。

办理状态必须原样保留：`PENDING_PAYMENT` 表示待付款，`PENDING_APPROVAL` 表示待审批，`UNKNOWN` 表示结果未知。它们都不是成功；未知时先用 `get_action_result` 核对本人记录。

## 3. 课程资料中心

资料中心把课程文件、公告附件和作业附件汇总成一个按账号与学期隔离的本地加密索引，支持跨课程检索、批量预览下载和 JSON 摘要导出。它不解析 PDF、图片、Office 或录音内容；全文检索只覆盖已经读取的公告正文与作业说明，正文被截断时标为 `body_truncated`，检索结果为 `PARTIAL`。

当前公共入口状态：CLI、MCP 和默认看板**没有注入资料库提供者**。`local.resources` 登记为 `MISSING`（原因 `RESOURCE_PROVIDER_UNAVAILABLE`，证据 `LOCAL_INDEX_READ`）。看板 `/api/v1/resources` 不带 `semester` 时返回 `status=ERROR`、`warnings=["SEMESTER_REQUIRED"]`、`error.code=INVALID_ARGUMENT_VALUE`；带 `semester` 但未注入提供者时返回 `status=ERROR`、`error.code=FACADE_CAPABILITY_UNAVAILABLE`，不是空列表，也不是 502。宿主可以注入提供者后调用 Python API：

```python
from thu_assistant.resources import ResourceCenter

centre = ResourceCenter(secure_store, "synthetic-account")
centre.collect(provider, semester="2026-2027-1", include_notice_details=True)
page = centre.search(semester="2026-2027-1", query="实验", source="NOTICE_ATTACHMENT")
plan = centre.preview_download(
    semester="2026-2027-1",
    selections=["learn:FILE:1001"],
    destination="/absolute/user/choice",
)
centre.execute_download(plan["plan_id"], confirm=True)
```

规则：

- 资源标识形如 `learn:FILE:<源编号>`、`learn:NOTICE_ATTACHMENT:<源编号>`、`learn:ASSIGNMENT_ATTACHMENT:<源编号>`；标题不参与身份判定。
- 索引只读元数据；公告/作业附件列表必须显式 `include_notice_details` / `include_assignment_details`，索引阶段不下载文件。
- 下载预览不写文件；执行需要 `confirm=True`，计划只在当前进程有效十分钟且一次性使用。
- 同名文件永不覆盖：内容相同则复用，不同则报告冲突并保留原文件。
- 导出是 JSON 摘要，不是资料副本；包含正文摘录时要求 `acknowledge_personal=True`，拒绝写入 Git 工作树，默认不覆盖已有文件。
- 删除索引使用 `delete_cache(semester)`，不删除归档目录，也不把索引转成明文。

## 4. 日程、增量变化与日历导出

### 4.1 读取日程

```sh
openthu agenda --start-date 2026-11-17 --end-date 2026-11-23
openthu agenda --services learn,library --semester 2026-2027-1
```

不带参数时默认读取未来七天，时区固定 `Asia/Shanghai`。`agenda` 是按需汇总，不启动轮询、不发送消息、不标记已送达。日期级时间保持 `DATE`，精确时刻保持 `DATE_TIME`；无日期的项保留为无日期项，不补造午夜。

高级筛选通过统一查询完成：

```sh
openthu query --service local --operation agenda --parameters '{
  "start_date":"2026-11-17","end_date":"2026-11-23",
  "services":["learn.assignments","library.loans"],
  "semester":"2026-2027-1",
  "include_completed":false,"include_overdue":true,"allow_stale":false,
  "max_source_pages":3}'
```

来源失败、未读完或加密存储不可用时，结果是 `PARTIAL` 并带 `partial_reasons`，不是空成功。已验证的空状态输出：

```json
{"status": "PARTIAL", "complete": false, "source_complete": false,
 "warnings": ["SOURCE_PARTIAL"],
 "sources_status": [{"source": "learn.assignments", "status": "FAILED",
                     "errors": [{"code": "SECURE_KEY_NOT_CONFIGURED"}]}],
 "items": [], "total_items": 0}
```

`views`（`upcoming`/`overdue`/`undated`/`completed`/`by_source`）与 `conflicts` 覆盖整个窗口；`items` 按快照分页。

### 4.2 增量变化

```sh
openthu query --service local --operation agenda_changes --parameters '{
  "start_date":"2026-11-17","end_date":"2026-11-23","services":["learn"]}'
```

第一次调用建立基线（`baseline_state=CREATED`），结果带 `change_cursor`（形如 `evc1.…`）；在同一进程内把它作为 `change_cursor` 传入下一次调用即可得到 `ADDED`、`MODIFIED`、`CANCELLED`、`DELETED`、`SOURCE_UNAVAILABLE`、`SOURCE_INCOMPLETE`。规则：

- `change_cursor` 是增量基线游标，`next_cursor` 只是本次变更集的分页游标，两者不能互换。
- 游标绑定账号、范围与规范版本，600 秒失效，且是进程内游标。CLI 每次都是新进程，跨调用复用会得到 `EVENT_CURSOR_EXPIRED`；应在同一 MCP 会话或宿主进程内先建基线再取增量。
- 只有前后两次都完整且可比较时，消失项才是 `DELETED`；来源失败或少读一页时保留原事件并标记 `HISTORICAL`，不把分页失败变成全量取消。
- 本版只输出结构化结果，不发送微信或其他消息，也不在后台自动轮询。

### 4.3 日历导出

```sh
openthu agenda --start-date 2026-11-17 --end-date 2026-11-30 \
  --export /absolute/user/calendar.ics --format ics
openthu agenda --export /absolute/user/calendar.md --format markdown --overwrite
openthu agenda --export /absolute/user/calendar.json --format json --allow-partial
```

- 必须给绝对路径；未给日期范围时导出会被拒绝（`EXPORT_SCOPE_REQUIRED`）。
- 默认不覆盖已有文件；覆盖必须显式 `--overwrite`。
- 结果不完整或来源失败时，导出默认拒绝（`EXPORT_SOURCE_INCOMPLETE`）。只有显式 `--allow-partial` 才写出文件，结果中 `source_complete=false`、`source_status=PARTIAL`。
- ICS 在头字段写入 `X-OPENTHU-SOURCE-COMPLETE`、`X-OPENTHU-WARNINGS`、`X-OPENTHU-SCOPE`；Markdown 正文只列事件，**不显示完整性**，因此导出后必须同时看命令 JSON 的 `source_complete`。
- 导出是本地文件写入，不是消息发送，也不改变任何校园记录。

## 5. 图片输入

`model_chat` 接受 `images` 媒体引用数组，但必须同时满足：媒体已按 3.3 登记、引用未过期、内容签名与声明一致、平台协议已登记。`images` 的每一项是对象 `{"media_id": "media_<8-64 位字母数字或下划线/连字符>"}`，不是路径、URL 或 `media:IMG:...` 字符串；带图片时还必须显式给 `model`，否则返回 `MODEL_REQUIRED_FOR_IMAGE_INPUT`。工程上限为单图 8 MiB、每次 4 图、合计 16 MiB、单图 1600 万像素，类型只接受 JPEG、PNG、WebP；平台契约更严格时取较小值。

当前出厂状态：公共 `model_chat` 路径没有注入媒体管道，图片请求在读取字节或发出请求之前返回 `MODEL_IMAGE_INPUT_UNAVAILABLE`；只有宿主注入媒体管道、但协议注册表仍为空时才返回 `MODEL_IMAGE_PROTOCOL_UNVERIFIED`。看板与 `describe` 把 `local.model_image_input` 标为 `BLOCKED`，原因是 `MODEL_IMAGE_INPUT_UNAVAILABLE`。目录中的 `supportImage` 标注只是官方目录标记，不构成传输证据。

```json
{"tool": "model_chat",
 "arguments": {"model": "synthetic-model",
               "prompt": "解释这张合成示意图",
               "images": [{"media_id": "media_synthetic0001"}]}}
```

`model` 必须来自 `list_models` 返回的目录；`media_synthetic0001` 是合成引用，真实调用必须使用本地媒体登记返回的 `media_id`。

规则：

- 只发送使用者明确选择的图片；不自动带入课程、公告、预约或看板中的其他内容。
- 文件路径、绝对目录、未登记引用和过期引用都会被拒绝。
- 图片不会被压缩或转码绕过大小与类型检查。
- 没有协议证据时返回明确错误，不静默退化为纯文本请求。

## 6. 常见状态与下一步

| 状态/错误 | 含义 | 下一步 |
|---|---|---|
| `OK` | 本次范围读取完成 | 按来源和时间解释结果 |
| `EMPTY` | 已证明完整且确实为空 | 可以报告空，但要带上范围 |
| `PARTIAL` | 来源失败、缺页或未读完 | 报告已读部分与缺失原因，不重试写入 |
| `AUTH_REQUIRED` | 需要本人认证 | 在 Windows 官方流程中完成认证 |
| `FORBIDDEN` | 账号无该权限 | 不换账号、不绕过 |
| `RATE_LIMITED` | 请求过快 | 等待后重试只读 |
| `SECURE_KEY_NOT_CONFIGURED` | 本机没有可用加密存储 | 配置 `THU_SECURE_KEY_FILE`/`THU_SECURE_DATA_DIR`，不要用明文替代 |
| `SECURE_KEY_FORMAT` | 密钥文件缺少 `THU-KEY-1` 文件头、长度不符或权限不对 | 用 `create_key_file()` 重建；不要手工写 32 字节随机数据 |
| `SECURE_LEGACY_UNAVAILABLE` | 当前平台不能读取旧 Windows DPAPI 密文 | 回到原来的 Windows 账户重新认证；迁移预览会把它标为 `UNSUPPORTED_PLATFORM` |
| `LOCAL_API_SESSION_REQUIRED` | 看板请求没有本地会话 | 用实例提供的地址在浏览器打开，不是重新登录学校 |
| `ADAPTER_REQUIRED` | 没有登记的站点契约 | 在官方页面完成，或等待适配 |
| `BLOCKED_SOURCE_EVIDENCE` | 来源证据不足 | 保持只读，不猜接口 |
| `PENDING_PAYMENT` | 待付款 | 以官方结算页为准，不当作已成功 |
| `PENDING_APPROVAL` | 待审批/成员同意 | 保持等待，不重复提交 |
| `UNKNOWN` | 结果无法核实 | 用 `get_action_result` 核对本人记录，禁止自动重发 |

## 7. 数据放在哪里

- 程序文件、配置模板和合成测试在仓库或安装目录。
- 状态、下载、临时、日志目录在代码库之外，可用 `OPENTHU_STATE_DIR`、`OPENTHU_DOWNLOAD_DIR`、`OPENTHU_TEMP_DIR`、`OPENTHU_LOG_DIR` 指定；相对路径会被拒绝。
- 密钥与数据目录分离；Windows 用当前用户 DPAPI，Linux 用外部 `0600` 密钥文件。
- 旧状态只通过 `python scripts/migrate_state.py preview` 只读预览；升级和迁移不会自动搬迁凭据、撤销远端业务或重放未决事务。详见[升级与回退](UPGRADE.md)。

不要在 Issue、PR、聊天或截图中提交真实姓名、学号、密码、Cookie、令牌、主机地址、个人目录或原始业务响应。公开报告只写错误类别、合成复现和已检查的摘要。
