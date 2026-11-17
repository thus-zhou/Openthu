# Codex 插件配置

此目录包含 Openthu 插件元数据、主技能和兼容别名。随源码分发的 .mcp.json 是占位模板，不能直接连接。插件版本与产品版本同为 0.2.0（候选未发布）。

先执行 `python scripts/configure_mcp.py --output .local/mcp.json`，将生成的机器专用配置注册到客户端。若需要完整插件目录，用 `python scripts/configure_mcp.py --plugin-output .local/openthu-plugin` 生成独立本地副本，再从该目录安装。输出不进入 Git。不要直接修改或提交模板中的私人路径。

工具名称、能力状态和使用顺序见仓库 `docs/TOOLS.md`、`docs/CAPABILITIES.md` 与 `docs/USER_GUIDE.md`；插件技能只引用这些稳定文档，不复制个人配置或验证结论。
