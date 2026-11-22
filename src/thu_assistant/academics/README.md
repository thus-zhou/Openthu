# academics：个人课表、考试与课程成绩只读适配（4.1）

本包是 v0.2 的学业记录边界。它负责把来源行规范化为 `course-v1`、`grade-v1`
和模块内的课表/考试记录，按显式校历展开周次，并把考试与课表转换成
`calendar-event-v1` 供日程模块（5.4）消费。

## 启用状态

| 能力 | 来源 | 状态 | 原因 |
|---|---|---|---|
| timetable | `academics.timetable` | `BLOCKED_SOURCE_EVIDENCE` | 没有已核对的个人课表入口、周次/教室字段和完整范围 |
| exams | `academics.exams` | `BLOCKED_SOURCE_EVIDENCE` | 没有已核对的考试入口、场次标识和覆盖证明 |
| grades | `academics.grades` | `BLOCKED_SOURCE_EVIDENCE` | 没有已核对的课程成绩入口与发布/扣留语义 |

状态与 `contracts/v0.2/sources/adapter-readiness.json` 的 4.1 行一致，测试会
逐项比对。`AcademicsReader.read()` 对三者都返回明确的阻塞结果：

```python
{
  "status": "UNKNOWN",
  "implementation_state": "BLOCKED",
  "runtime_availability": "UNAVAILABLE",
  "evidence_level": "NONE",
  "items": [],
  "error": {"code": "ADAPTER_REQUIRED", "category": "NOT_IMPLEMENTED", ...},
  "warnings": ["SOURCE_CONTRACT_BLOCKED"],
}
```

它不是 `EMPTY`，不返回模拟成绩、考试或课表，也不提供任何 URL、Cookie、请求头
或上游路径参数。教学日历（`learn.calendar`）不会被当成课表或考试表。

## 工程演练入口

`AcademicsReader.read_synthetic(capability, transport, ...)` 只接受声明
`synthetic=True` 的传输，并要求 `purpose="SYNTHETIC_FIXTURE"`。结果同时标注
`evidence_level="SYNTHETIC_TRANSPORT"`、`implementation_state="BLOCKED"` 和
`rehearsal.production_blocked=true`，因此不能当作生产读取证据。非合成传输在
任何情况下都会被 `ACADEMICS_PRODUCTION_TRANSPORT_BLOCKED` 拒绝。

## 模型与规则

- 学期：使用来源学期编号；两个不同编号即使同标题也保留两个记录。同名标题只有
  在调用方显式允许且唯一匹配时才可选中，并带 `SEMESTER_SELECTED_BY_TITLE`。
- 课程关联：优先 `course_id` 或教学班编号；只有标题时返回候选但 `course_uid`
  为 `null`，绝不按名称合并。未索引的来源编号仍生成带来源与学期范围的
  `course_uid`，并标注 `COURSE_NOT_IN_SOURCE_INDEX`。
- 课表条目：保留教学周、单双周、星期、节次、日期区间、地点、平台时间和教学班/
  重修字段。条目编号为模块内稳定键 `<source>:TIMETABLE:<semester>:<record>`；
  缺少记录号但已有课程/教学班与星期节次时，生成确定性的来源内临时键
  `<source>:TEMPORARY_TIMETABLE:<semester>:<digest>` 并标注
  `identity_state=TEMPORARY_SCOPED` 与 `TEMPORARY_SCOPED_IDENTITY`，标题不参与；
  连课程与星期节次都没有时保留条目但 `identity_state=MISSING_STABLE_ID`。
- 周次展开（`week-expansion-v1`）：只有显式校历才产生日期，只有显式节次表才产生
  时刻；缺校历保留原始周期，缺节次表只给日期且不给虚构时刻；节假日、调课替换和
  来源声明的 `date_range` 之外日期属于正常跳过（记录在 `skipped`，不把状态降为
  `PARTIAL`），教学周以外的意外缺口才降级；调课日只在所属周被选中或替换本 pattern
  的自身日期时参与；校历与节次表学期不一致直接 `CALENDAR_MISMATCH`。展开结果带
  `derivation`（规则版本、日期/时刻来源、`not_a_source_fact=true`）。
