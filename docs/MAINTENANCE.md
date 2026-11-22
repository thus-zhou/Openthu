# 更新、Release、维护与项目管理

## 维护责任

Codex 担任集成与发布维护者：整理需求和 Issue、审查和合并 PR、兼容修复、依赖更新、发布说明、版本标签、下载包校验、故障排查、回滚与能力状态维护。Hermes 在独立分支实现任务并提交可复现证据。使用者可以通过 Issue/PR 链接请求任一方接手。

这些是执行职责约定；不包含无人值守持续监控或自动唤醒。需要下一次维护时，由使用者发起任务。

## 日常更新

默认按[完整升级批次](BATCH_UPGRADES.md)组织：Hermes 可独立完成一整套版本开发及候选包准备，再由 Codex 整体审查和正式发布。小模块以提交和清单记录，不逐模块交 PR 或发行新版本。

建立 Issue，明确目标/范围/验收；检查当前分支与已有工作；以最小必要改动实现并补充有价值的测试；在 PR 写清问题、结果、验证及限制。新版本不得将模拟通过提升为真实写入通过。依赖更新保留版本、许可证、来源与修改说明。

## 发布步骤

1. 从干净且已审查的 main 发布，核对 CHANGELOG、版本号、能力矩阵、用户手册、工具对照和未解决事项；候选说明必须写明“未发布”，不能出现正式发布用语。
2. 运行隐私扫描、完整离线测试、MCP smoke、文档一致性测试；在 Windows/Linux 验证。真实业务仅按该次具体授权进行。
3. 构建源码包、wheel、sdist，检查归档成员和包内源码，确认没有认证状态、本机路径或个人资料；运行 `package_release.py verify`，并对来源和散列已核对的原始依赖 wheel 执行 `install-check --wheelhouse <仓库外依赖目录>`。
4. 创建 `vX.Y.Z` 标签与 GitHub Release，上传归档、构建包和 SHA256SUMS。预览版本用 prerelease 标记。
5. 从公开下载地址读取校验文件并核对上传资产，记录对应提交和测试结果。更新维护资料和交接状态。
6. 发布后核对 README、`docs/CAPABILITIES.md`、`docs/TOOLS.md` 和 `docs/UPGRADE.md` 的链接与版本状态；发现不一致时用补充说明澄清，不改写历史记录。

## 版本回归门禁与验证报告

7.2 的回归门禁由 `scripts/regression_report.py` 生成机器可读结果，CI 在每个平台运行后上传安全摘要，发布门禁再汇总两个平台并检查候选包：

```text
python scripts/regression_report.py gate --junit artifacts/junit.xml \
  --dev-results .local/test-results.json --platform linux \
  --commit "$(git rev-parse HEAD)" --output artifacts/regression-report.json
python scripts/regression_report.py aggregate --reports artifacts/reports \
  --expected-commit "$(git rev-parse HEAD)" \
  --output artifacts/release-report.json
python scripts/regression_report.py release-gate --reports artifacts/reports \
  --dist dist --markers tests/security_regression/marker_set.json \
  --expected-commit "$(git rev-parse HEAD)" --require-python-matrix \
  --require-dist --output artifacts/release-gate.json
```

报告包含源码提交、契约版本、平台、用例范围、通过/失败/跳过、跳过原因、副作用计数，以及按模块归属的失败与最小复现命令。平台专属跳过必须写明原因；跳过原因使用登记表或 `platform:`/`env:` 前缀，未登记的原因按 `unexplained` 阻断。环境缺口记为 `INCOMPLETE`，不能当作该平台全部可用。报告不包含原始异常、Cookie、令牌、账号或本机路径。

聚合会核对报告中的 `platform.name` 与运行宿主 `platform.system`、CI 的 `runner.os`，并要求报告提交、产品版本和契约集与候选提交一致；形状报告、脏工作区、缺少提交或契约的报告都不能通过。证据下限在 `tests/release_regression/evidence_floor.json`：空套件、单条用例、全跳过、缺少 Node 结果或 `--dev-results`、以及未覆盖 `coverage_matrix` 中 `covered` 模块测试文件的报告都不会 PASS。

发布门禁同时扫描源码与全部候选归档、核对固定依赖和保守默认值，并验证候选包；任一隐私标记、未解释跳过、依赖漂移或候选校验失败都会阻断通过。`--markers` 是发布门禁的必填参数，缺失、不可读或为空时 fail-closed，不会静默关闭检查。通用凭据和敏感文件名由 `scripts/privacy_scan.py` 检查全部源码与归档；精确标记集扫描全部归档，只按归档内路径跳过 `tests/security_regression/marker_set.json` 这一个已提交的标记定义文件，因此源码包和 sdist 里的真实本机路径/标记仍会被发现。候选包只在 Ubuntu 上构建（`py3-none-any` wheel 与 sdist），Windows 与 Linux 的测试证据由各自平台作业产生。原始失败材料留在受控环境，公开摘要只保留错误类别和合成复现。

## 故障与回退

先保留错误证据及未决事务，不自动重复发送模型生成或校园写入。按问题建立修复分支；需要回退时切换已验证标签，保留状态格式兼容性和新用户修改。公开报告不附私有日志。错误的发布资产用修订版替换并说明原因，不删除事故证据后声称未发生。

## GitHub 权限

云端使用独立配置的 GitHub 授权或专属于本仓库的写入部署密钥；不转存本机个人令牌。部署密钥支持 Git 推送，但不授予 Issue/PR 管理 API。若云端已有相应授权可使用其现有 GitHub CLI；否则推送分支后由维护者创建 PR。
