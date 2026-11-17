# 任务交接：v0.2.0 候选交审

- 任务 / Issue：Issue #4（v0.2.0 完整升级）；模块 8.1 全版本集成、候选构建与交审
- 当前 PR / 分支：版本 PR #2（Draft）；模块分支 `hermes/8.1`，集成分支 `hermes/1-install-diagnostics`
- 当前负责者 → 下一位：Hermes → Codex
- 目标与验收标准：把批次 1–7 的成果集成为一个可安装、可恢复、可复核的 `0.2.0` 候选；候选包、集成清单、模块结果和两平台证据齐备；由 Codex 整体审查后合并、打标签与发布

## 复现步骤

```text
git fetch origin
git switch hermes/8.1
python scripts/dev.py setup-node
python scripts/dev.py check
python scripts/dev.py test
python scripts/package_release.py build
python scripts/package_release.py verify --expected-commit <候选提交>
python scripts/package_release.py install-check --dist dist
```

CI 在每个平台运行同一套离线检查，并在 `release gate (ubuntu-24.04)` 汇总两个平台的报告、扫描源码与候选归档。

## 预期与实际

预期：全部离线检查通过，候选 `verify` 与 `install-check` 通过，仓库外全新安装可运行看板与只读查询的失败关闭路径。
实际：见 `plans/v0.2.0/交付/候选版本记录.md` 与 `plans/v0.2.0/交付/模块结果/8.1.json` 的实测记录。

## 已尝试的方法及结果

- 逐项核对批次 1–7 的模块交付提交与批次集成提交：均为候选提交祖先。
- 修订 `local-read-api-v1` 的请求侧 `cursor`，删除 6.2 的 `QUERY_EXTENSION`；运行时路由与冻结参数逐项相等。
- 更正 `actions.py` 旧 docstring 与 `source-matrix.md` 的历史归属行。
- 构建 4 个候选产物，`verify` 绑定提交，`install-check` 在仓库外安装；另建全新 venv 复核看板外壳、无会话 401、缺资源与缺密钥行为。
- 隐私扫描源码与全部候选归档，0 发现。

## 需要解决的具体问题

没有阻塞交审的缺陷。仍需 Codex 独立复核，并在代码审查发现问题时按同一分支交回 Hermes。

## 已运行的检查与结果

见 `plans/v0.2.0/交付/模块结果/8.1.json` 的 `checks` 与 `plans/v0.2.0/交付/批次门禁.json` 的批次 8 条目；运行号绑定到具体提交。

## 剩余工作与继续入口

1. Codex 在版本 PR #2 上整体审查实现、风险、证据与公开内容。
2. 需要修改时在同一分支提交修复，重新构建候选并重跑受影响检查；旧资产不得冒充新候选。
3. 审查通过后由 Codex 合并 main、创建 `v0.2.0` 标签、发布 Release 并核对公开下载。
4. 本阶段不合并 main、不打标签、不发布；`accepted` 由审查阶段填写。

公开记录不含账号、个人业务记录、凭据、主机/IP 或私人路径。
