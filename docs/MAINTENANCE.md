# 更新、Release、维护与项目管理

## 维护责任

Codex 担任集成与发布维护者：整理需求和 Issue、审查和合并 PR、兼容修复、依赖更新、发布说明、版本标签、下载包校验、故障排查、回滚与能力状态维护。Hermes 在独立分支实现任务并提交可复现证据。使用者可以通过 Issue/PR 链接请求任一方接手。

这些是执行职责约定；不包含无人值守持续监控或自动唤醒。需要下一次维护时，由使用者发起任务。

## 日常更新

默认按[完整升级批次](BATCH_UPGRADES.md)组织：Hermes 可独立完成一整套版本开发及候选包准备，再由 Codex 整体审查和正式发布。小模块以提交和清单记录，不逐模块交 PR 或发行新版本。

建立 Issue，明确目标/范围/验收；检查当前分支与已有工作；以最小必要改动实现并补充有价值的测试；在 PR 写清问题、结果、验证及限制。新版本不得将模拟通过提升为真实写入通过。依赖更新保留版本、许可证、来源与修改说明。

## 发布步骤

1. 从干净且已审查的 main 发布，核对 CHANGELOG、版本号、能力矩阵和未解决事项。
2. 运行隐私扫描、完整离线测试、MCP smoke；在 Windows/Linux 验证。真实业务仅按该次具体授权进行。
3. 构建源码包、wheel、sdist，检查归档成员和包内源码，确认没有认证状态、本机路径或个人资料。
4. 创建 `vX.Y.Z` 标签与 GitHub Release，上传归档、构建包和 SHA256SUMS。预览版本用 prerelease 标记。
5. 从公开下载地址读取校验文件并核对上传资产，记录对应提交和测试结果。更新维护资料和交接状态。

## 版本回归门禁与验证报告

7.2 的回归门禁由 `scripts/regression_report.py` 生成机器可读结果，CI 在每个平台运行后上传安全摘要，发布门禁再汇总两个平台并检查候选包：

```text
python scripts/regression_report.py gate --junit artifacts/junit.xml \
  --dev-results .local/test-results.json --platform linux \
  --output artifacts/regression-report.json
python scripts/regression_report.py aggregate --reports artifacts/reports \
  --output artifacts/release-report.json
python scripts/regression_report.py release-gate --reports artifacts/reports \
  --dist dist --require-dist --output artifacts/release-gate.json
```

报告包含源码提交、契约版本、平台、用例范围、通过/失败/跳过、跳过原因、副作用计数，以及按模块归属的失败与最小复现命令。平台专属跳过必须写明原因；环境缺口记为 `INCOMPLETE`，不能当作该平台全部可用。报告不包含原始异常、Cookie、令牌、账号或本机路径。发布门禁同时扫描源码与候选归档、核对固定依赖和保守默认值，并验证候选包；任一隐私标记、未解释跳过、依赖漂移或候选校验失败都会阻断通过。通用凭据和敏感文件名由 `scripts/privacy_scan.py` 检查全部源码与归档；`--markers` 的精确标记集只扫描安装产品 wheel，源码包和 sdist 因按设计包含测试而由通用隐私扫描覆盖，这样既能验证“标记进入公共产品必须失败”，也不会把已提交的合成标记定义本身误报为泄露。原始失败材料留在受控环境，公开摘要只保留错误类别和合成复现。

## 故障与回退

先保留错误证据及未决事务，不自动重复发送模型生成或校园写入。按问题建立修复分支；需要回退时切换已验证标签，保留状态格式兼容性和新用户修改。公开报告不附私有日志。错误的发布资产用修订版替换并说明原因，不删除事故证据后声称未发生。

## GitHub 权限

云端使用独立配置的 GitHub 授权或专属于本仓库的写入部署密钥；不转存本机个人令牌。部署密钥支持 Git 推送，但不授予 Issue/PR 管理 API。若云端已有相应授权可使用其现有 GitHub CLI；否则推送分支后由维护者创建 PR。
