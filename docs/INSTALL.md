# 安装与配置

## 首次安装诊断

完成依赖安装后，先运行：

```sh
python scripts/dev.py doctor
```

它逐项检查：

| 检查项 | 内容与修复入口 |
|---|---|
| Python | 版本（最低 3.10，已验证 3.12）与是否在虚拟环境 |
| Python 开发依赖 | `requirements-dev.txt` 与可编辑安装的 `openthu` |
| Node.js / npm | Node.js 24+ 与 npm 是否可执行 |
| 锁文件运行依赖 | `package-lock.json` 固定的 tough-cookie 是否已由 `setup-node` 安装且版本一致 |
| Playwright Chromium | 浏览器相关测试所需的 Chromium 是否可执行 |
| Windows 生产认证 | Windows 上检查 `runtime/windows/access.js` 与认证依赖；Linux 上明确标为限制 |
| 源码包完整性 | 当前检出是否包含 `vendor/`、`runtime/windows/`、`scripts/` 与 `tests/` |

每项缺失都会给出修复命令。退出码 0 表示没有阻塞项，1 表示需要先处理。`python scripts/dev.py doctor --json` 输出机器可读 JSON。

默认报告只含仓库相对名称、版本、状态和修复命令，不含绝对路径、主机名、账号或凭据，可直接用于公开 Issue。诊断不登录校园账户、不读取凭据、不发起模型请求。

`python scripts/dev.py check` 会先运行同一诊断；未通过时直接打印修复提示并退出。

## 安装来源与能力边界

| 来源 | 内容 | 能做什么 / 不能做什么 |
|---|---|---|
| 完整源码包（`git clone` 或 Release 源码归档） | Python/Node 源码、`vendor/madmodel-proxy`、`runtime/windows`、合成测试、开发脚本 | 可做云端离线开发、Windows 真实认证与运行、贡献；真实写入仍需按具体事项验收 |
| Python wheel（`python -m build` 生成的 `.whl`） | Python 包、静态服务目录、兼容命令 | 可做静态目录与库调用；不含 Node 锁文件、`vendor/`、`runtime/windows/`，不能完成真实校园认证 |
| 云端离线开发检出 | 完整源码、Python、Node.js 24、Chromium | 可运行 `doctor` / `check` / `smoke` / `test`；不访问校园账户，不保存或导入 Windows 凭据 |

## 云端开发

按 README 创建虚拟环境并安装依赖，然后先运行 `python scripts/dev.py doctor` 确认环境可用。`python scripts/dev.py setup-node` 从锁文件恢复 tough-cookie 运行依赖；`python -m playwright install --with-deps chromium` 在 Linux 安装合成浏览器测试所需组件。`python scripts/dev.py test` 设置隔离的临时状态目录并运行 Python/Node 离线测试，不连接校园账户。

Node 依赖默认从官方 npm registry 安装，版本和完整性哈希由锁文件固定。网络环境确有需要时，可显式设置 `OPENTHU_NPM_REGISTRY` 使用自行选定的镜像；不要把个人 npm 认证配置提交到仓库。

事务测试在 Windows 使用真实 DPAPI；Linux 的合成用例显式注入测试专用可逆编码器，以验证事务和并发逻辑。该编码器只在 tests/，不进入生产程序。另有测试确保 Linux 的生产默认仍拒绝无法加密的写入。

`python scripts/dev.py smoke` 验证 MCP 初始化、29 个工具和静态目录。它不验证登录、权限或真实业务。云端 Hermes 在此目录工作即可，不需要读取任何人的私有配置。

## Windows 真实运行

1. 同样安装 Python/Node 依赖和 Chromium，运行 `python scripts/dev.py doctor` 确认 Windows 认证辅助与依赖就位。`doctor` 通过只代表文件可用，不代表已登录或已取得业务权限。保持所有认证文件在版本库之外的用户状态目录。
2. 先阅读 `vendor/madmodel-proxy/README.md`，按该公开上游项目的官方流程在本机建立自己的认证。可使用单独安装的同版本上游，并通过 `THU_MADMODEL_DIR` 指定其代码目录。不要把密码或 Cookie 贴到 AI 聊天中。
3. 使用 `python scripts/configure_mcp.py --output .local/mcp.json` 生成当前机器专用 MCP 配置；该输出被 Git 忽略。客户端导入生成的配置，或使用 `openthu-mcp` 命令。
4. 第一次业务读取按官方登录/MFA 流程完成。模型调用使用使用者自己的学校额度。当前 Linux 实时认证会明确返回 Windows 加密依赖错误。
5. 先查状态和只读结果，再进行有明确具体授权的业务。预约和取消的真实验收状态见能力矩阵。

生成器默认使用当前 Python、可找到的 Node 与仓库中的辅助程序；不读取旧安装配置或复制凭据。正式运行与离线测试使用不同的状态目录。更换代码、回退版本时保留未决加密事务及能读取它们的运行环境。

## 下载与构建

GitHub Releases 提供完整源码包、校验文件及构建包。完整源码归档包含认证依赖、Windows 辅助代码和测试；单独 Python wheel 不包含仓库级认证依赖，实时功能请使用完整源码包。两者的能力边界见上文“安装来源与能力边界”；`doctor` 的源码包完整性检查会提示缺少 `vendor/` 或 `runtime/windows/` 的检出。

`python -m build` 构建 Python 包；`python scripts/package_release.py` 在 `dist/` 生成源码归档与 SHA256。预发布不能视为所有校园系统都已可用。
