# 安装与配置

本页覆盖安装、诊断、配置来源与运行边界。第一次使用请按[用户手册](USER_GUIDE.md)的顺序操作；升级、迁移与回退见[升级与回退](UPGRADE.md)。

## 首次安装诊断

完成依赖安装后，先运行：

```sh
python scripts/dev.py doctor
```

它逐项检查：

| 检查项 | 内容与修复入口 |
|---|---|
| Python | 版本（最低版本读取 `pyproject.toml`，当前为 3.12）与虚拟环境提示 |
| Python 开发依赖 | `requirements-dev.txt` 与可编辑安装的 `openthu` |
| Node.js / npm | Node.js 24+ 与 npm 是否可执行 |
| 锁文件运行依赖 | `package-lock.json` 固定的 tough-cookie 是否已由 `setup-node` 安装且版本一致 |
| 只读校园服务目录 | `services.json` 是否可读；只证明静态目录存在，不证明登录或权限 |
| Playwright Chromium | 浏览器相关测试所需的 Chromium 是否可执行；缺少时为警告 |
| Windows 生产认证 | Windows 上检查 `runtime/windows/access.js` 与认证依赖；Linux 上为“不适用” |
| 源码包完整性 | 当前检出是否包含 `vendor/`、`runtime/windows/`、`scripts/` 与 `tests/` |
| 统一配置 | 配置来源、类型、未知/重复键、秘密字段与路径范围 |
| 私有目录边界 | 状态、下载、临时、日志目录是否在程序资源和密钥目录之外 |

条目状态为“通过 / 警告 / 失败 / 不适用”，失败条目仅在阻塞时影响退出码。`python scripts/dev.py doctor --json` 输出机器可读 JSON；`--config PATH` 指定显式配置文件。退出码 0 表示没有阻塞项，1 表示需要先处理。

默认报告只含逻辑检查项、脱敏版本、仓库相对资源名、状态和修复命令，不含绝对路径、主机名、账号、凭据或命令原始输出，可直接用于公开 Issue。诊断不登录校园账户、不读取凭据、不发起模型请求。

`python scripts/dev.py check` 会先运行同一诊断；未通过时直接打印修复提示并退出。

## 配置来源与路径规则

配置按以下顺序覆盖，前一层优先：

1. 命令行明确参数（当前提供 `--config PATH`，配置对象 API 也接受显式字段）。
2. `OPENTHU_*` 项目环境变量；`THU_ASSISTANT_STATE_DIR`、`THU_NODE`、`THU_PYTHON` 作为兼容别名。
3. 本机 JSON 配置文件。默认位置为 Windows `%APPDATA%\Openthu\config.json`，Linux `$XDG_CONFIG_HOME/openthu/config.json`；可用 `OPENTHU_CONFIG_FILE` 覆盖。
4. 安全默认值。

示例文件见 [`configs/openthu.example.json`](../configs/openthu.example.json)。配置分为 `resources`、`storage`、`runtime`、`network`、`ui` 五组；未知键、重复键、非法类型、超出范围和不可读文件都会返回稳定错误，错误只显示相对配置项名称，不回显值或绝对路径。显式配置无效时不会悄悄切换旧目录或另一账号。

秘密不属于配置文件：需要秘密的模块只能通过 `Configuration.read_secret("<name>")` 读取 `OPENTHU_SECRET_<NAME>`，不会从文件、命令行或公开摘要中合并。`network.listen_host` 只接受回环地址；`network.npm_registry` 拒绝嵌入用户名/密码。

`storage.state_dir`、`download_dir`、`temp_dir`、`log_dir` 必须是绝对路径，支持 `~` 展开并会规范化。它们不能位于源码包、安装包资源、`.git` 或状态目录的 `keys/`、`keyrings/`、`secrets/`、`credentials/` 之下；相对路径会被拒绝。默认状态目录在代码库之外；迁移只提供旧位置到新位置的预览，本模块不搬移真实会话或凭据。旧 Windows DPAPI 记录仍由后续认证模块按原格式读取，不因路径变更自动迁移。

Linux 的本地加密存储需要独立的外部密钥文件，不能把密钥放进数据目录。密钥文件不是任意 32 字节随机文件，必须包含 `THU-KEY-1\0` 文件头和 32 字节随机主密钥；用 `create_key_file()` 创建：

```sh
python - <<'PY'
from thu_assistant.secure_store import create_key_file
create_key_file("/absolute/owner-only/master.key", "file-key")
PY
export THU_SECURE_KEY_FILE=/absolute/owner-only/master.key
export THU_SECURE_DATA_DIR=/absolute/user/state
```

`create_key_file()` 拒绝覆盖已有文件，父目录不存在时以 `0700` 创建；文件必须是当前用户所有、`0600`、普通文件且不是符号链接。缺少密钥时相关写入被拒绝（`SECURE_KEY_NOT_CONFIGURED`）；格式或权限不符得到 `SECURE_KEY_FORMAT` 或权限错误。程序不会自动生成密钥，也不会退化为明文。`THU_SECURE_BACKEND` 选择提供者，`THU_SECURE_RETIRED_KEY_FILES` 用于轮换后的旧记录读取。Windows 使用当前用户 DPAPI，不读取 Linux 密钥文件。

## 安装来源与能力边界

