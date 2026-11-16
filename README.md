# Openthu

开放的清华校园服务助手。通过 MCP 或命令行连接网络学堂、图书馆、体育场馆、校园服务目录与学校模型服务。

这是独立社区项目，与清华大学官方无隶属关系。软件可由所有人下载、修改和分发（MIT）；各校园服务仍以使用者自身账号、资格和官方规则为准。

**当前版本：0.1.0 预览版。** 云端支持开发和离线测试；真实校园会话目前依赖 Windows DPAPI。预约和取消已有适配及模拟验证，真实写入仍需按具体事项验收。

## 开始使用

需要 Python 3.12+、Node.js 24、Git。Windows 和 Linux 均可运行开发测试。

```sh
git clone https://github.com/thus-zhou/Openthu.git
cd Openthu
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# PowerShell: .venv/Scripts/Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python scripts/dev.py setup-node
python -m playwright install chromium
python scripts/dev.py doctor
python scripts/dev.py check
python scripts/dev.py test
```

离线演示：`openthu services`。该命令只读随程序提供的静态服务目录，不证明登录或校园业务可用。

**真实使用：** 阅读[安装与配置](docs/INSTALL.md)。不要向聊天、Issue 或仓库提交学校密码、Cookie、模型凭据或个人业务记录。默认安装不会搬迁任何人的认证状态。

## 首次安装诊断

安装依赖后先运行 `python scripts/dev.py doctor`。它按 `development`、`readonly`、`browser`、`production-auth`、`packaging` 和 `config` 分类检查 Python 版本与虚拟环境、开发包、Node.js/npm、`src/thu_assistant/package-lock.json` 固定的运行依赖、只读服务目录、Playwright Chromium、Windows 生产认证条件、源码包布局和配置；每项给出可执行修复命令。

条目状态为“通过 / 警告 / 失败 / 不适用”。失败条目只有标记为阻塞时才阻止开始；Linux 上 Windows 生产认证为“不适用”，缺少 Chromium 只影响浏览器能力，不把合法离线安装误判为损坏。Python 最低版本取自 `pyproject.toml`，已验证版本不是唯一可用版本。

诊断默认输出只包含逻辑检查项、脱敏版本、仓库相对资源名、状态和修复命令，不含绝对路径、主机名、账号、凭据或命令原始输出，可直接贴到公开 Issue。`python scripts/dev.py doctor --json` 提供机器可读结果；`python scripts/dev.py check` 会先运行同一诊断，再执行隐私扫描、源码编译和 MCP 冒烟检查。显式配置无效时使用 `--config PATH` 重新指定，不会静默回退到旧目录或另一账号。

## 配置与路径

配置优先级固定为：命令行明确参数、`OPENTHU_*` 环境变量、本机 JSON 配置文件、安全默认值。不同来源不隐式合并秘密；秘密只能由具体模块通过 `Configuration.read_secret()` 从 `OPENTHU_SECRET_<NAME>` 读取，不进入配置文件或公开序列化结果。

默认配置文件位于各平台的应用配置目录（Windows 为 `%APPDATA%\Openthu\config.json`，Linux 为 `$XDG_CONFIG_HOME/openthu/config.json`）。可用 `--config PATH` 或 `OPENTHU_CONFIG_FILE` 指定；显式指定但不可读会报错，不会改用其他文件。示例见 [`configs/openthu.example.json`](configs/openthu.example.json)。

常用环境变量包括 `OPENTHU_STATE_DIR`、`OPENTHU_DOWNLOAD_DIR`、`OPENTHU_TEMP_DIR`、`OPENTHU_LOG_DIR`、`OPENTHU_NODE`、`OPENTHU_PYTHON`、`OPENTHU_NPM_REGISTRY`、`OPENTHU_LISTEN_HOST`、`OPENTHU_LISTEN_PORT`、`OPENTHU_PAGE_SIZE` 和 `OPENTHU_REFRESH_SECONDS`。旧调用者的 `THU_ASSISTANT_STATE_DIR`、`THU_NODE`、`THU_PYTHON` 仍作为低优先级别名可用；规范化环境变量优先。

状态、下载、临时和日志目录必须是绝对路径，支持 `~` 展开并会规范化。默认状态目录在代码库之外；用户自选位置不得指向源码包、程序资源、`.git` 或密钥子目录，相对路径会被拒绝。状态目录迁移只提供预览和目标，本版本不搬移任何真实会话或凭据。配置对象公开摘要只含来源标签和安全的非路径值。

## 获取方式与能力边界

| 获取方式 | 包含内容 | 适用场景 |
|---|---|---|
| 完整源码包（`git clone` 或 GitHub Release 源码归档） | Python/Node 源码、`vendor/madmodel-proxy`、`runtime/windows`、合成测试 | 云端离线开发、Windows 真实运行、贡献代码 |
| Python wheel（`python -m build` 生成的 `.whl`） | Python 包、静态服务目录、兼容命令 | 静态目录与库调用；不含 Node 锁文件、认证依赖和 Windows 辅助，不能完成真实校园认证 |
| 云端离线开发检出 | 完整源码、Python 3.12、Node.js 24、Playwright Chromium | `doctor` / `check` / `test` / `smoke`；不访问校园账户，不保存 Windows 凭据 |

真实校园认证目前依赖 Windows DPAPI。云端开发机只做开发与离线验证，不应导入或改用明文凭据。

## 项目资料

- [完整开发交接](docs/HANDOVER.md)：架构、代码入口、接口契约和已知限制。
- [能力与验证状态](docs/CAPABILITIES.md)：区分实现、模拟测试、历史实测和真实写入。
- [人工接力](docs/MANUAL_RELAY.md)：Hermes 与 Codex 使用 Issue、分支和 PR 交接。
- [完整升级批次](docs/BATCH_UPGRADES.md)：Hermes 连续完成整套升级或版本，再交 Codex 整体审查和发布。
- [Hermes 接手入口](docs/HERMES_START.md)：云端 AI 的阅读顺序与工作步骤。
- [维护与发布](docs/MAINTENANCE.md)：更新、Release、问题管理、回滚及职责。
- [开发计划](docs/ROADMAP.md)、[贡献指南](CONTRIBUTING.md)、[隐私与安全](SECURITY.md)。

Python 包名为 `openthu`；为兼容既有客户端保留 `thu_assistant`、`learn_assistant` 导入与原命令名。现有 MCP 协议标识保留 `tsinghua-assistant`，公共项目和插件显示名为 Openthu。

第三方代码及许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。
