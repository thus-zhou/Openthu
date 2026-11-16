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
python scripts/dev.py check
python scripts/dev.py test
```

离线演示：`openthu services`。该命令只读随程序提供的静态服务目录，不证明登录或校园业务可用。

**真实使用：** 阅读[安装与配置](docs/INSTALL.md)。不要向聊天、Issue 或仓库提交学校密码、Cookie、模型凭据或个人业务记录。默认安装不会搬迁任何人的认证状态。

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
