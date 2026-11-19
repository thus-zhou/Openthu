# 升级与回退

本页覆盖程序文件升级、旧状态迁移、凭据与会话边界、未决事务保留和回退。功能变化见 [`CHANGELOG.md`](../CHANGELOG.md)，能力边界见 [`CAPABILITIES.md`](CAPABILITIES.md)，首次安装见 [`USER_GUIDE.md`](USER_GUIDE.md)。

## 1. 升级前确认

| 项目 | 要求 |
|---|---|
| Python | 3.12 或 3.13（v0.2.0 起不再支持 3.10/3.11；继续使用旧环境的用户保留 v0.1） |
| Node.js | 24+，`npm ci --ignore-scripts` 只从锁文件安装 `tough-cookie` |
| 浏览器 | Playwright Chromium 是可选能力，诊断会指出是否缺失 |
| 旧版本 | 保留上一稳定版本的源码包或 wheel，回退时使用它，不要删除用户目录 |
| 状态 | 升级前记录当前状态目录、密钥位置和未决事务；不要在升级过程中清理或重置 |

安装后先运行诊断：

- 源码包：`python scripts/dev.py doctor`（含开发依赖、Node、Chromium 与源码完整性）。
- wheel / sdist：`openthu-doctor`（或 `python -m thu_assistant.doctor`）。它检查版本、包内 `_bundled` 布局、认证资源、配置与能力状态，不登录、不读取凭据、不写状态。

`release-layout` 失败表示程序文件与包内认证资源不配套或安装不完整，应重新安装而不是继续使用。升级后再运行 `openthu health` 与 `openthu dashboard --check`，确认能力登记和本地看板可启动。

## 2. 程序文件升级

程序文件可整体替换，凭据、会话、下载和未决事务都在用户私有目录，不在安装目录内。

```sh
# 源码包
python -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/dev.py setup-node
python scripts/dev.py doctor

# wheel 或 sdist
python -m pip install --force-reinstall openthu-0.2.0-py3-none-any.whl
openthu-doctor
openthu-mcp   # 或 python -m thu_assistant.mcp_stdio
```

wheel 内的认证辅助位于 `thu_assistant/_bundled/`，与源码包使用同一份资源清单生成。`node_modules` 不在 wheel 内，升级后需要时自行在包目录执行 `npm ci --ignore-scripts`（有 npm 与锁文件即可，不需要源码包）。升级不覆盖配置、密钥、会话、下载、日志或未决事务。

### 2.1 升级不会自动做的事

| 项目 | 升级行为 | 需要使用者做什么 |
|---|---|---|
| 个人凭据与密钥 | 不搬迁、不复制、不重新生成；Windows DPAPI 密文只在同一 Windows 用户下可读，Linux 密钥文件不会被复制到新位置 | 保持原账户与原密钥文件；换机器或换用户需重新建立 |
| 认证会话 | 不重建、不刷新；旧会话可能因站点策略失效 | 需要时重新完成官方认证与 MFA |
| 配置文件 | 不合并、不覆盖；新默认配置目录不会自动接管旧文件 | 需要时用 `--config PATH` 显式指定 |
| 下载与日志 | 保持原位 | 自行备份或清理 |
| 未决事务 | 不重放、不撤销、不自动确认 | 用能读取原格式的运行时核对本人记录 |
| 校园侧记录 | 不因升级而创建、取消或付款 | 任何写入仍需当次具体授权 |

升级成功不等于登录成功，也不等于旧会话仍有效。不要把“程序启动正常”写成“业务已恢复”。

## 3. 旧状态迁移（先预览，后执行）

迁移是显式动作，默认只预览：

```sh
python scripts/migrate_state.py preview            # 只读；列出格式、目标位置、兼容性、是否需重新认证
python scripts/migrate_state.py preview --json     # 机器可读
python scripts/migrate_state.py apply --dry-run    # 报告将执行的动作，不写文件
python scripts/migrate_state.py apply              # 实际复制
```

Windows 的默认状态目录仍是 `%LOCALAPPDATA%\TsinghuaAssistant`，与旧版本相同，因此不带参数运行 `preview` 会得到 `NO_MIGRATION_NEEDED`（无需迁移，退出码 0）。只有在确实要搬到另一个目录时才需要 `--to`。

规则：

