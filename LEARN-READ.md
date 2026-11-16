# 网络学堂读取适配（第 6 块）

2026-10-31。默认启用 `LearnReadProvider`，沿用原 HTTP transport、DPAPI 会话、统一认证恢复及下载流程。MCP 工具仍为 27 个，通过既有 query_service 扩展 learn 操作；旧工具参数及原字段保留。

## 来源和许可

读取映射与类型依据 [Harry-Chen/thu-learn-lib v4.2.6](https://github.com/Harry-Chen/thu-learn-lib/tree/3e8b5ba1382c2c87e9bdba431705dca82d6d07ee)，固定提交 `3e8b5ba1382c2c87e9bdba431705dca82d6d07ee`，MIT，Copyright (c) 2019–2022 Harry Chen。

完整许可证、原文件 SHA256、所参考行段与摘录哈希在 `src/learn_assistant/vendor/thu-learn-lib/`。摘录是非执行参考，实际实现为 Python 只读移植；不引入上游密码保存、上传、提交或第二套登录。MIT 许可随包保留。

本机修改：严格检查成功标志与字段；公告 HTML 转为惰性格式和纯文本；支持多个附件与实际作业嵌套下载路径；分页缺失、重复及部分失败显式报告；缺失成绩保留 null；日历 JSONP 仅解析 JSON、不执行脚本；适配学校目前返回的纯文本 SSO 票据，票据不进入输出。跨到教务域时不发送网络学堂 CSRF 头。

## 能力与证据

learn 操作包括 courses、semesters、notices、notice_detail、assignments、assignment_detail、files、calendar。参数以 describe_service 为准。课程 ID 优先，其次指定学期内唯一精确名称；歧义返回候选。列表不指定课程时查询当前学期，详情必须指定唯一课程。

公告读取有效及过期两类，作业读取未交、已交待批、已批改三类。分页超过界限、重复页、总数未知、部分课程失败时返回 PARTIAL；失败不能变成已验证的空集合。文件按分类读取并合并主列表；主列表触及 200 条且无法证明未分类完整时明确报告不完整。正文和附件状态独立。

附件 ID 绑定来源课程和公告/作业。新进程先重新验证来源再下载，不凭调用方拼接 URL。下载沿用 SHA256、清单和安全路径检查，记录来源关联；同名不同内容不覆盖，编号后的已有同内容文件也能复用。只读验收不执行真实下载，下载回归用合成文件。

日历查询起止日期差最多 366 天，每个窗口最多 28 个日期；只查询指定本科/研究生教务日历。来源提供事件 ID 时去重并检查冲突；缺失时保留原记录并警告，不生成伪 ID 或声称全校考试安排完整。

真实验收仅代表报告所列账号、课程、日期和服务。研究生日历、真实历史公告正文、含多附件公告、真实文件字节下载仍未现场验收，相应解析和下载行为有离线覆盖。`prepare_submission` 仍只做本地检查。

## 回退与验证

在插件源 MCP 配置 env 中设置 `THU_LEARN_READ_V2=0`，按本机插件安装流程重新加载并新建连接，切回原 WebProvider。新增读取显式报 EXPANDED_LEARN_READ_DISABLED；旧工具保留原能力和完整性限制。恢复扩展时移除该设置或设为 1。回退不删除会话、下载成果或第 5 块事务日志。

