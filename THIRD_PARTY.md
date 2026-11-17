# 第三方来源与许可

| 组件 | 固定来源 | 许可与修改 |
|---|---|---|
| madmodel-proxy 1.10.8 | https://github.com/noroadback/madmodel-proxy ，提交 `5a26c322535bca734547fd11951edf3adf7ccbd2` | MIT；代码原样置于 vendor/madmodel-proxy，原 LICENSE 与 THIRD-PARTY-NOTICES 保留；不包含其原 Git 历史或运行状态 |
| thu-learn-lib 4.2.6 读取片段 | https://github.com/Harry-Chen/thu-learn-lib ，提交 `3e8b5ba1382c2c87e9bdba431705dca82d6d07ee` | MIT；许可证、来源哈希、片段保存在 src/learn_assistant/vendor/thu-learn-lib |
| tough-cookie 6.0.0 及传递依赖 | src/thu_assistant/package-lock.json | 按锁文件安装；许可见 src/thu_assistant/THIRD_PARTY_NOTICES.txt |
| cryptography 50.0.2、Pillow 13.0.0、requests 2.33.0 | 运行时依赖（`pyproject.toml`、`requirements.txt`） | Apache-2.0 OR BSD-3-Clause、MIT-CMU、Apache-2.0；通过官方分发安装，不把本机安装目录打包 |
| Playwright 1.57.0 | 可选浏览器依赖（`browser` extra） | Apache-2.0；Chromium 的下载是独立步骤，不在安装钩子中执行 |
| pytest、build、jsonschema、wheel、setuptools | 开发工具（`requirements-dev.txt`） | MIT、MIT、MIT、MIT、MIT；不进入 wheel 的运行时依赖 |

Openthu 自有代码采用根目录 MIT 许可。上游原作者的版权与许可不因项目改名而移除。未明确授权的参考项目、个人状态、旧安装备份和聊天记忆未进入公共仓库。