- 考试：保留考试编号、课程关联、日期、开始/结束时间、地点、座位、状态精度和
  发布状态。只有日期时 `time_precision=date`，不补午夜；只有结束时间、只有时刻
  没有日期、结束早于开始都失败关闭；补考、缓考、改期、取消是独立状态；来源未声明
  覆盖全部考试时结果 `complete=false` 并给出 `EXAM_COVERAGE_NOT_DECLARED`。缺少
  考试号但已有课程与日期/开始时间时生成 `TEMPORARY_EXAM` 临时键。
- 成绩：严格输出 `grade-v1`。未发布为 `UNGRADED`，扣留为 `WITHHELD`，未知为
  `UNKNOWN`；零分是 `GRADED` + `0`；等级制与通过制保留文本；未知制式保留来源值
  并标注。不计算 GPA、学分合计或排名，也不把作业成绩当课程成绩。

## 成绩隐私

`academics.sensitivity` 固定以下策略：

- 查询日志只记录计数与状态分布，不含成绩原值或课程标题；
- 公共看板摘要完全排除成绩，只允许计数；
- 模型上下文默认不包含成绩，只有 `explicit_request=True` 且用途已登记时才可读；
- 缓存必须来自加密存储，普通 JSON 缓存被 `ACADEMICS_GRADE_CACHE_FORBIDDEN` 拒绝；
- 成绩不会转换成日程事件或待办。

## 事件转换

`calendar_events('exams', ...)` 与 `calendar_events('timetable', ...)` 返回
`calendar-event-v1`。考试状态由 `state` 与 `release_state` 按
`exam-event-v1` 推导：已发布且正常才 `CONFIRMED`，补考/缓考/改期为
`TENTATIVE`，取消保持 `CANCELLED`，未知状态只能是 `UNKNOWN`。课表 occurrence
为 `TENTATIVE`，日期有而节次表缺失时使用 `time.kind=DATE`。`PARTIAL` 展开只要
仍有 `occurrences` 就继续转换，并把 `skipped` 原样返回；时间不是合法对象时转换器
失败关闭。事件 `record_id` 直接使用既有 `exam_uid`/`entry_uid`/`temporary_uid`
或 `<source>:<record>`，不做有损替换；重复 `event_uid` 会进入 `excluded` 并给出
`EVENT_UID_CONFLICT`。`calendar_events('grades', ...)` 直接抛出
`ACADEMICS_GRADE_NOT_EVENT`。

## 公共接线

本包不修改 `queries.py` 注册、`todos.py` 或 MCP 工具。`operation_schemas()` 给出
只读参数（学期、课程、业务范围、分页上限），由 6.1 统一登记；`scope` 只有
`SEMESTER` 与 `COURSE`，`COURSE` 必须携带 `course`，按 `course_id`/
`teaching_class_id`/`title` 精确过滤；`limit` 真正截断并在 `read_scope.limit`、
`truncated` 与 `RESULT_LIMIT_REACHED` 中体现。`describe()` 提供能力启用表和规则
版本；`errors.classification_map()`/`classify()` 给 6.1 提供冻结契约登记前的分类
桥接。解除阻塞需要按 `docs/contracts/academic-sources.md` 取得脱敏来源证据，更新
`source-registry.json` 与 `adapter-readiness.json` 后再接生产传输。

## 测试

`tests/test_academics.py` 使用 `tests/fixtures/academics/` 的合成学期、课程、课表、
校历、节次表、考试、成绩和非法行，覆盖同名课程与教学班、周次/单双周/节假日/调课、
缺校历与环节次表、考试精度与状态、未发布/扣留/零分成绩、成绩隐私、分页失败和阻塞
结果。全部为离线合成验证。
