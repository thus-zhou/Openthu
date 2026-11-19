# 任务交接：v0.2.0 候选交审

- 任务 / Issue：Issue #4（v0.2.0 完整升级）；模块 8.1 全版本集成、候选构建与交审
- 当前 PR / 分支：版本 PR #2（Draft；head `hermes/1-install-diagnostics`，代码/CI head `50ce675`）；模块分支 `hermes/8.1`
- 当前负责者 → 下一位：Hermes → Codex
- 目标与验收标准：把批次 1–8 的成果、总审计修复与第 9 批前优化集成为一个可安装、可恢复、可复核、台账完全一致的 `0.2.0` 候选；由 Codex 整体审查后合并、打标签与发布

## 复现步骤

```text
git fetch origin
git switch hermes/1-install-diagnostics   # PR #2 head
git log -1 --format=%H                       # 应为 50ce675 或其后的台账/交接记录提交
python scripts/dev.py setup-node
python scripts/dev.py check
python scripts/dev.py test
python scripts/package_release.py build
python scripts/package_release.py verify --expected-commit <最终 head>
python scripts/package_release.py install-check --dist dist
```

CI 在每个平台运行同一套离线检查，并在 `release gate (ubuntu-24.04)` 汇总两个平台的报告、扫描源码与候选归档。

## 预期与实际

预期：全部离线检查通过，候选 `verify` 与 `install-check` 通过，仓库外全新安装可运行看板与只读查询的失败关闭路径，全仓测试零 `unmapped`，全部台账与最终候选一致。
实际：`50ce675` 的 push `44434121491` 与 pull_request `44434129313` 四作业全绿；Linux 3.12/3.13 均 1908 通过/8 跳过、Windows 1895 通过/21 跳过（均含 Node 66，0 `unmapped`）；`release gate` 九项 true、0 gap；候选 4 个产物由门禁在同一提交重建并通过 `verify`，本机 Windows 同提交 `verify` 与仓库外 `install-check` 均通过。详细数字见 `plans/v0.2.0/交付/候选版本记录.md` 与 `模块结果/8.1.json`。

## 已尝试的方法及结果

- 逐项核对批次 1–7 的模块交付提交与批次集成提交：均为候选提交祖先。
- 修订 `local-read-api-v1` 的请求侧 `cursor`，删除 6.2 的 `QUERY_EXTENSION`；运行时路由与冻结参数逐项相等。
- 更正 `actions.py` 旧 docstring 与 `source-matrix.md` 的历史归属行。
- `8.1-audit` 的 P1×3 + P2×9 全部闭环；`/todos`、`/calendar` 拒绝非 `lac1` 游标；consent-peek 并发竞争由 `2bd42cc` 修复。
- 总审计（`de1d2fc` 快照）P0=0、P1=4、P2=11、P3=2，17/17 闭环；修复链 `88c48e1` … `4a03ecd`。
- 第 9 批前收尾优化（`50ce675`）：补齐 3 份总审计测试的 `regression_report.py` 模块归因并加全仓零 `unmapped` 守卫；`learn_assistant` 默认状态/下载根与非 Windows 会话目录接入统一配置/状态目录，并将 `prepare_submission` 预览改为 `O_CREAT|O_EXCL` `0o600` 原子创建；`BusinessFacade` 实时感知 `os.environ` 变化、改用完整 `service_ids` 缓存键并向 `queries.describe`/`actions.describe_actions`/`library_contracts` 贯通注入 `env` 开关。
- 全部滞后台账（`docs/HANDOVER.md` §8.3 所列全部 10+ 份文件及 `模块负责表.md`、`批次门禁.json` 顶层 `gate_note`）已统一同步到 `50ce675`。

## 需要解决的具体问题

第 9 批前的全部开发、总审计修复（17/17）、代码优化（`50ce675`）和台账同步均已闭环，无遗留产品缺陷或台账滞后。需要 Codex 在第 9 批执行：
1. 独立审查 PR #2 的整体改动，重点核对总审计修复（`88c48e1..4a03ecd`）与第 9 批前收尾优化（`50ce675`）。
2. 在最终 PR head 重新构建候选（`package_release.py build` / `verify` / `install-check`）并登记最终发布 SHA256，不复用旧提交的资产。
3. 审查通过后按 `docs/MAINTENANCE.md` 合并 main、创建 `v0.2.0` 标签、发布 GitHub Release 并核对公开下载。

## 已运行的检查与结果

| 检查 | 结果 |
|---|---|
| 方案校验 | `ok=true`；24 模块 / 9 批次 |
| 契约校验 | `ok=true`；8 契约 / 1 兼容基线 / 370 错误码 / 29 legacy 工具 |
| 来源校验 | `ok=true`；29 sources / 10 models / 8 tasks / 14 readiness / 7 fixtures |
| `dev.py check` | `ok=true`；571 文件、0 隐私发现、29 工具 |
| Linux 全量离线（CI 3.12 / 3.13） | Python 1842 通过 / 8 跳过 / 0 失败；Node 66 通过；0 `unmapped` |
| Windows 全量离线（CI 3.12） | Python 1829 通过 / 21 跳过 / 0 失败；Node 66 通过；0 `unmapped` |
| `release gate` | PASS；九项 true、0 gap（源码 571 文件、归档 1,849 成员均 0 发现） |
| 候选构建 | 4 个产物；源码 568、wheel 214（54 随包）、sdist 499 |
| 恢复安装 | 仓库外 `install-check` 通过（29 工具、`doctor_ok=true`、`user_state_unchanged=true`） |
| 真实调用 | 校园 / 模型 / 浏览器 / 付款 / 提交 / 消息发送均为 0 |

## 剩余工作与继续入口

1. Codex 在版本 PR #2 上整体审查实现、风险、证据与公开内容。
2. 需要修改时在同一分支提交修复，重新构建候选并重跑受影响检查；旧资产不得冒充新候选。
3. 审查通过后由 Codex 合并 main、创建 `v0.2.0` 标签、发布 Release 并核对公开下载。
4. 本阶段不合并 main、不打标签、不发布；23 个 Hermes 模块 accepted，批次 8 accepted、批次 9 ready。

公开记录不含账号、个人业务记录、凭据、主机/IP 或私人路径。