| 来源 | 内容 | 能做什么 / 不能做什么 |
|---|---|---|
| 完整源码包（`git clone` 或 Release 源码归档） | Python/Node 源码、`vendor/madmodel-proxy`、`runtime/windows`、合成测试、开发脚本 | 可做云端离线开发、Windows 真实认证与运行、贡献；真实写入仍需按具体事项验收 |
| Python wheel / sdist | Python 包、静态服务目录、兼容命令，以及打包进 `thu_assistant/_bundled/` 的 Windows 认证辅助、固定上游 Proxy 与许可证 | 可做静态目录、库调用和认证辅助解析；`node_modules` 与离线测试不在包内，需要时自行 `npm ci --ignore-scripts`；安装后用 `openthu-doctor` 自检 |
| 云端离线开发检出 | 完整源码、Python、Node.js 24、Chromium | 可运行 `doctor` / `check` / `smoke` / `test`；不访问校园账户，不保存或导入 Windows 凭据 |

运行时功能差异主要是 `node_modules`、离线测试和开发脚本；wheel 用 `thu_assistant/_bundled/` 携带认证辅助与固定上游代码，不包含 `docs/`、`plans/`、`contracts/`、`tests/`、`scripts/` 和顶层 `vendor/`/`runtime/` 等源码树资料。`doctor` 的 `release-layout` 检查会核对程序版本与包内资源配置是否配套，混装旧版本会直接阻止启动。

## 云端开发

按 README 创建虚拟环境并安装依赖，然后先运行 `python scripts/dev.py doctor` 确认环境可用。`python scripts/dev.py setup-node` 从锁文件恢复 tough-cookie 运行依赖；`python -m playwright install --with-deps chromium` 在 Linux 安装合成浏览器测试所需组件。`python scripts/dev.py test` 设置隔离的临时状态目录并运行 Python/Node 离线测试，不连接校园账户。

Node 依赖默认从官方 npm registry 安装，版本和完整性哈希由锁文件固定。网络环境确有需要时，可显式设置 `OPENTHU_NPM_REGISTRY` 使用自行选定的镜像；不要把个人 npm 认证配置提交到仓库。

事务测试在 Windows 使用真实 DPAPI；Linux 的合成用例显式注入测试专用可逆编码器，以验证事务和并发逻辑。该编码器只在 tests/，不进入生产程序。另有测试确保 Linux 的生产默认仍拒绝无法加密的写入。

`python scripts/dev.py smoke` 验证 MCP 初始化、29 个工具和静态目录。它不验证登录、权限或真实业务。云端 Hermes 在此目录工作即可，不需要读取任何人的私有配置。

## Windows 真实运行

1. 同样安装 Python/Node 依赖和 Chromium，运行 `python scripts/dev.py doctor` 确认 Windows 认证辅助与依赖就位。`doctor` 通过只代表文件可用，不代表已登录或已取得业务权限。保持所有认证文件在版本库之外的用户状态目录。
2. 先阅读 `vendor/madmodel-proxy/README.md`，按该公开上游项目的官方流程在本机建立自己的认证。可使用单独安装的同版本上游，并通过 `THU_MADMODEL_DIR` 指定其代码目录。不要把密码或 Cookie 贴到 AI 聊天中。
3. 使用 `python scripts/configure_mcp.py --output .local/mcp.json` 生成当前机器专用 MCP 配置；该输出被 Git 忽略。客户端导入生成的配置，或使用 `openthu-mcp` 命令。
4. 第一次业务读取按官方登录/MFA 流程完成。模型调用使用使用者自己的学校额度。Linux 没有生产校园登录，实时认证会返回 Windows 加密依赖错误；Linux 的外部密钥只用于本地状态加密，不能替代校园会话。
5. 先查状态和只读结果，再进行有明确具体授权的业务。预约和取消的真实验收状态见能力矩阵。
6. 安装后最小核对：`openthu-doctor`（或 `python scripts/dev.py doctor`）、`openthu health`、`openthu describe --service library`、`openthu dashboard --check`。`health` 的 `PARTIAL` 表示有已登记但被阻塞的操作，不是安装失败。

生成器默认使用当前 Python、可找到的 Node 与仓库中的辅助程序；不读取旧安装配置或复制凭据。正式运行与离线测试使用不同的状态目录。更换代码、回退版本时保留未决加密事务及能读取它们的运行环境。

## 下载与构建

GitHub Releases 提供完整源码包、校验文件及构建包。完整源码归档包含认证依赖、Windows 辅助代码和测试；wheel 与 sdist 通过同一份资源清单把认证辅助与固定上游代码装进 `thu_assistant/_bundled/`，`node_modules` 和离线测试仍只在源码包中。两者的能力边界见上文“安装来源与能力边界”；`doctor` 的 `release-layout` 与源码包完整性检查会提示混装或缺少的资源。

```sh
python scripts/package_release.py build         # dist/：源码 zip/tar.gz、sdist、wheel、SHA256SUMS、release-manifest.json
python scripts/package_release.py verify        # 重新核对版本、校验和、包内资源与许可证
python scripts/package_release.py install-check # 在仓库外的临时虚拟环境安装 wheel 并启动 MCP
```

构建从已提交的 Git 内容取文件：工作区换行、文件时间或未跟踪的缓存/日志/密钥不会进入任何归档；跟踪树不干净时构建直接拒绝。版本号来自 `src/thu_assistant/version.py`，包元数据、`release-manifest.json`、`SHA256SUMS` 与候选说明必须一致。预发布不能视为所有校园系统都已可用。
