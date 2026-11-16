# 网络学堂

课程匹配不唯一时列出候选，不能自行选择相似课程。课程和作业只读查询可直接执行；公告需要逐页读列表并打开正文，列表标题不是全文。附件和课件仅按用户范围下载，不默认整学期全部下载。

会话保存在原 `%LOCALAPPDATA%\LearnAssistant\session.dpapi`，使用 Windows DPAPI。清华助手会识别文件变更并重建 provider，避免长期进程继续用旧 Cookie；一次业务认证失败仅触发一次统一认证，再读相同业务。

`get_assignment`、`download_files` 保留旧缓存与安全下载逻辑。`prepare_submission` 生成提交前摘要，不执行真实远端提交。第五块通用浏览器无站点写契约时返回 ADAPTER_REQUIRED；用户明确要求交作业也不能绕过保护，须补齐并验证对应适配器，或交还用户在官方页面完成最后提交。不能把本地摘要当作提交证据。

严格“只阅读、不下载、不改文件”任务使用当前已登录的浏览器完成阅读，不执行默认持久化登录、目录刷新、下载或本地导出；正常页面导航可能产生站点访问记录，应如实说明。

## 结构化读取

先 `describe_service("learn")` 获取参数约定。`query_service` 的 `parameters` 只接受登记字段，不接受 Cookie、票据、任意接口或请求头。

| operation | parameters | 结果范围 |
|---|---|---|
| semesters | `{}` | 有效学期 ID、当前学期标记 |
| courses | 可选 semester、query | 当前或指定学期课程 |
| notices | 可选 course_id，或 semester + course_name | 有效及历史公告，含正文、发布时间和课程标识 |
| notice_detail | 课程选择字段 + notice_id | 公告正文与附件条目分别报告读取状态 |
| assignments | 课程选择字段 | 未交、已交待批、已批改三个列表 |
| assignment_detail | 课程选择字段 + assignment_id | 作业说明、状态和附件条目 |
| files | 课程选择字段、可选 query | 分类文件与主列表合并；来源数量无法证实时标记不完整 |
| calendar | start_date、end_date，均 YYYY-MM-DD；可选 graduate | 通过网络学堂正常 SSO 读取本科/研究生教务日历 |

课程 ID 优先；不指定课程时列表查询当前学期各课程。名称必须精确且在所选学期唯一；NEEDS_USER_ACTION 时根据 candidates 让用户明确目标，不选近似项。详情必须定位一门课。`course_id` 定位时课程名称可能为空，必要时从 courses 合并，不能凭空补全。

分页按相同参数和 page_size 使用 next_cursor 续读。最终 `complete=true` 才表示所选来源读完；PARTIAL 的 source_errors、warnings 和 upstream_pages 说明缺页或失败，不能把失败解释为“没有公告/作业”。空集合仅在来源完整时标为 EMPTY。

公告 `body_state` 与 `attachments_state` 分开；作业使用 `instructions_state`。LIST_READ 只表示附件目录已解析，NOT_DOWNLOADED 不是下载完成；UNRESOLVED 表示页面提示有附件但未能解析。详情得到的 `attachment:...` file_id 可交给原 download_files，新进程会先重新核对原公告/作业关联，再下载。下载清单记录 source_kind/source_id、字节数及 SHA256；同名异内容不覆盖，已有同内容按哈希复用。只在用户要求后下载。

`submitted`、`graded`、`deadline`、`late_submission_deadline`、`overdue`、`feedback` 与 `graded_at` 保留各自含义。grade=null 表示平台未提供成绩，不能转换成 0；feedback=null 与平台返回空字符串也要区分。说明正文允许平台明确返回空字符串，此时 READ 不等于有文字内容。

日历按最多 28 天的窗口读取，查询跨度最多 366 天（起止日期差）。按来源事件 ID 去重，缺 ID 时不凭课程名合并；EVENT_ID_UNAVAILABLE_NO_DEDUP 是需告知的限制，完整窗口读取不证明来源本身没有重复。日期范围内的教务日历不是所有课程、考试、调课和预约的总表。本次真实验收为本科日历，研究生日历仅离线契约覆盖。

