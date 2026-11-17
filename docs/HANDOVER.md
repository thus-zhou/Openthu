# Openthu 完整开发交接

交接基准：2026-11-17，v0.2.0 未发布候选。本文与代码、能力矩阵、用户手册和 Release 检查共同组成交付；任何历史测试结论都不替代当前运行结果。

## 目标与结构

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

## 运行链

MCP / CLI / 本地看板 → 统一业务门面 → 查询或事务引擎 → 校园服务适配器 → Node 网关 → 固定认证依赖或校园站点。浏览器为独立实例。真实登录状态使用当前 Windows 用户的 DPAPI 保存，不发送到云端开发机；Linux 的本地加密存储只保护本机状态，不提供校园登录。

每个 MCP 进程拥有一个网关；任务串行排队，认证锁和状态锁不覆盖整个网络请求。取消、断线或进程崩溃后不重放已发出的请求。发现不确定的事务时先核对已有记录。本地看板是独立进程，只读、只绑定回环地址，通过一次性引导令牌建立本地会话。

## 必须保留的接口语义

- 门面只有六个公共方法：`query`、`health`、`prepare`、`execute`、`result`、`cancel`；本地导出、媒体登记和看板读取是并列的本地方法，不是校园写入。
- 能力结论以登记表的 `state`/`reason` 为准：`AVAILABLE`、`DISABLED`、`BLOCKED`、`MISSING`；代码存在不构成可用证据。
- `status/source/checked_at/complete/warnings/verification_level` 一起解释。空数组或 HTTP 200 不自动证明业务成功。
- `EMPTY` 必须同时满足 `source_complete=true` 与 `complete=true`；`PARTIAL` 保留缺失页与原因；`snapshot_complete` 只代表快照读完。
- 游标绑定参数和进程内快照，十分钟有效；过期后重新查询，不拼接不同快照。日程增量的 `change_cursor` 是另一种游标，600 秒且进程内有效，不可与分页游标互换。
- 准备操作不等于提交。提交一次后核对本人记录和精确字段；审批、付款、未知结果分别保留状态。
- `authorization` 文本不是宿主授权证据，不能伪造或绕过执行框架；真实写入需要当次具体事项的明确授权。
- 模型只在未开始响应且明确 401 时刷新重试一次；额度不足、403、网络异常或已输出不自动重放。图片输入必须有已登记协议，未验证时失败关闭。
- 本地 API 只读：没有 `prepare`/`execute`/付款/上传/密钥/模型生成路由；请求体 64 KiB、分页 50/200、刷新最短 10 秒。
- 真实凭据不得改用明文作为云端兼容方案。云端使用合成测试依赖；测试编码器与生产密钥后端必须可辨别。
- 29 个 MCP 工具、6 个旧入口点和 `thu_assistant`/`learn_assistant` 导入路径保持兼容，新增 `openthu-doctor`；新增字段可选，不删除旧字段。

## 当前已知问题与限制

1. 真实校园认证依赖 Windows DPAPI；Linux 有本地加密存储但没有生产级校园登录，不能用明文 Cookie 替代。
2. 图书馆与体育创建/取消已实现准备、执行和核对框架，真实写入尚未按具体事项完成验收。
3. 图书馆续借只有资格读取、单笔计划、官方页面移交与新到期核对；没有已验证的请求端点，`renew_loan` 执行阻塞。
4. 作业提交只有本地准备与校验，没有上传、草稿或正式提交能力；写开关 `THU_LEARN_SUBMIT` 默认关闭。
5. 个人课表、考试、成绩没有已验证来源，三个学业操作均为 `BLOCKED_SOURCE_EVIDENCE`；教学日历不能代替。
6. 模型图片输入已实现本地检查与请求管线，但协议注册表为空，当前返回 `MODEL_IMAGE_INPUT_UNAVAILABLE`。
7. 课程资料中心的公共 CLI/MCP/默认看板未注入提供者；`local.resources` 为 `RESOURCE_PROVIDER_UNAVAILABLE`，导出与媒体登记只在宿主 API 可用。
8. 亲友入校只有公开入口，没有当前表单和权限契约；通用浏览器看到表单不代表业务已接通。
9. 本地看板只读；HTML 外壳缺失时 `/` 返回 `LOCAL_API_UI_NOT_INSTALLED`，JSON 路由仍可用。
10. 学校接口会变化。历史真实读取通过仅说明当时的版本和账号，公开版不携带那些业务记录。

## 接手后的首轮工作

1. 阅读 `AGENTS.md`、[用户手册](USER_GUIDE.md)、[能力矩阵](CAPABILITIES.md)、[工具对照](TOOLS.md)、[批次升级](BATCH_UPGRADES.md)。
2. 运行 `python scripts/dev.py check`、`python scripts/dev.py test` 和 `python scripts/dev.py smoke`，确认 29 个工具、0 真实校园调用。
3. 选一个 Issue 建立任务分支，按批次屏障确认当前批次；只改本模块负责的路径。
4. 需要真实校园证据时提交清楚的本机验证请求，由本地维护者接手。不要从模拟数据推导新的站点规则。
5. 文档与能力状态同步：新增或改名登记操作必须更新 `docs/CAPABILITIES.md` 与 `docs/TOOLS.md`，否则文档一致性测试失败。

Git 的提交与 PR 是代码事实来源；交接记录是开发现场来源。私有环境文件和聊天不属于项目依赖。
