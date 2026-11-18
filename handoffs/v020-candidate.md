# 任务交接：v0.2.0 候选交审

- 任务 / Issue：Issue #4（v0.2.0 完整升级）；模块 8.1 全版本集成、候选构建与交审
- 当前 PR / 分支：版本 PR #2（Draft；head `hermes/1-install-diagnostics`，代码/CI head `4a03ecd`）；模块分支 `hermes/8.1`
- 当前负责者 → 下一位：Hermes → Codex
- 目标与验收标准：把批次 1–7 的成果集成为一个可安装、可恢复、可复核的 `0.2.0` 候选；候选包、集成清单、模块结果和两平台证据齐备；由 Codex 整体审查后合并、打标签与发布

## 复现步骤

```text
git fetch origin
git switch hermes/1-install-diagnostics   # PR #2 head
git log -1 --format=%H                       # 应为 4a03ecd 或其后的记录性提交
python scripts/dev.py setup-node
python scripts/dev.py check
python scripts/dev.py test
python scripts/package_release.py build
python scripts/package_release.py verify --expected-commit <最终 head>
python scripts/package_release.py install-check --dist dist
```

CI 在每个平台运行同一套离线检查，并在 `release gate (ubuntu-24.04)` 汇总两个平台的报告、扫描源码与候选归档。

## 预期与实际

预期：全部离线检查通过，候选 `verify` 与 `install-check` 通过，仓库外全新安装可运行看板与只读查询的失败关闭路径。
实际：`4a03ecd` 的 push `44236818999` 与 pull_request `44236825532` 四作业全绿；Linux 1905 通过/8 跳过、Windows 1892 通过/21 跳过（均含 Node 66）；`release gate` 九项 true、0 gap；候选 4 个产物由门禁在同一提交重建并通过 `verify`。详细数字见 `plans/v0.2.0/交付/候选版本记录.md` 与 `模块结果/8.1.json`。

## 已尝试的方法及结果

- 逐项核对批次 1–7 的模块交付提交与批次集成提交：均为候选提交祖先。
- 修订 `local-read-api-v1` 的请求侧 `cursor`，删除 6.2 的 `QUERY_EXTENSION`；运行时路由与冻结参数逐项相等。
- 更正 `actions.py` 旧 docstring 与 `source-matrix.md` 的历史归属行。
- 构建 4 个候选产物，`verify` 绑定提交，`install-check` 在仓库外安装；另建全新 venv 复核看板外壳、无会话 401、缺资源与缺密钥行为。
- `8.1-audit` 的 P1×3 + P2×9 全部闭环；`/todos`、`/calendar` 拒绝非 `lac1` 游标；consent-peek 并发竞争由 `2bd42cc` 修复。
- 总审计（`de1d2fc` 快照）P0=0、P1=4、P2=11、P3=2，17/17 闭环；修复链 `88c48e1` … `6e22dbb`，产品代码在 `43f19ef`。
- 总审计后的 CI 在 `6e22dbb` 发现一个 Windows-only 测试假设问题：`test_state_root_honours_the_configuration_file` 把默认配置路径写死为 Linux 的 `$HOME/.config/openthu/config.json`，Windows 实际读 `%APPDATA%\Openthu\config.json`。已改为使用 `OPENTHU_CONFIG_FILE` 指向临时配置（`4a03ecd`，只改测试），双平台复跑全绿。

## 需要解决的具体问题

总审计 17 条与 8.1 审计 12 条均已闭环，未发现阻塞交审的产品缺陷。需要 Codex 独立复核：
1. 总审计修复的 15 个提交，特别是 P1-1 状态目录、P1-2 ICS SEQUENCE、P1-3 写开关错误码、P1-4 旧预览路径/权限；本轮没有独立第二审查者。
2. 门禁与台账仍有一处分层滞后：批次 8 的 `integration_commit`/`8.1` 的 `delivery_commit` 仍记 `2bd42cc`，未回写总审计修复后的 `4a03ecd`；`版本总清单`、`进度清单`、`集成清单`、`批次门禁记录` 还停在审计前。合并前应按 §8.3 的清单做记录性同步。
3. 合并前必须在最终 PR head 重新构建候选并登记 SHA256，不能复用 `43f19ef`/`4a03ecd`/`2bd42cc` 的旧资产。

## 已运行的检查与结果

| 检查 | 结果 |
|---|---|
| 方案校验 | `ok=true`；24 模块 / 9 批次 |
| 契约校验 | `ok=true`；8 契约 / 1 兼容基线 / 370 错误码 / 29 legacy 工具 |
| 来源校验 | `ok=true`；29 sources / 10 models / 8 tasks / 14 readiness / 7 fixtures |
| `dev.py check` | `ok=true`；571 文件、0 隐私发现、29 工具 |
| Linux 全量离线 | Python 1839 通过 / 8 跳过 / 0 失败；Node 66 通过 |
| Windows 全量离线（CI） | Python 1826 通过 / 21 跳过 / 0 失败；Node 66 通过 |
| `release gate` | PASS；九项 true、0 gap |
| 候选构建 | 4 个产物；源码 568、wheel 214（54 随包）、sdist 499 |
| 恢复安装 | 仓库外 `install-check` 通过；全新 venv 看板可用、无会话 401、缺资源/缺密钥失败关闭 |
| 真实调用 | 校园 / 模型 / 浏览器 / 付款 / 提交 / 消息发送均为 0 |

## 剩余工作与继续入口

1. Codex 在版本 PR #2 上整体审查实现、风险、证据与公开内容。
2. 需要修改时在同一分支提交修复，重新构建候选并重跑受影响检查；旧资产不得冒充新候选。
3. 按 `docs/HANDOVER.md` §8.3 同步门禁、模块结果、版本清单、进度清单、集成清单与批次门禁记录。
4. 审查通过后由 Codex 合并 main、创建 `v0.2.0` 标签、发布 Release 并核对公开下载。
5. 本阶段不合并 main、不打标签、不发布；23 个 Hermes 模块 accepted，批次 8 accepted、批次 9 ready。

公开记录不含账号、个人业务记录、凭据、主机/IP 或私人路径。
