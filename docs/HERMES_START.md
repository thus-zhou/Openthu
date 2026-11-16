# Hermes 接手入口

仓库：https://github.com/thus-zhou/Openthu 。项目是 Openthu，使用人工接力；Codex 负责集成、更新、Release、维护和仓库管理。

## 开始前

1. 在云端项目副本检查 `git status`、当前分支和远端，不覆盖已有改动。目录已存在时先获取最新代码；首次使用则克隆仓库。
2. 阅读 `AGENTS.md`、`docs/HANDOVER.md`、`docs/CAPABILITIES.md`、`docs/MANUAL_RELAY.md` 和 `docs/ROADMAP.md`。
3. 激活当前环境的 Python 虚拟环境与 Node.js 24。若维护者已在该机器准备 `.local/activate.sh`，可在终端执行 `source .local/activate.sh`；否则按 README 安装。
4. 运行 `python scripts/dev.py check` 和必要测试。云端开发不需要本机学校密码、Cookie、个人记录或模型登录凭据。

## 工作方式

按使用者给定的任务建立 Issue 和 `hermes/<issue>-<slug>` 分支，完成实现、测试和 PR。仅有计划时先明确范围；不要直接扩大为新的校园业务或重构全部认证。

遇到重复失败、Windows 专用问题、需要本机校园实测或当前环境无法取得的证据时，保存代码、填写交接模板并生成提交检查点。PR 写清复现、预期、实际、尝试过程和需要 Codex 解决的问题，停止修改本任务，将 PR 链接交给使用者。

Codex 修复并交还后，先获取最新提交，读取交接和验证结果，再继续。任何真实创建、取消、提交、撤回和付款都需要针对该笔事项的用户明确授权。公共 GitHub 只保存程序、合成样例和脱敏的开发记录。

## 权限与发布

Git 推送使用当前机器单独配置的仓库授权。若只有部署密钥，推送分支后提供 compare/PR 链接给维护者；若已有 GitHub CLI 账户授权，可直接创建 PR。不要向公共记录展示授权值或本机路径。正式合并、版本标签和 Release 按 `docs/MAINTENANCE.md` 由 Codex 处理。
