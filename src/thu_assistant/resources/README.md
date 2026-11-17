# 课程资料中心与跨课程检索（模块 4.2）

本目录是 v0.2 模块 4.2 的全部实现。公共 MCP/CLI 入口留给 6.1；本模块不修改
`queries.py`、`todos.py`、`mcp_stdio.py`，也不新增第二个下载实现。

## 边界

- 只接收三类来源：课程文件（`COURSE_FILE`）、公告附件（`NOTICE_ATTACHMENT`）、
  作业附件（`ASSIGNMENT_ATTACHMENT`）。资源标识沿用冻结样例
  `stable-identity-samples-v1`：`learn:FILE:<源编号>`、`learn:NOTICE_ATTACHMENT:<源编号>`、
  `learn:ASSIGNMENT_ATTACHMENT:<源编号>`。标题从不参与身份判定。
- 索引构建只读元数据；公告/作业附件列表需要显式 `include_notice_details` /
  `include_assignment_details`，且只调用既有读取方法。索引阶段从不调用
  `download_file`。
- 持久化只走 2.2 的加密存储（用途 `INDEX`，账号域 `CAMPUS`，服务域 `LEARN`）。
  按账号与学期分记录；内存中解密后搜索，不建立明文全文数据库。
- 下载通过 `LearnDownloader` 调用既有 `provider.download_file`，没有新的网络路径。
  预览不写文件；执行需要 `confirm=True`，计划在进程内有效十分钟并一次性使用。
- 归档目录由使用者显式选择。文件名 = 安全课程目录 + 安全标题 + `(uid, version)`
  的短令牌；远端标题不能生成上级目录、绝对路径或 Windows 保留设备名。
- 已存在的文件永不覆盖：内容哈希相同则复用，不同则报告冲突并保留原文件。
- 导出只含选择的字段；包含正文摘录时要求 `acknowledge_personal=True`，且拒绝写入
  Git 工作树。
- 本包不导入 socket/urllib/requests/http，不打印、不写普通日志、不把绝对下载路径
  写进索引、导出或错误。

## 主要入口

```python
from thu_assistant.resources import ResourceCenter, ResourceLimits

centre = ResourceCenter(secure_store, "synthetic-account")
centre.collect(provider, semester="2026-2027-1", include_notice_details=True)
centre.search(semester="2026-2027-1", query="实验", source="NOTICE_ATTACHMENT")
plan = centre.preview_download(
    semester="2026-2027-1",
    selections=["learn:FILE:1001"],
    destination="/absolute/user/choice",
)
centre.execute_download(plan["plan_id"], confirm=True)
centre.export_summary("2026-2027-1", destination="/absolute/user/export.json")
centre.delete_cache("2026-2027-1")
```

`collect` 只是适配器 + 索引；`search`/`manifest`/`coverage` 只读；`preview_download`
只读；`execute_download` 是唯一写文件的入口。

## 版本规则

- 可靠来源更新时间（带时刻、带时区）作为版本；只有日期或缺失时为 `UNKNOWN`。
- 同一来源编号出现未知版本时，索引分配 `u1`、`u2`… 并标记
  `UNKNOWN_SEQUENCE`；已有已知版本不会被未知值覆盖。
- 下载后内容哈希与已记录值不同（同一个来源版本下内容变化）时，生成
  `c:<hash12>` 新版本，旧文件保留，旧条目标记 `SUPERSEDED`。
- 相同内容来自不同来源只做分组报告，不合并条目、不复用另一来源的文件路径。

## 错误

所有错误带 `code`/`category`/`retryable`/`user_action`，遵循
`error-classification-v1` 的类别策略。`RESOURCE_*` 码在 6.1 登记到
`contracts/v0.2/error-classification.json` 之前只在模块内使用；错误详情只允许
短枚举词，路径样式的内容被替换为 `DETAIL_REDACTED`。

## 已知限制

- 不解析 PDF、图片、Office 文档或录音内容；全文检索只覆盖已经读取的公告正文与
  作业说明，结果中的 `body_coverage` 会说明覆盖是否完整。
- 索引是进程内解密后的有界文档（默认 4 MiB / 每学期 2000 条），不是增量全文
  搜索引擎。
- 公告只有标题、没有附件时不会形成资源条目；其正文无法单独检索。
- 计划只在当前进程有效；重启或换进程后必须重新预览。
- 归档写入假定单写者（同一目录由一个进程使用）；并发写者只会遇到冲突，不会覆盖。
- 导出是 JSON 摘要，不是课程资料副本；原文件仍在用户选择的下载目录。

## 回退

停止调用 `ResourceCenter` 即可；已下载文件与用户目录不受影响。删除加密索引使用
`delete_cache(semester)`，它用空索引原子替换旧记录，不删除归档目录，也不把索引
转存为明文。原始课程文件列表与下载入口仍由既有 Learn 接口提供。