- Windows DPAPI 会话密文只在同一 Windows 账户下可读；在 Linux/macOS 上标记为 `UNSUPPORTED_PLATFORM`，不复制、不解密，需重新登录。即使目标已有同名文件并选择 `replace` 也不会复制。
- `session.secure`（v3）需要原主密钥，而且必须位于新运行时读取的默认位置（配置目录下的 `keys/master.key`）；`--key-file` 指向其他位置时预览会提示迁移后仍需重新认证。密钥只由使用者在本机提供，不进入仓库或聊天。
- 真实状态目录按实际名称分类：`actions-v1/` 与 `submissions-v1/` 标为 `REVIEW_REQUIRED`（只复制记录，任何重放都必须按具体事项重新授权）；`consent-v1/` 的主机授权密钥与待消费 grant 以及 `keys/` 等密钥材料标为 `EXCLUDED_SENSITIVE`，不复制，需要时重新授权。
- 目标位置已有同名文件时为 `CONFLICT`，默认保留双方并报告 `NEEDS_DECISION`。必须显式 `apply --replace <项>` 或 `--keep <项>`；覆盖前旧目标会被复制为 `<名称>.pre-migration`。
- 复制先写 `<名称>.migrating`，校验 SHA256 后再原子替换；失败保留原文件，并在目标状态目录追加 `migration-journal.jsonl`，记录失败原因。迁移不重试、不重放、不删除旧目录。
- `downloads/` 与 `logs/` 标为 `EXCLUDED_USER_FILES`，保持原位，不复制也不改写。
- 退出码：`0` 表示迁移完整（没有失败、也没有未决定的冲突）；仍有 `NEEDS_DECISION` 时退出码为 `1`，脚本可按此判断后续动作。
- 计划文件是不可信输入：`apply` 会拒绝相对/越界的目标目录、程序资源目录、落在旧目录内的目标，以及任何越出旧目录的源路径。

### 3.1 状态格式不兼容时

状态格式（`secure-storage-header-v1`、`THU-TODOS-1`、`THU-ACTION-1`）不兼容时，不要用“删掉状态目录”换取干净启动。按以下顺序保留恢复能力：

1. 停止新版本进程，保留整个状态目录、密钥文件、`.pre-migration` 备份和 `migration-journal.jsonl`。
2. 保留一个能读取该格式的运行时：上一稳定版本的源码包或 wheel，加上同一 Python 主版本、同一 Node 主版本和同一密钥位置。运行时和密钥必须匹配，单独复制密文没有意义。
3. 用旧运行时只读核对未决事务、提交计划和会话状态；需要重放或撤回时按具体事项重新授权，不自动重试。
4. 确认新版本能读取旧格式后，再决定是否删除旧副本；不能确认时继续保留两份。
5. 需要重新建立会话时，在 Windows 上重新完成官方认证；Linux 没有生产登录，不能以明文 Cookie 代替。

## 4. 回退

1. 用上一稳定版本的源码包或 wheel 重新安装程序文件。
2. 旧状态目录在迁移中从未被删除，可直接继续使用；迁移产生的目标副本和 `.pre-migration` 备份由使用者自行决定保留。
3. 若新版本写入过状态，先确认旧运行时能读取该格式；不能确认时保留副本，并用能读取它的运行时恢复，不要用“删除用户目录”换取干净启动。
4. 回退保留未决事务与密钥：`actions-v1/`、`submissions-v1/` 的记录仍需人工核对后按具体事项重新授权；`consent-v1/` 的主机授权密钥与 grant 不随迁移复制，需要重新授权。
5. 回退不改变任何校园侧的预约、提交或付款记录。已经发生的远端业务不会因本地回退而撤销。

## 5. 候选构建与核对

```sh
python scripts/package_release.py build
python scripts/package_release.py verify            # 默认要求产物 commit == HEAD
python scripts/package_release.py verify --expected-commit <SHA>
python scripts/package_release.py install-check
```

`build` 从已提交的 Git 内容生成确定性源码归档、sdist 与 wheel，全部成功后才原子替换 `dist/`（失败保留上一份候选），并写出 `release-manifest.json` 与 `SHA256SUMS`。`verify` 核对版本、产物与 `HEAD` 的提交绑定、`Requires-Python`、`Requires-Dist`、7 个入口点、包内资源与只读模式、许可证精确路径、校验和与 CHANGELOG 候选说明；`install-check` 在仓库外的临时虚拟环境安装 wheel、运行 `openthu-doctor`、启动 29 个 MCP 工具，并确认用户状态目录未被改写。以上均为候选准备，不代表发布，也不代表真实校园业务可用。

## 6. 升级后的最小核对

```sh
openthu-doctor                 # 或 python scripts/dev.py doctor
openthu health                 # 本地能力登记；不探测站点
openthu describe --service library
openthu dashboard --check      # 只读看板可启动
```

`health` 的 `PARTIAL` 表示有已登记但被阻塞的操作，不是升级失败；`BLOCKED`/`MISSING` 的原因以 `describe` 为准。升级后不要用一次成功的静态目录查询推断登录、权限或真实业务已恢复。
