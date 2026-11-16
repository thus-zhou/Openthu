# 更新记录

## 未发布

- 新增 `python scripts/dev.py doctor`：区分 Python、开发包、Node/npm、锁文件运行依赖、Playwright Chromium、Windows 认证条件和源码包完整性，缺失项给出可执行修复命令。
- `doctor` 默认输出只含仓库相对名称、版本、状态和修复命令，不含绝对路径、主机名、账号或凭据；`--json` 提供机器可读结果。
- `python scripts/dev.py check` 先运行同一诊断，失败时直接给出修复提示。
- README 与安装文档补充完整源码包、Python wheel 和云端离线开发的边界。

## 0.1.0 — 2026-11-16

- 首次以 Openthu 发布独立公共源码，提供 MCP、命令行、校园读取和事务适配代码。
- 将原有分散依赖整理为仓库布局，移除私人环境路径，保留兼容命令与工具 schema。
- 提供 Windows/Linux 离线开发入口、合成测试、配置生成器、隐私检查和可校验发布包。
- 明确 Hermes/Codex 人工接力、维护管理、更新与 Release 责任。
- 实时认证仍依赖 Windows；图书馆/体育真实写入和亲友专用办理的限制见能力矩阵。
