# Openthu

开放的清华校园服务助手。通过 MCP 或命令行连接网络学堂、图书馆、体育场馆、校园服务目录与学校模型服务。

这是独立社区项目，与清华大学官方无隶属关系。软件可由所有人下载、修改和分发（MIT）；各校园服务仍以使用者自身账号、资格和官方规则为准。

**版本状态：已发布 0.1.0 预览版；仓库当前打包 0.2.0，属于未发布候选。** 云端支持开发和离线测试；真实校园认证目前仍依赖 Windows 的当前用户 DPAPI。Linux 有独立的加密存储（外部密钥文件），但没有生产级校园登录。图书馆与体育的创建/取消已有专用适配和合成验证，真实写入仍需按具体事项授权与验收。

**先读这三份，再动手：**

- [用户手册](docs/USER_GUIDE.md)：安装、认证、只读查询、看板、日程、资料中心、图片输入与授权办理。
- [能力与验证状态](docs/CAPABILITIES.md)：按登记操作分列代码、环境、协议与真实业务证据。
- [MCP 工具与登记操作](docs/TOOLS.md)：29 个工具、旧别名和新操作的对照。

## 快速开始

需要 Python 3.12 或 3.13、Node.js 24、Git。

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
python scripts/dev.py check
python scripts/dev.py test
```

离线演示：`openthu services --query 图书馆`。该命令只读随程序提供的静态服务目录，不证明登录或校园业务可用。

使用 wheel 或 sdist 时，安装后运行 `openthu-doctor` 自检；它检查版本、包内资源布局、配置与能力状态，不登录、不读取凭据、不写状态。

## 第一次使用

完整步骤与预期状态见[用户手册](docs/USER_GUIDE.md)。最短路径：

1. **诊断**：`python scripts/dev.py doctor`（源码）或 `openthu-doctor`（安装包）。失败条目只有标记为阻塞时才阻止开始；Linux 上 Windows 生产认证为“不适用”，缺少 Chromium 只影响浏览器能力。
2. **建立本人认证**：Windows 按官方统一认证与 MFA 流程完成；Linux 只配置本地加密存储，不提供生产校园登录，不能用明文 Cookie 代替。
3. **只读查询**：先 `openthu describe --service library` 看登记操作，再 `openthu query --service learn --operation courses`。`openthu health` 只报告本地能力，不探测真实站点。
4. **看板**：`openthu dashboard --check` 验证可启动；`openthu dashboard --port 8765` 常驻。只绑定回环地址，只提供只读 API。
5. **授权办理**：`prepare_service_action` → 使用者在可信宿主确认 → `execute_service_action` → `get_action_result`。准备不等于提交，`authorization` 文本不是授权证据。

## 命令行

`openthu` 保留原有 `services`、`discover`、`login`、`read`、`courses`、`model` 子命令和参数，新增：

- `openthu describe [--service ID]`：业务门面的能力登记、阻塞原因与本地方法。
- `openthu health [--service ID]`：本地能力健康；只报告静态登记，不探测真实站点。
- `openthu query --service ID --operation OP [--parameters JSON|@file] [--cursor C] [--page-size N]`：执行一个已登记的只读查询。
- `openthu agenda [--start-date D] [--end-date D] [--services LIST] [--semester S] [--export PATH] [--format ics|markdown|json] [--overwrite] [--allow-partial]`：按需读取统一日程；导出只写本地文件，必须给绝对路径。
- `openthu dashboard [--host 127.0.0.1] [--port N] [--check]`：启动本地只读看板；先输出监听地址与实例号再常驻；`--check` 输出同样信息后立即停止；提供者未安装时返回 `LOCAL_DASHBOARD_UNAVAILABLE`。

迁移说明：原来的位置参数 `operation` 现在是子命令，全局参数必须写在子命令之后；成功输出新增顶层 `command` 字段；错误对象从 `{"code": ...}` 扩展为带 `category`、`retryable`、`automatic_replay_allowed`、`user_action` 的分类错误。旧调用如 `openthu read --url ...` 仍按原字段返回。

## 首次安装诊断

安装依赖后先运行 `python scripts/dev.py doctor`。它按 `development`、`readonly`、`browser`、`production-auth`、`packaging` 和 `config` 分类检查 Python 版本与虚拟环境、开发包、Node.js/npm、`src/thu_assistant/package-lock.json` 固定的运行依赖、只读服务目录、Playwright Chromium、Windows 生产认证条件、源码包布局、发行布局和配置；每项给出可执行修复命令。

条目状态为“通过 / 警告 / 失败 / 不适用”。失败条目只有标记为阻塞时才阻止开始；Linux 上 Windows 生产认证为“不适用”，缺少 Chromium 只影响浏览器能力，不把合法离线安装误判为损坏。Python 最低版本取自 `pyproject.toml`，已验证版本不是唯一可用版本。

诊断默认输出只包含逻辑检查项、脱敏版本、仓库相对资源名、状态和修复命令，不含绝对路径、主机名、账号、凭据或命令原始输出，可直接贴到公开 Issue。`python scripts/dev.py doctor --json` 提供机器可读结果；`python scripts/dev.py check` 会先运行同一诊断，再执行隐私扫描、源码编译和 MCP 冒烟检查。显式配置无效时使用 `--config PATH` 重新指定，不会静默回退到旧目录或另一账号。

## 配置与路径

配置优先级固定为：命令行明确参数、`OPENTHU_*` 环境变量、本机 JSON 配置文件、安全默认值。不同来源不隐式合并秘密；秘密只能由具体模块通过 `Configuration.read_secret()` 从 `OPENTHU_SECRET_<NAME>` 读取，不进入配置文件或公开序列化结果。

默认配置文件位于各平台的应用配置目录（Windows 为 `%APPDATA%\Openthu\config.json`，Linux 为 `$XDG_CONFIG_HOME/openthu/config.json`）。可用 `--config PATH` 或 `OPENTHU_CONFIG_FILE` 指定；显式指定但不可读会报错，不会改用其他文件。示例见 [`configs/openthu.example.json`](configs/openthu.example.json)。

常用环境变量包括 `OPENTHU_STATE_DIR`、`OPENTHU_DOWNLOAD_DIR`、`OPENTHU_TEMP_DIR`、`OPENTHU_LOG_DIR`、`OPENTHU_NODE`、`OPENTHU_PYTHON`、`OPENTHU_NPM_REGISTRY`、`OPENTHU_LISTEN_HOST`、`OPENTHU_LISTEN_PORT`、`OPENTHU_PAGE_SIZE` 和 `OPENTHU_REFRESH_SECONDS`。旧调用者的 `THU_ASSISTANT_STATE_DIR`、`THU_NODE`、`THU_PYTHON` 仍作为低优先级别名可用；规范化环境变量优先。

Linux 本地加密存储另需 `THU_SECURE_KEY_FILE`（`0600` 外部密钥文件）与 `THU_SECURE_DATA_DIR`（数据目录），两者必须分离；缺少密钥时拒绝写入，不自动生成密钥或退化为明文。Windows 使用当前用户 DPAPI，不导入其他机器或账号的凭据。

状态、下载、临时和日志目录必须是绝对路径，支持 `~` 展开并会规范化。默认状态目录在代码库之外；用户自选位置不得指向源码包、程序资源、`.git` 或密钥子目录，相对路径会被拒绝。旧状态只通过 `python scripts/migrate_state.py preview` 只读预览；`apply` 需要逐项明确 `replace`/`keep`，只复制不移动，失败保留原文件与日志，下载与日志保持原位。升级不会自动搬迁个人凭据、重建会话或撤销远端业务。

## 获取方式与能力边界

| 获取方式 | 包含内容 | 适用场景 |
|---|---|---|
| 完整源码包（`git clone` 或 GitHub Release 源码归档） | Python/Node 源码、`vendor/madmodel-proxy`、`runtime/windows`、合成测试 | 云端离线开发、Windows 真实运行、贡献代码 |
| Python wheel / sdist（`python scripts/package_release.py build` 生成） | Python 包、静态服务目录、兼容命令，以及 `thu_assistant/_bundled/` 内的 Windows 认证辅助、固定上游 Proxy 与许可证 | 静态目录、库调用与认证辅助解析；`node_modules` 与离线测试不在包内，需要时自行 `npm ci --ignore-scripts`，安装后用 `openthu-doctor` 自检 |
| 云端离线开发检出 | 完整源码、Python 3.12、Node.js 24、Playwright Chromium | `doctor` / `check` / `test` / `smoke`；不访问校园账户，不保存 Windows 凭据 |

真实校园认证目前依赖 Windows DPAPI。云端开发机只做开发与离线验证，不应导入或改用明文凭据。Linux 的加密存储只保护本机状态，不提供校园登录。

能力矩阵以登记操作为主键；文档中的“已实现”“已登记”“真实业务有证据”是三件不同的事。带 `PARTIAL`、`UNKNOWN`、`PENDING_PAYMENT`、`PENDING_APPROVAL` 的结果都不是成功。

## 项目资料

- [用户手册](docs/USER_GUIDE.md)：首次安装、认证、查询、看板、日程、资料、图片与办理。
- [安装与配置](docs/INSTALL.md)：诊断分类、配置优先级、来源边界与构建。
- [升级与回退](docs/UPGRADE.md)：程序、配置、密钥、会话、下载与未决事务的关系。
- [能力与验证状态](docs/CAPABILITIES.md)：按登记操作区分实现、环境、协议和真实证据。
- [MCP 工具与登记操作](docs/TOOLS.md)：29 个工具与旧别名。
- [完整开发交接](docs/HANDOVER.md)：架构、代码入口、接口契约和已知限制。
- [人工接力](docs/MANUAL_RELAY.md)：Hermes 与 Codex 使用 Issue、分支和 PR 交接。
- [完整升级批次](docs/BATCH_UPGRADES.md)：批次屏障、共享文件负责制与单一版本 PR。
- [Hermes 接手入口](docs/HERMES_START.md)：云端 AI 的阅读顺序与工作步骤。
- [维护与发布](docs/MAINTENANCE.md)：更新、Release、问题管理、回滚及职责。
- [开发计划](docs/ROADMAP.md)、[贡献指南](CONTRIBUTING.md)、[隐私与安全](SECURITY.md)。

Python 包名为 `openthu`；为兼容既有客户端保留 `thu_assistant`、`learn_assistant` 导入与原命令名。现有 MCP 协议标识保留 `tsinghua-assistant`，公共项目和插件显示名为 Openthu。

第三方代码及许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。
