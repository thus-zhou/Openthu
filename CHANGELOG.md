# 更新记录

## 0.2.0 — 候选（未发布）

- 完整发行布局：`build_support/distribution.json` 是唯一资源清单，wheel 与源码包由它生成；`setup.py` 在构建时把 Windows 认证辅助、固定上游 `madmodel-proxy`（含 tokenizer 与许可证）复制进 `thu_assistant/_bundled/`，并写出版本、提交与逻辑资源名的 `layout.json`。
- `thu_assistant.paths` 按安装布局描述解析认证资源：安装包不再依赖 `src/` 的父目录层级，源码检出仍优先使用工作区文件；布局中的绝对路径或越界路径一律拒绝。
- 版本只有一个来源 `thu_assistant.version.__version__`（0.2.0），Python 包元数据、`learn_assistant`、MCP `serverInfo`、发布清单和候选说明同源；最低 Python 提升到 3.12，Node 仍为 24。
- 依赖分层：运行时固定 `cryptography`、`Pillow`、`requests`；浏览器客户端为可选 `browser` extra；`pytest`/`build`/`jsonschema` 只在开发依赖中。新增 `requirements.txt` 与许可证清单。
- `python scripts/package_release.py build|verify|install-check|manifest`：从 Git 对象生成确定性源码归档、sdist 与 wheel，规范时间、权限、顺序与属主；`verify` 核对版本、校验和、包内资源、许可证与入口点，`install-check` 在仓库外的临时虚拟环境安装 wheel 并启动 29 个 MCP 工具。
- 新增 `thu_assistant.upgrade` 与 `python scripts/migrate_state.py preview|apply`：只读预览旧状态格式、目标位置、兼容性与是否需重新认证；Windows DPAPI 密文不导往其他平台，目标冲突需显式 `replace`/`keep`，复制经临时文件与哈希校验，失败保留原文件与日志，下载与日志保持原位。
- `doctor` 增加 `capabilities` 汇总与 `release-layout` 检查：缺少 Node 或 Chromium 时指出具体受影响能力；程序版本与包内资源布局不一致时阻止启动。

- 2.2 审计修复：记录 ID 绑定进 AAD，阻止记录文件互换；轮换校验新密钥位置并禁止把 key 放进数据目录；新增 `THU_SECURE_RETIRED_KEY_FILES`，支持重启后回退并在切换前核验旧记录；`health()` 使用真实解密认证并捕获不可读记录，`preview_rotation()` 不再泄露绝对路径；首次 pointer 失败可回收空目录，记录读取有大小上限。
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
