# 规范业务数据模型

机器可读清单：[contracts/v0.2/sources/data-models/manifest.json](../../contracts/v0.2/sources/data-models/manifest.json)。
所有模型都是 **Openthu 内部字段**，不是学校接口字段；上游字段到规范字段的映射记录在来源登记表中。

## 模型一览

| 模型 | 契约 | 主要用途 | 关键规则 |
|---|---|---|---|
| 事件 | `calendar-event-v1` | 教学日历、作业截止、预约、考试 | 复用 1.1 冻结契约；日期级不补午夜；结束不早于开始 |
| 课程 | `course-v1` | 课程身份、学期、教师、角色 | 同名课程不得按标题合并 |
| 资源 | `resource-v1` | 课程文件、馆藏、数据库、外链 | 资源标识不来自标题或相对路径 |
| 成绩 | `grade-v1` | 课程成绩、学分、发布状态 | 缺失成绩不为 0；未发布/扣留/未知分开 |
| 订单 | `order-v1` | 体育预约、图书馆座位/房间、借阅 | 审批、付款、退款分轴；未知不写成成功 |
| 附件 | `attachment-v1` | 来源附件与本地媒体引用 | 不含原始字节、绝对路径或认证下载 URL |
| 作业 | `assignment-v1` | 作业只读记录 | 提交状态是读取解释，不是本机提交回执 |
| 提交 | `submission-v1` | 上传、草稿、正式提交 | 上传成功、草稿保存、正式提交三态分离 |
| 续借 | `renewal-v1` | 续借资格、执行、到期日 | 资格提示不是成功；失败不改到期日 |
| 图片能力 | `model-image-capability-v1` | 模型图片输入结论 | 目录标记不等于实际传输能力 |

## 稳定标识

```text
course_uid     = <source_id>:COURSE:<semester_id>:<source_record_id>
resource_uid   = <source_id>:RESOURCE:<source_record_id>
grade_uid      = <source_id>:GRADE:<semester_id>:<source_record_id>
order_uid      = <source_id>:ORDER:<source_record_id>
attachment_uid = <source_id>:ATTACHMENT:<source_record_id>
assignment_uid = <source_id>:ASSIGNMENT:<source_record_id>
submission_uid = <source_id>:SUBMISSION:<assignment_or_record_id>:<version>
renewal_uid    = <source_id>:RENEWAL:<loan_uid>
event_uid      = 1.1 calendar-event-v1 的账号、服务、来源和 occurrence 组合
```

规则：

1. 标题、教师名、房间名、数组下标和显示顺序不参与稳定标识。
2. 缺少 `source_record_id` 或必要学期时，UID 为 `null`，`identity_state=MISSING_STABLE_ID`；不得用标题哈希、随机数或“第一条”补齐。
3. 同一标题、不同来源编号或不同学期是不同对象；`dedupe_state=SAME_TITLE_DIFFERENT_IDENTITY` 表示明确未合并。
4. 来源编号冲突时保留 `CONFLICT`，不挑选一个“看起来更对”的编号。
5. 账号域、服务域和用途必须一起参与稳定键；不得把一个账号的记录复用到另一个账号。

## 未知、空与部分

| 场景 | 正确表示 | 错误表示 |
|---|---|---|
| 未提供成绩 | `grade_value=null`，`state=UNGRADED/UNKNOWN` | `0`、及格、默认学分 |
| 未提供截止时间 | `deadline=null` | 猜测当天 23:59 |
| 来源失败 | 错误状态，`complete=false`，行数组为空但不叫空结果 | `EMPTY`、成功空列表 |
| 真实空结果 | `EMPTY` + `source_complete=true` + `complete=true` + `rows=[]` | 只有 `rows=[]` |
| 第一页空但还有下一页 | `PARTIAL`/`UNKNOWN`，保留 `missing_pages` | 直接返回空 |
| 目录命中 | 目录条目，能力标记 `DISCOVERED_NOT_BUSINESS_VERIFIED` | 业务权限或成功 |
| 可见单册/空闲座位 | 来源状态，仍标记资格未核验 | 可借/可预约 |
| 目录支持图片 | `catalog_support_image=true`，能力 `UNVERIFIED` | 图片输入 `AVAILABLE` |

## 字段映射方式

每个来源行包含 `field_mappings`，每项记录：

- `raw_field`：上游字段名或明确的行位置；
- `canonical_field`：规范字段；没有规范落点时写 `null`；
- `transform`：类型、时间、枚举和缺失值处理；
- `required`：是否参与稳定标识或核心语义；
- `disposition`：`MAPPED`、`DERIVED`、`RETAINED_SOURCE_ONLY`、`DROPPED`；
- `note`：为什么这样映射，以及不能推出什么。

`RETAINED_SOURCE_ONLY` 不是“以后再随手加字段”，而是明确承认当前规范模型没有对应位置。后续模块若要提升为规范字段，必须先修改契约、样例和拒绝样例。

`required: true` 表示规范记录必须满足该字段，不表示当前读取适配已经强制它。现有 `learn.courses`/`learn.assignments` 仍可能把缺失标题读成空字符串，`Course.role` 也可能是小写；3.2 必须在规范化层统一转换为契约值或失败关闭，不能把 registry 的 required 当成运行时保证。

## 事件模型

事件复用 1.1 的 `calendar-event-v1`，不新建重复 schema。日期级事件使用 `time.kind=DATE`，精确事件使用 `DATE_TIME`；二者不能互相伪装。教学日历的 `fl` 是来源分类字段，不得直接映射成 `status=CONFIRMED`。事件覆盖范围仍受来源限制，不能把教学日历说成完整课表或考试表。

## 来源范围

模型本身不证明来源完整。每个结果仍需携带来源证据：来源 ID、访问范围、读取页数、上游总数（未知为 `null`）、缺失页、警告、检查时间和证据等级。模型与来源证据必须一起解释。
