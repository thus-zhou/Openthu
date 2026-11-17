# 升级与状态迁移

本页只覆盖安装布局和旧状态迁移。功能变化见 [`CHANGELOG.md`](../CHANGELOG.md)，能力边界见 [`CAPABILITIES.md`](CAPABILITIES.md)。

## 1. 升级前确认

| 项目 | 要求 |
|---|---|
| Python | 3.12 或 3.13（v0.2.0 起不再支持 3.10/3.11；继续使用旧环境的用户保留 v0.1） |
| Node.js | 24+，`npm ci --ignore-scripts` 只从锁文件安装 `tough-cookie` |
| 浏览器 | Playwright Chromium 是可选能力，`doctor` 会指出是否缺失 |
| 旧版本 | 保留上一稳定版本的源码包或 wheel，回退时使用它，不要删除用户目录 |

安装后运行 `python scripts/dev.py doctor`：`release-layout` 通过表示程序文件与包内认证资源版本一致；`capabilities` 汇总指出 `readonly`、`browser`、`production-auth` 等能力当前是否可用。

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
python -m thu_assistant.mcp_stdio   # 或使用 openthu-mcp 入口
```

wheel 内的认证辅助位于 `thu_assistant/_bundled/`，与源码包使用同一份资源清单生成；Node 运行依赖仍需单独安装，`doctor` 的 `node-runtime` 项会给出 `setup-node` 提示。升级不覆盖配置、密钥、会话、下载、日志或未决事务。

## 3. 旧状态迁移（先预览，后执行）

迁移是显式动作，默认只预览：

```sh
python scripts/migrate_state.py preview            # 只读；列出格式、目标位置、兼容性、是否需重新认证
python scripts/migrate_state.py preview --json     # 机器可读
python scripts/migrate_state.py apply --dry-run    # 报告将执行的动作，不写文件
python scripts/migrate_state.py apply              # 实际复制
```

规则：

- Windows DPAPI 密文只在同一 Windows 账户下可读；在 Linux/macOS 上标记为 `UNSUPPORTED_PLATFORM`，不复制、不解密，需重新登录。
- `session.secure`（v3）需要原主密钥；没有密钥时迁移后需要重新认证。密钥只由使用者在本机提供，不进入仓库或聊天。
- 目标位置已有同名文件时为 `CONFLICT`，默认保留双方并报告 `NEEDS_DECISION`。必须显式 `apply --replace <项>` 或 `--keep <项>`；覆盖前旧目标会被复制为 `<名称>.pre-migration`。
- 复制先写 `<名称>.migrating`，校验 SHA256 后再原子替换；失败保留原文件，并在目标状态目录追加 `migration-journal.jsonl`，记录失败原因。迁移不重试、不重放、不删除旧目录。
- `actions/` 里的未决事务标为 `REVIEW_REQUIRED`：迁移只复制记录，任何重放都必须由使用者按具体事项重新授权。
- `downloads/` 与 `logs/` 标为 `EXCLUDED_USER_FILES`，保持原位，不复制也不改写。

## 4. 回退

1. 用上一稳定版本的源码包或 wheel 重新安装程序文件。
2. 旧状态目录在迁移中从未被删除，可直接继续使用；迁移产生的目标副本和 `.pre-migration` 备份由使用者自行决定保留。
3. 若新版本写入过状态，先确认旧运行时能读取该格式；不能确认时保留副本，不要用“删除用户目录”换取干净启动。
4. 回退不改变任何校园侧的预约、提交或付款记录。

## 5. 候选构建与核对

```sh
python scripts/package_release.py build
python scripts/package_release.py verify
python scripts/package_release.py install-check
```

`build` 从已提交的 Git 内容生成确定性源码归档、sdist 与 wheel，并写出 `release-manifest.json` 与 `SHA256SUMS`；`verify` 重新核对版本、校验和、包内资源、许可证与入口点；`install-check` 在仓库外的临时虚拟环境安装 wheel、解析包内资源并启动 29 个 MCP 工具。以上均为候选准备，不代表发布，也不代表真实校园业务可用。
