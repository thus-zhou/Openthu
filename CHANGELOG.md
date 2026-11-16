# 更新记录

## 未发布

- 2.1 审计修复：锁文件版本严格脱敏，避免公开诊断泄露路径或崩溃；`runtime.python`/`runtime.node`/超时/输出上限接入 `doctor`、`check`、`smoke`、`test`；非法 URL、不可写父目录、缺失包资源和命令探测失败改为稳定诊断；清理测试环境中的 `OPENTHU_*` 用户配置。
- 2.1 交付补齐：新增模块结果 JSON/MD，更新验收映射、模块负责表和版本总清单；2.1 仍待与 2.2/2.3 在同一提交回接后形成批次验收。
- 新增统一配置层 `thu_assistant.configuration`：命令行、`OPENTHU_*` 环境变量、本机 JSON 和默认值按固定优先级合并；未知/重复键、非法类型、不可读文件和秘密字段返回稳定错误，公开摘要不含路径或秘密。
- 新增统一路径解析 `thu_assistant.paths`：资源、状态、下载、临时和日志使用同一套描述；源码与包安装共享逻辑资源标识，缺少资源直接报告而不搜索旧安装；状态目录默认在代码库之外，拒绝覆盖程序资源、相对路径和密钥目录。
- `doctor` 改为 `development`、`readonly`、`browser`、`production-auth`、`packaging`、`config` 分类，状态为通过/警告/失败/不适用；缺少 Chromium 只影响浏览器能力，Linux 生产认证为不适用，Python 最低版本读取项目声明。
- 诊断中的命令调用固定参数、超时和输出上限，版本字符串必须匹配格式，原始命令输出不进入报告。
- 新增 `configs/openthu.example.json`、配置/路径合成测试，README 与安装文档补充来源优先级、秘密边界和迁移预览。
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
