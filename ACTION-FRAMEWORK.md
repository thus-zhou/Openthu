# 第五块：业务执行框架

本块仅验证本地事务机制。没有生产站点写适配器，也没有可从模型参数伪造的授权通道。体育、座位、研讨间、亲友报备各登记 create/cancel；`describe_service` 的 actions 明确返回 enabled=false。第 11—13 块接入真实站点并验收后才可能办理。

## 使用

`prepare_service_action(service_id, operation, parameters)` 创建十分钟计划。创建必需 target、date、start_time、end_time、members、headcount、fee、payment_method；取消必需 target、record_id。members 暂为字符串，由后续站点契约定义结构及规范化；“无成员/不适用/免费”也应由适配器明确解释。缺字段返回 NEEDS_USER_ACTION，未适配返回 ADAPTER_REQUIRED 原因。未知参数和未登记操作在网络访问前拒绝。

`execute_service_action(plan_id, authorization)` 的字符串仅记录实际用户指令。执行还要求可信宿主提供绑定 plan_id、账号、digest、adapter_version、挑战值和到期时间的证据。证据有两个来源：本机 `python -m thu_assistant.consent --plan <plan_id>` 签发的单次授权，或代码内的 `consent_verifier` 回调（现有 Windows 消息框、合成测试）。旧式确认字典会在引擎内规范化为签名授权后才落盘。回调返回 `consent.DECLINED`（Windows 消息框点“否”即此语义）会否决并删除待用授权；返回 `consent.NOT_APPLICABLE` 才回退到待用授权；返回 `None` 表示没有证据，本次拒绝且不回退。MCP 参数、网页文本、模型输出和普通环境变量都不能生成证据；没有证据时保持拒绝，计划不变。授权只对一笔计划有效，预检成功后才消费，公开结果不包含授权值、签名或挑战值。

`get_action_result(operation_id)` 在重启后恢复日志，有适配器时只查询记录。原 prepare/execute_browser_action 参数保持兼容，通用表单只能预览，提交返回 ADAPTER_REQUIRED。不再有同域 15 秒写入窗口。

## 站点适配器契约

生产注册表默认空，只有维护者审查并修改代码才能添加适配器。适配器包含 version、preview(business)、preflight(record)、dispatch(record, gate)、reconcile(record)。测试中的 Adapter 是合成夹具，不能用作真实站点实现。

- preview 规范化完整业务字段；计划绑定账号、字段摘要与版本。浏览器采集 form-associated controls、checkbox/radio.checked、select.selectedOptions、ARIA/data 选择属性、隐藏业务 ID、页面 document ID 和 iframe 位置。CSRF/认证字段不进入摘要。自定义前端私有状态无法通用穷举，form_complete=false；必须由具体适配器补全并在 preflight 重查。
- preflight 返回 business、context、checks（availability/eligibility/fees/rules 均须 true）及 RequestSpec 序列。任何业务字段或页面绑定变化都使旧计划失败。请求中的新 CSRF 可刷新，但不能改变业务内容。站点代码负责从已重查业务构造请求；不得直接执行模型传入的 URL、脚本或 headers。
- RequestGate 对 URL（含参数）、方法、规范化请求体、frame 做精确有序单次匹配；JSON 重复键拒绝，form 重复参数保留。浏览器提交阶段不沿用宽泛 POST 查询启发规则。同域无关写请求被阻断。多步骤适配器须确保每一步经过 gate，外部支付和未核实依赖不默认授权；接口需要更细的 header/内容类型约束时应扩展并验证契约后才启用。
- dispatch 获得能力前，DISPATCHING 已原子落盘。NotDispatched 只可用于有证据证明尚未发出的错误；发送或结果无法确定均进入 UNKNOWN。gate 被消耗后不重试。实际 API 适配器也必须使用 gate，禁止裸 HTTP 重放。
- reconcile 返回 complete=true 及 records，每条须含 service_id、account、operation、完整 business、record_id、state。恰好一个匹配记录才允许 SUCCEEDED/PENDING_PAYMENT/PENDING_APPROVAL/FAILED。零条、重复、缺页、查询失败均 UNKNOWN。平台 HTTP 200、弹窗和点击不构成成功证据。

## 持久状态与恢复

默认 `%LOCALAPPDATA%/TsinghuaAssistant/actions-v1`；可随现有 THU_ASSISTANT_STATE_DIR 隔离。DPAPI 保护业务原值和授权记录；外部返回成员和记录号脱敏，通用表单值全部隐藏。原子临时文件 + flush/fsync + replace；跨进程 OS 文件锁仅覆盖本地读改写，锁文件不删除，进程退出后锁由 OS 释放。加密失败不降级明文。日志失败不猜测“没提交”。

状态：PREPARED → DISPATCHING → UNKNOWN → 已核实结果；缺资料为 NEEDS_USER_ACTION，未适配为 PREPARED + reason=ADAPTER_REQUIRED（不能执行，也不提供本机确认命令）；过期或前置变化为 FAILED；PENDING_PAYMENT/PENDING_APPROVAL 可继续核对。DISPATCHING 的进程已退出或超过 120 秒时只进入核对。Windows 使用只读进程查询，禁止用 os.kill(pid,0) 探测。终态不会因迟到的派发返回而降级重发。

每个状态转换使用带修订号的条件更新；派发意图（DISPATCHING、授权证据、进程号）必须在请求发出前原子落盘，保存失败时不发出请求。多阶段适配器可声明 `accepts_ledger = True`，用副作用清单逐步记录 NOT_SENT/SENT/UNKNOWN；超时后不得自动重复上传，关闭页面不撤销远端草稿。公开结果包含 `effects`（步骤与状态）、`state_history`（最近 20 条）、`event_count` 和 `next_actions`，不含业务原值或授权值。只读轮询在状态、原因和远端记录都未变化时不改写日志、不追加事件。

本地 operation_id 不是平台幂等键。同账号、服务、操作和业务摘要的未决或已成功事务重复准备返回已有记录；失败后可重新准备，但仍须重新检查与具体授权。PREPARED 还绑定上下文和适配器版本。UNKNOWN/PENDING 日志不因到期自动删除。

## 回滚

先停止新增业务调用；保留 actions-v1 原目录及第五块完整运行时备份，不删除未知/待审批/待付款日志。恢复旧查询版本时，禁用旧 execute_browser_action，不能恢复旧同域放行逻辑。第五块运行时可独立保留用于 `get_action_result`；无适配器时只报告本地状态。

离线盘点：`python -m thu_assistant.action_recovery --state-dir <actions-v1绝对目录>`，只列操作 ID、服务、状态、版本和副作用；单个日志损坏或属于其他 Windows 用户时，其余未决事务仍会列出，并逐条报告错误类别。命令不访问网络、不解密打印业务原值、不发起取消。日志依赖原 Windows 用户 DPAPI，代码快照刻意不包含日志/会话/凭据；不能把代码恢复成功等同于事务记录已迁移。

