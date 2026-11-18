# Openthu 完整开发交接

交接基准：2026-11-18，v0.2.0 未发布候选。集成分支 `hermes/1-install-diagnostics`，代码/CI head `4a03ecd`（本文档所在记录提交为 PR head）；版本 PR [#2](https://github.com/thus-zhou/Openthu/pull/2) 保持 Draft。本文与代码、能力矩阵、用户手册、版本计划和候选记录共同组成交付；任何历史测试结论都不替代当前运行结果。

## 0. 一句话结论

v0.2.0 计划内的实现、集成、离线回归、候选构建和总审计修复已经完成：Hermes 负责的 23 个模块全部 `accepted`，第 9 批（Codex 整体审查、合并、标签与 Release）为 `ready`。总审计的 17 条发现（P1×4、P2×11、P3×2，P0=0）全部闭环，其中 P2-9 按审计允许的替代证明闭环。当前没有未闭环的 P0/P1/P2/P3；但真实校园写入仍没有生产证据，且总审计后的最终 PR head 仍需 Codex 重建候选包并重新登记哈希。未合并 main、未打标签、未发布。

## 1. 当前状态速览

| 项目 | 值 |
|---|---|
| 目标版本 | `0.2.0`（未发布候选；已发布版本仍是 `0.1.0`） |
| 集成分支 | `hermes/1-install-diagnostics` |
| 代码/CI head | `4a03ecd1fc4cc1059f544381a16c3d3f348712ef`（总审计修复 + Windows 配置路径测试修复） |
| 产品代码提交 | `43f19ef65e4802092386c0dfab0f50da44590a3d`（总审计 P1/P2/P3 修复）；`4a03ecd` 只改 Windows 配置路径测试 |
| 候选产物提交 | `4a03ecd`（CI `release gate` 重建 4 个产物并 `verify`） |
| 前序候选 | `2bd42cc6ea414a246b71abe530b99eb759b78930`（consent-peek 修复后的旧候选） |
| 回接合并 | `5ec73d955073fdd283c8f5a6bab1fee072e848ed`（`hermes/8.1` → 集成分支） |
| 版本 PR | #2，Open / Draft，代码/CI head `4a03ecd`；文档记录提交后 PR head 前移 |
| main | `9135a75abc1847f317b8aedd1bb7447945669ac9`（未合并本候选） |
| 标签 | 仅有 `v0.1.0`；没有 `v0.2.0` 标签 |
| 包版本 | `0.2.0`；Python `>=3.12` |
| 规模 | 相对 main：337 个提交（截至 `6e22dbb`）、376 个文件、+94,503 / −948 行；另有 `4a03ecd` 测试修复与本文档记录提交 |
| 工具面 | 29 个 MCP 工具；6 个旧入口点 + `openthu-doctor` |
| 登记操作 | 81 条：64 `AVAILABLE`、6 `BLOCKED`、11 `MISSING`；按类别 READ 63、LOCAL_WRITE 2、MODEL 1、CAMPUS_WRITE 15 |
| 本地验证 | Python **1839 passed / 8 skipped / 0 failed**；Node **66 passed / 0 failed**；`dev.py check` 571 文件 / 0 隐私发现 / 29 工具 |
| CI（`4a03ecd`） | push `44236818999` 与 PR `44236825532` 四作业全绿；Linux 1905/8（Python 1839 + Node 66）、Windows 1892/21（Python 1826 + Node 66）；`release gate` 九项 true、0 gap |
| 总审计 | P0=0，P1=4，P2=11，P3=2；17/17 闭环 |
| 真实调用 | 校园 / 模型 / 浏览器 / 付款 / 提交 / 消息发送均为 0 |

> CI 状态必须按提交和运行号阅读，不能把旧候选的绿色当成当前 head 的结论。`2bd42cc` 是总审计前的完整候选；`6e22dbb` 的 Windows 作业因一个 Linux-only 测试失败；`4a03ecd` 修复该测试后双平台与 `release gate` 全绿，是当前代码/CI 证据。

## 2. 目标与代码结构

Openthu 用统一工具入口访问校园服务，保留网络学堂兼容接口，并把查询、准备、授权执行和结果核对分开。公共入口（MCP、CLI、本地看板）只调用统一业务门面，不直接拼接站点请求。

| 路径 | 责任与修改入口 |
|---|---|
| `src/thu_assistant/mcp_stdio.py` | MCP 工具 schema、调用分派、进度与取消；当前 29 个工具 |
| `src/thu_assistant/__main__.py` | 命令行：旧子命令 + `describe`、`health`、`query`、`agenda`、`dashboard` |
| `src/thu_assistant/facade/` | `business-facade-v1`：能力登记、查询、准备、执行、结果核对、取消 |
| `src/thu_assistant/local_api/` | `local-read-api-v1`：回环只读 HTTP API、本地会话、限流、分页与静态外壳边界 |
| `src/thu_assistant/gateway_client.py`、`gateway.js` | Python 与常驻 Node 子进程通信、认证衔接、读取与下载 |
| `scheduler.js`、`session-store.js`、`cookies.js`、`session-policy.js` | 串行调度、锁、账号绑定会话、Cookie 与认证证据 |
| `src/thu_assistant/queries.py`、`pagination.js` | 查询注册、完整性、稳定快照、分页与游标 |
| `src/thu_assistant/secure_store/` | `secure-storage-header-v1`：AES-256-GCM、密钥生命周期、Windows DPAPI 与 Linux 外部密钥 |
| `src/thu_assistant/actions.py`、`action_store.py`、`action_recovery.py` | 加密事务日志、计划绑定、一次执行、未决恢复 |
| `src/thu_assistant/library*.py`、`spaces.py`、`renewal/` | 馆藏、借阅、座位、研讨间、续借读取/计划/核对与图书馆办理 |
| `src/thu_assistant/sports*.py` | 体育场景、场次、订单和办理 |
| `src/thu_assistant/academics/` | 课表、考试、成绩的模型、规则、敏感度与失败路径；来源证据阻塞 |
| `src/thu_assistant/resources/` | 课程资料中心：加密索引、跨课程检索、清单、下载计划与 JSON 导出 |
| `src/thu_assistant/events.py`、`snapshot_diff.py`、`calendar_export.py` | 统一日程、增量快照、冲突与 ICS/Markdown/JSON 导出 |
| `src/thu_assistant/media_assets/`、`model_media.py` | 本地媒体引用与隐私处理；模型图片协议证据与请求预览 |
| `src/thu_assistant/models.py`、`models.js` | 官方模型目录、对话、来源映射、取消与用量 |
| `src/thu_assistant/todos.py` | 作业、借阅、预约统一待办及按账号隔离的历史缓存 |
| `src/learn_assistant/` | 原课程、作业、文件、下载、网页读取与兼容 CLI/MCP |
| `runtime/windows/` | Windows 加密认证辅助及网络学堂会话导入 |
| `vendor/madmodel-proxy/` | 固定版本的公开认证协议依赖；独立 MIT 许可 |
| `integrations/codex/`、`configs/` | 公共插件说明与可生成的配置模板 |
| `contracts/v0.2/` | 冻结契约、来源登记、数据模型、错误分类与样例 |
| `build_support/`、`scripts/package_release.py` | 发行资源清单、确定性构建、校验与安装检查 |
| `tests/` | 合成网页、JSON、模拟认证、真实浏览器中的合成页面；不包含个人导出数据 |
| `scripts/` | 开发、离线核验、人工交接、隐私检查和打包 |

### 运行链

MCP / CLI / 本地看板 → 统一业务门面 → 查询或事务引擎 → 校园服务适配器 → Node 网关 → 固定认证依赖或校园站点。浏览器为独立实例。真实登录状态使用当前 Windows 用户的 DPAPI 保存，不发送到云端开发机；Linux 的本地加密存储只保护本机状态，不提供校园登录。

每个 MCP 进程拥有一个网关；任务串行排队，认证锁和状态锁不覆盖整个网络请求。取消、断线或进程崩溃后不重放已发出的请求。发现不确定的事务时先核对已有记录。本地看板是独立进程，只读、只绑定回环地址，通过一次性引导令牌建立本地会话。

## 3. 完成度：9 批 24 模块

Hermes 负责第 1–8 批（23 个模块）；第 9 批 9.1 是 Codex 的发布关口，当前 `ready`。

| 批次 | 模块 | 状态 | 集成/交付提交 |
|---:|---|---|---|
| 1 | 1.1 版本基线、架构契约与调度冻结 | accepted | `2fcc0a6` |
| 2 | 2.1 收编安装诊断与统一配置路径；2.2 跨平台加密存储与密钥生命周期；2.3 校园来源证据与业务数据契约 | accepted | `4519117` |
| 3 | 3.1 跨平台认证会话与网关迁移；3.2 统一查询、数据模型与完整性规则；3.3 附件与图片的本地隐私处理 | accepted | `400adbe` |
| 4 | 4.1 个人课表、考试与成绩只读适配；4.2 课程资料中心与跨课程检索；4.3 图书馆体育查询一致性与规则读取；4.4 统一办理框架与跨平台授权凭据；4.5 模型图片输入与资料引用增强 | accepted | `4e07357`（接缝 `2cc1c05`） |
| 5 | 5.1 图书馆预约、取消与续借适配；5.2 体育预约取消与付款状态闭环；5.3 网络学堂作业上传与正式提交；5.4 统一日程、增量事件与日历导出 | accepted | `0779fff`（接缝 `ac99326`） |
| 6 | 6.1 公共业务门面与 MCP CLI 集成；6.2 本地看板读取 API 与进程边界；6.3 完整发行布局与安装升级迁移 | accepted | `4f5c62e`（接缝 X1–X7） |
| 7 | 7.1 轻量本地看板与统一使用体验；7.2 跨平台回归、隐私与故障注入；7.3 能力文档、用户手册与升级记录 | accepted | `fb6edb1`（接缝 `d81c81c`） |
| 8 | 8.1 全版本集成、候选构建与交审 | accepted | `2bd42cc`（回接合并 `5ec73d9`） |
| 9 | 9.1 Codex 整体审查与正式发行管理 | ready | — |

模块结果台账在 `plans/v0.2.0/交付/模块结果/`，每项都有 `.md` 与 `.json`。批次状态机在 `plans/v0.2.0/交付/批次门禁.json`，规则是“已验收前缀 → 唯一 ready → 其余 locked”。

### 3.1 各批的关键交付

- **第 1 批**：冻结 8 类契约 + 1 份旧接口兼容基线；冻结 Python 3.12/3.13、Node 24、依赖版本、工具数与批次屏障。
- **第 2 批**：安装诊断收编（`dev.py doctor` / `openthu-doctor`）、统一配置与路径、跨平台加密存储（Windows DPAPI / Linux 外部 `0600` 密钥）、29 条来源登记与 10 个规范模型。
- **第 3 批**：认证会话与网关迁移、统一查询/完整性规则、附件与图片的本地隐私处理；快照游标、账号域、错误分类贯穿查询层。
- **第 4 批**：学业只读适配（来源阻塞）、课程资料中心、图书馆/体育查询一致性、统一办理框架与宿主授权、模型图片输入管线。
- **第 5 批**：图书馆座位/研讨间/续借适配、体育预约取消与付款状态、作业上传/提交（来源阻塞）、统一日程与增量事件、ICS/Markdown/JSON 导出。
- **第 6 批**：公共业务门面、29 个 MCP 工具、CLI 新子命令、本地只读看板 API、完整发行布局、安装升级迁移。
- **第 7 批**：看板静态外壳随包安装并由 CLI 自动接线；跨平台回归、隐私与故障注入；能力文档、用户手册与升级记录。
- **第 8 批**：全版本集成、候选构建、双平台 CI、`release gate`、仓库外恢复安装；8.1 审计 P1×3 + P2×9 闭环，consent-peek 并发竞争修复。
- **总审计（跨批）**：17 条发现全部闭环，详见 §5。

## 4. 工程证据

### 4.1 校验器（在当前 head 实跑）

| 校验 | 结果 |
|---|---|
| `python plans/v0.2.0/工具/validate_plan.py` | `ok=true`；24 模块 / 9 批次；汉字 2149–2354，非空白 2537–2872，总汉字 53879 |
| `python contracts/v0.2/validate.py` | `ok=true`；8 契约 / 1 兼容基线 / 9 schema 样例 / 35 拒绝样例 / 8 额外夹具 / 29 legacy 工具；错误码 370 条 |
| `python contracts/v0.2/sources/validate.py` | `ok=true`；29 sources / 10 models / 8 tasks / 14 readiness / 7 fixtures；9 schema / 24 拒绝样例 |

### 4.2 本地离线验证（head `4a03ecd`）

| 检查 | 结果 |
|---|---|
| `python scripts/dev.py check` | `ok=true`；571 文件、0 隐私发现、29 个 MCP 工具；规划校验通过 |
| `python scripts/dev.py test` | Python **1839 passed / 8 platform skipped / 0 failed / 0 errors**；Node **66 passed / 0 failed**；`ok=true` |
| 真实调用 | `live_campus_calls=0`、`model_calls=0`、`business_submissions=0` |

审计修复新增 26 个 Python 用例（1813 → 1839），Node 保持 66；修复过程中没有用 `xfail`、删除用例或放宽断言掩盖失败。

### 4.3 CI 与候选

- **`4a03ecd`（当前代码/CI head）**：push `44236818999` 与 pull_request `44236825532` 四作业全部 success；Linux 报告 1905 passed/8 skipped（Python 1839/8 + Node 66），Windows 报告 1892 passed/21 skipped（Python 1826/21 + Node 66）；`release gate` 九项 true、0 gap；候选 4 个产物由门禁在同一提交重建。
- **`6e22dbb`（总审计修复后的首个 head，Windows 红）**：Linux 3.12/3.13 作业 success，Windows 的 `test_state_root_honours_the_configuration_file` 失败——测试把默认配置路径写死为 Linux 的 `$HOME/.config/openthu/config.json`，Windows 实际读 `%APPDATA%\Openthu\config.json`，于是 `state_root()` 回退到默认目录。修复为使用 `OPENTHU_CONFIG_FILE` 指向临时配置（跨平台），产品代码不变；`4a03ecd` 复跑双平台与门禁全绿。该失败是测试假设问题，不是 P1-1 产品修复回退。
- **`2bd42cc`（总审计前的完整绿色候选）**：push `44204917343` 与 pull_request `44204927343`，四作业全部 success；Linux 报告 1879 passed/8 skipped（Python 1813/8 + Node 66），Windows 报告 1866 passed/21 skipped（Python 1800/21 + Node 66）；`release gate` 九项 true、0 gap。该提交的候选是 4 个产物：源码 565、wheel 214（54 随包）、sdist 496。
- 历史失败运行都已定位并修复：depth-1 checkout 的祖先核对失败改为 `fetch-depth: 0`；`test_concurrent_execute_dispatches_once` 的 `STATE_READ_FAILED` 是 consent `peek()`/`take()` 竞争，已由 `2bd42cc` 的 consent 锁修复。

### 4.4 当前候选产物（`artifact_commit 4a03ecd`，CI `release gate` 重建）

| 产物 | 字节 | SHA256 |
|---|---:|---|
| `Openthu-0.2.0-source.tar.gz` | 3,691,357 | `d8bf25eebdcc43900481fda4b1df79eb0d766292f9020cc713d664995ebf7f8bd` |
| `Openthu-0.2.0-source.zip` | 4,151,902 | `6085eced8a284629ea307c156f80dcda07cfc29949186281af2b8fa8038dcd7a` |
| `openthu-0.2.0-py3-none-any.whl` | 2,810,776 | `cde3156579119b8dffc56c2f79d23d1f447579f02f13786b2eef7a74ea0ff0c3` |
| `openthu-0.2.0.tar.gz` | 3,455,708 | `28d28b61aa4f7b8152a7dfbffd0dc5f4a429196549e49c43b557e669dd437cc8` |

- 成员统计：源码 568；wheel 214（54 随包）；sdist 499。`4a03ecd` 的 CI 报告与 `verify` 均以该提交为准。
- `verify` 在 CI `release gate` 中通过：metadata、`Requires-Python`、`Requires-Dist`、七个入口点、许可证、随包布局与资源哈希全部通过。
- `install-check` 在仓库外临时 venv 中安装 wheel：包位于 `site-packages/thu_assistant`、`source_root=null`、缺失资源 `[]`、29 个工具、`doctor_ok=true`、用户状态不变。该检查在 `43f19ef` 候选上执行；`4a03ecd` 相对它只改测试文件，安装路径与资源布局不变，CI 门禁在 `4a03ecd` 重建并 `verify` 通过。
- 仓库外全新安装复核：看板外壳可服务、无本地会话 `/api/v1/health` 返回 401、缺 `_bundled` 资源时 doctor 失败关闭、无密钥返回 `SECURE_KEY_NOT_CONFIGURED`。该复核同样在 `43f19ef` 候选上执行。

**哈希边界**：`code_commit`、`artifact_commit`、`candidate_head` 是三个不同概念。`43f19ef` 是总审计后的产品代码提交；`4a03ecd` 在其上只改一个 Windows 测试，并由 CI `release gate` 重建 4 个产物；其后的记录性提交只改 `plans/`、`handoffs/`、文档与报告归因，不改变 wheel 内产品代码，但会改变 source/sdist 的字节和 SHA256。**最终 PR head 必须由 Codex 在同一提交树或最终 head 重新构建并登记，不能复用 `43f19ef`、`4a03ecd` 或 `2bd42cc` 的旧资产。**

## 5. 总审计与修复（2026-11-18）

- 审计快照：`de1d2fc409c64fca57854100a2ee9870405756d2`；只读审计，未修改代码。
- 结论：**P0 = 0，P1 = 4，P2 = 11，P3 = 2**；17/17 闭环。
- 修复范围：`88c48e1` … `6e22dbb`，共 15 个提交；产品代码修复集中在 `43f19ef`。总审计后的 CI 又发现一个 Windows-only 测试假设问题，由 `4a03ecd` 修复（只改测试）。
- 方法偏差：任务建议并行子智能体，但本会话 one-shot 委派预算为 0/2，17 条全部由本会话顺序完成；**没有独立的第二审查者**。Codex 的整体审查应覆盖本记录列出的所有提交。

### 5.1 P1（4 条）

| 编号 | 问题 | 修复 | 验证 |
|---|---|---|---|
| P1-1 | 统一状态目录没有端到端落地：`catalog.state_root()` 仍返回旧目录，迁移目标不被运行时使用 | `state_root()` 按 `OPENTHU_STATE_DIR` → `THU_ASSISTANT_STATE_DIR` → 配置 → 平台默认解析；动作、提交、待办、事件、迁移目标、网关/看板 bootstrap 使用同一层叠配置 | 新增状态目录/存储根测试；89 passed |
| P1-2 | ICS SEQUENCE 在事件离开导出窗口后回退为 0 | 新增 `MAX_SEQUENCE_ROWS=4096` 与 `_merge_sequences()`，保留窗口外 UID 并按更新顺序有界淘汰；`observe()`/`query()` 都改为合并 | A→B→A 后 SEQUENCE 仍递增；222 passed |
| P1-3 | 写开关关闭后 `prepare` 抛未登记错误码 `WRITES_DISABLED`，被归类为 INTERNAL/CONTACT_SUPPORT | `writes_disabled_code()` 按服务映射到 `LIBRARY_WRITES_DISABLED`/`SPORTS_WRITES_DISABLED`；通用 `WRITES_DISABLED` 登记为 `PERMISSION` | MCP 断言 `isError`、具体码、`category=PERMISSION`；258 passed |
| P1-4 | 旧 `prepare_submission` 返回绝对路径并写 0664/0775 明文预览 | 返回值只含 `name`/`exists`/`bytes`/`sha256`/`extension`；错误只用 `path.name`；预览改为相对 `submission-previews/<op>.json`，目录 0700、文件 0600 | 核心与 MCP 路径均无 tmp/home 路径；69 passed |

### 5.2 P2（11 条）与 P3（2 条）

- **P2-1** `learn/courses` 吞掉 `complete=False`：读取 `raw['complete']`、`status`、`warnings`、`checked_at`；不完整时固定 PARTIAL，不得变 EMPTY。
- **P2-2** 本地 API 把门面客户端错误折叠为 502：保留 404/409/429 分类；`AUTH_REQUIRED` 不映射为 401（401 专用于本地会话过期）。
- **P2-3** IPv6 回环 `base_url` 丢方括号：hostname 含 `:` 时加方括号。
- **P2-4** `describe()` 的 `tool_count`/`legacy_tools_unchanged` 硬编码：从实时 `TOOLS` 计算，并与冻结的 29 名基线比较。
- **P2-5** 隐私扫描不覆盖邮箱与学号：新增 `email`、`tsinghua-email`、`student-id` 规则；二进制用 latin-1 扫描高信号规则；忽略 `.invalid`/`.example` 等文档域名与合成学号。
- **P2-6** `lac1` 游标不绑定校园账号：`paging.binding()` 增加 `account_scope`；同会话切换账号后旧游标返回 `CURSOR_QUERY_MISMATCH`。
- **P2-7** 10 个模块错误码未登记：登记 10 个码 + `WRITES_DISABLED`，全表 359 → 370；新增静态字面量守卫。
- **P2-8** 批次 1 门禁 `integration_commit` 是占位字符串：改为真实 40 位 SHA `2fcc0a6...`；新增“所有 accepted 批次必须是 40 位 SHA”的测试。
- **P2-9** 候选记录称“不随包发布”但源码归档含 `plans/`/`handoffs/`；最终 head 产物哈希未登记：改写为“不改变 wheel 产品代码，但会改变 source/sdist 字节”；登记 `4a03ecd` 的四个 CI 重建哈希（`43f19ef` 是产品代码提交）；明确最终 head 由 Codex 重建。**残留：记录提交自指哈希在数学上不可行，这是审计允许的替代证明，不是静默跳过。**
- **P2-10** CHANGELOG 未完成事项过期：只保留“正式审查（9.1，Codex）；PR 仍 Draft，合并/标签/发布未发生”。
- **P2-11** `_account_scope()` 身份不可用仍返共享常量并放行查询：身份不可用/缺失时抛 `AUTH_REQUIRED`，账号绑定查询 fail-closed。
- **P3-1** `validate_plan.py` 未进 `dev.py check`/CI：已接线，并新增 pytest 包装。
- **P3-2** 隐私扫描只覆盖已跟踪文件：改为 `git ls-files -z --cached --others --exclude-standard`，覆盖未跟踪文件。

修复记录（本地，不入库）在 `.local/audits/total-audit-20261118.md` 与 `.local/audits/total-audit-remediation-20261118.md`。`4a03ecd` 的 Windows 测试修复记录在 `tests/test_remediation_state_scope.py` 与 CI 运行 `44236818999`/`44236825532`。公开记录只保留结论与命令，不包含账号、主机、凭据或私人路径。

## 6. 必须保留的接口语义

- 门面只有六个公共方法：`query`、`health`、`prepare`、`execute`、`result`、`cancel`；本地导出、媒体登记和看板读取是并列的本地方法，不是校园写入。
- 能力结论以登记表的 `state`/`reason` 为准：`AVAILABLE`、`DISABLED`、`BLOCKED`、`MISSING`；代码存在不构成可用证据。
- `status/source/checked_at/complete/warnings/verification_level` 一起解释。空数组或 HTTP 200 不自动证明业务成功。
- `EMPTY` 必须同时满足 `source_complete=true` 与 `complete=true`；`PARTIAL` 保留缺失页与原因；`snapshot_complete` 只代表快照读完。
- 分页游标绑定参数、进程内快照、本地看板会话、路由、页大小与当前校园账号，十分钟有效；过期后重新查询，不拼接不同快照。日程增量的 `change_cursor` 是另一种游标，600 秒且进程内有效，不可与分页游标互换。
- 准备操作不等于提交。提交一次后核对本人记录和精确字段；审批、付款、未知结果分别保留状态。
- `authorization` 文本不是宿主授权证据，不能伪造或绕过执行框架；真实写入需要当次具体事项的明确授权。
- 模型只在未开始响应且明确 401 时刷新重试一次；额度不足、403、网络异常或已输出不自动重放。图片输入必须有已登记协议，未验证时失败关闭。
- 本地 API 只读：没有 `prepare`/`execute`/付款/上传/密钥/模型生成路由；请求体 64 KiB、分页 50/200、刷新最短 10 秒。
- 真实凭据不得改用明文作为云端兼容方案。云端使用合成测试依赖；测试编码器与生产密钥后端必须可辨别。
- 29 个 MCP 工具、6 个旧入口点和 `thu_assistant`/`learn_assistant` 导入路径保持兼容，新增 `openthu-doctor`；新增字段可选，不删除旧字段。

## 7. 已知限制与外部阻塞

1. 真实校园认证依赖 Windows DPAPI；Linux 有本地加密存储但没有生产级校园登录，不能用明文 Cookie 替代。
2. 图书馆与体育创建/取消已实现准备、执行和核对框架，真实写入尚未按具体事项完成验收；合成传输通过不等于真实办理成功。
3. 图书馆续借只有资格读取、单笔计划、官方页面移交与新到期核对；没有已验证的请求端点，`renew_loan` 执行阻塞。
4. 作业提交只有本地准备与校验，没有上传、草稿或正式提交能力；写开关 `THU_LEARN_SUBMIT` 默认关闭。
5. 个人课表、考试、成绩没有已验证来源，三个学业操作均为 `BLOCKED_SOURCE_EVIDENCE`；教学日历不能代替。
6. 模型图片输入已实现本地检查与请求管线，但协议注册表为空，当前返回 `MODEL_IMAGE_INPUT_UNAVAILABLE`。
7. 课程资料中心的公共 CLI/MCP/默认看板未注入提供者；`local.resources` 为 `RESOURCE_PROVIDER_UNAVAILABLE`，导出与媒体登记只在宿主 API 可用。
8. 亲友入校只有公开入口，没有当前表单和权限契约；通用浏览器看到表单不代表业务已接通。
9. 本地看板只读；HTML 外壳缺失时 `/` 返回 `LOCAL_API_UI_NOT_INSTALLED`，JSON 路由仍可用。Chromium 已实测；Firefox/Safari 的静态子资源 `Sec-Fetch-Mode` 未验证。
10. 本地 API 的 HEAD 执行一次读取但不占 10 秒刷新配额；bootstrap 令牌一次性，会话过期需重启实例。
11. 旧 `learn_assistant.core.prepare_submission` 仍写本地未加密预览 JSON，但只含文件 `name`/`bytes`/`sha256` 与相对 `preview` 名，目录 0700、文件 0600，不含本地绝对路径（不上传、不提交；总审计 P1-4 修复）。
12. 学校接口会变化。历史真实读取只说明当时的版本和账号，公开版不携带那些业务记录。

## 8. 接手后的工作

### 8.1 第 9 批：Codex 整体审查与正式发行

1. **读取**：PR #2 描述（含“总审计修复”段）、`handoffs/v020-candidate.md`、`plans/v0.2.0/交付/候选版本记录.md`、`批次门禁记录.md`、`.local/audits/total-audit-*.md`（本地，不入库）。
2. **核对当前 head 的 CI**：代码/CI head `4a03ecd` 的 push `44236818999` 与 PR `44236825532` 已全绿；文档记录提交后的 PR head 只改记录，仍应核对一次。若红，先在同一分支交回 Hermes，不合并。
3. **审查总审计修复**：15 个提交，重点是 P1-1 状态目录、P1-2 ICS SEQUENCE、P1-3 写开关错误码、P1-4 旧预览路径/权限；注意本轮没有独立第二审查者。
4. **在最终 PR head 重建候选**：`python scripts/package_release.py build`、`verify --expected-commit <最终 head>`、`install-check --dist dist`；重新登记四个产物的字节与 SHA256。**不要复用 `43f19ef`、`4a03ecd` 或 `2bd42cc` 的旧资产。**
5. **同步台账**（见 §8.3），然后按 `docs/MAINTENANCE.md` 合并 main、创建 `v0.2.0` 标签、发布 GitHub Release，并核对公开下载、SHA256 与安装检查。
6. 合并前确认：PR #2 的最终 head 包含全部 23 个 Hermes 模块提交；29 个工具、6 个旧入口点、导入路径与错误形状未变；没有把任何 `BLOCKED`/`PARTIAL`/`UNKNOWN` 写成成功。

### 8.2 发布后 / 下一版本

1. **真实写入验收**：在 Windows 本机与真实账号下，按具体事项完成图书馆座位/研讨间、体育预约/取消、续借和作业提交的读取、执行、付款/审批与结果核对；公开只写脱敏摘要。
2. **来源证据**：续借请求契约、作业上传/草稿/正式提交端点、个人课表/考试/成绩来源、学校模型图片协议、亲友入校表单与审批语义。没有契约时保持 `BLOCKED_SOURCE_EVIDENCE`，不用合成夹具冒充真实站点。
3. **Linux 生产认证与桌面密钥环**：在“缺密钥拒绝写入”的前提下评估；不得用明文落盘绕过 Windows 依赖。
4. **路线图其余项**：更多只读来源（校内通知、云盘、培养方案）、推送与通知、原生移动体验、OCR/录音、课程知识库、适配生态；按独立 Issue 规划，不自动接力。

### 8.3 当前台账的已知滞后（必须在合并前处理）

总审计修复发生在批次 8 验收之后。以下文件仍指向 `2bd42cc` 或缺少总审计段，**不是**当前产品树的完整描述：

| 文件 | 现状 | 建议 |
|---|---|---|
| `plans/v0.2.0/交付/批次门禁.json` | 批次 8 `integration_commit` 仍是 `2bd42cc`；`8.1` 的 `delivery_commit`/`artifact_commit` 也是 `2bd42cc` | 在最终绿提交上把批次 8 的集成提交指向含总审计修复的 CI 绿提交（`4a03ecd`）；`delivery_commit` 记产品代码提交 `43f19ef`，并补 `4a03ecd` 测试修复；补总审计 17/17 与新的测试计数 |
| `plans/v0.2.0/交付/模块结果/8.1.json` / `.md` | `branch_head` 仍写“product code ends at 2bd42cc”；checks 是 1813/8、568 文件 | 增加 `total_audit_remediation` 段：`43f19ef`/`4a03ecd`、17/17、1839/8、571 文件、370 错误码；更新 `branch_head` |
| `plans/v0.2.0/版本总清单.md` | 状态摘要停在批次 8，未提总审计；候选表写 `2bd42cc` | 更新为当前 head、`4a03ecd` 候选与 17/17 |
| `plans/v0.2.0/进度清单.md` | 第 8 批后没有总审计段 | 增加“总审计修复 17/17”与下一步 |
| `plans/v0.2.0/交付/集成清单.md` | 8.1 行和候选段停在 `2bd42cc` | 补总审计提交链与当前候选 |
| `plans/v0.2.0/交付/批次门禁记录.md` | 有 P2-6/P1-4 的个别同步，但没有完整的总审计小节 | 追加“总审计修复与复验（`4a03ecd`）”小节 |
| `handoffs/v020-candidate.json` | `code_commit` 仍为 `2bd42cc` | 在最终交接提交上重新运行 `python scripts/handoff.py v020-candidate --to codex` |
| `docs/HANDOVER.md`（本文） | 已更新到总审计后状态 | 随本次记录性提交进入仓库 |

这些是记录一致性问题，不是产品缺陷；但不能让门禁继续指向审计前的提交而声称“已验收”。处理方式应是记录性提交 + 在新 head 上重跑 CI/`release gate`，不需要改产品代码。

## 9. 关键文件索引

- 总控与批次：`plans/v0.2.0/00-总控与批次调度.md`、`03-Hermes执行启动说明.md`
- 版本清单与进度：`plans/v0.2.0/版本总清单.md`、`进度清单.md`
- 交付台账：`plans/v0.2.0/交付/基线记录.md`、`候选版本记录.md`、`集成清单.md`、`批次门禁.json`、`批次门禁记录.md`、`模块负责表.json`、`验收映射.json`、`模块结果/`
- 契约：`contracts/v0.2/`（8 类契约 + `compatibility-baseline.json` + `error-classification.json` 370 码 + `sources/`）
- 能力与工具：`docs/CAPABILITIES.md`（81 条登记）、`docs/TOOLS.md`（29 工具）
- 使用与安装：`docs/USER_GUIDE.md`、`docs/INSTALL.md`、`docs/UPGRADE.md`、`README.md`
- 协作与发布：`AGENTS.md`、`docs/BATCH_UPGRADES.md`、`docs/MANUAL_RELAY.md`、`docs/MAINTENANCE.md`、`handoffs/`
- 本地审计（不入库）：`.local/audits/total-audit-20261118.md`、`total-audit-remediation-20261118.md`、各批 `*-audit.md`
- 候选构建：`scripts/package_release.py`、`build_support/distribution.json`、`dist/`（被 `.gitignore` 排除）

## 10. 常用命令

```sh
# 开发环境（本机已有 .venv 时）
source .local/activate.sh
unset PYTHONPATH          # 防止外部 site-packages 遮蔽项目 .venv

python scripts/dev.py doctor
python scripts/dev.py check
python scripts/dev.py test
python scripts/dev.py smoke

python plans/v0.2.0/工具/validate_plan.py
python contracts/v0.2/validate.py
python contracts/v0.2/sources/validate.py

python scripts/package_release.py build
python scripts/package_release.py verify --expected-commit <commit>
python scripts/package_release.py install-check --dist dist

openthu describe --service library
openthu health
openthu query --service learn --operation courses
openthu dashboard --check

python scripts/handoff.py v020-candidate --to codex
gh api repos/thus-zhou/Openthu/commits/<sha>/check-runs
```

环境基线：Python 3.12.3、Node 24.18.0、npm 11.16.0；依赖 `cryptography==50.0.2`、`Pillow==13.0.0`、`requests==2.33.0`，可选 `playwright==1.57.0`，开发依赖 `pytest==8.3.4`、`build==1.2.2.post1`、`jsonschema==4.26.0`；Node `tough-cookie==6.0.0` 由锁文件固定。

## 11. 关键提交链

```text
9135a75  main 协作规范基线
e92fd47  批次协作规范合入集成分支
2fcc0a6  批次 1 交付/回接
4519117  批次 2 回接
400adbe  批次 3 回接
4e07357  批次 4 回接（接缝 2cc1c05）
0779fff  批次 5 回接（接缝 ac99326）
4f5c62e  批次 6 回接
fb6edb1  批次 7 回接（接缝 d81c81c）
9b1bef5  8.1 实现起点
c81a877  8.1 审计修复
2bd42cc  8.1 consent-peek 修复 + 前序候选（CI 绿）
5ec73d9  hermes/8.1 → 集成分支
de1d2fc  总审计快照（审计前 head）
88c48e1 … 6e22dbb  总审计 17 条修复与记录（15 个提交）
43f19ef  总审计后产品代码 + 候选产物重建
4a03ecd  Windows 配置路径测试修复（代码/CI head，双平台全绿）
```

## 12. 边界声明

- 本文和仓库中的“已实现”“已登记”“真实业务有证据”是三件不同的事；`PARTIAL`、`UNKNOWN`、`PENDING_PAYMENT`、`PENDING_APPROVAL` 都不是成功。
- 合成测试、HTTP 200、目录命中、单册可见、座位空闲、费用报价、模型目录标注，都不提升为业务成功。
- 真实创建、取消、申请提交和付款必须由使用者针对该具体事项明确授权；开发与发布授权不代替业务授权。
- Hermes 不合并 main、不打标签、不发布 Release。第 9 批由 Codex 按 `docs/MAINTENANCE.md` 完成；合并前必须重建并核对最终 head 的候选资产。
- 公开仓库不保存账号、学号、Cookie、令牌、主机/IP、个人路径、原始响应或截图；本地审计与私有实测记录不进入公共仓库。
