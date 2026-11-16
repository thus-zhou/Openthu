# 图书馆座位和研讨间办理

使用 describe_service("library") 查看本机登记的办理操作。query_service 仍提供 seat_areas、seat_availability、room_types、room_availability、reservations。先选定资源，读取本次日期和时段的情况。

| 事项 | prepare_service_action 的 operation 和 parameters |
|---|---|
| 座位预约 | create_seat_reservation。给 area_id、seat_id、date、start_time、end_time。时段取平台提供的完整时段。预览核对座位图的控件编号、座位名称、区域、日期和时段。 |
| 研讨间预约 | create_room_reservation。给 kind_id、room_id、date、start_time、end_time、headcount、members 和 purpose。members 第一项为 self，其余填写该笔预约的真实成员登录标识。人数包含本人。 |
| 取消 | cancel_reservation。给 system（seat 或 room）、record_id 和 date。记录号取本人的 reservations 返回值，使用该预约的开始日期。核对位置、时间、当前状态、取消规则和涉及的参加者。 |

三项工具的 service_id 均为 library。成员需要与学校查询返回的实际账号逐一对应。成员缺失时请用户提供这笔预约的成员，在官方页才能辨认的成员保留为待确认。成员信息仅在办理所需的内存及本机加密事务内处理，普通预览提供人数。研讨主题、预约时段及成员应符合用户本次提供的信息。

用户明确指定一笔办理后，使用 execute_service_action，保留 plan_id。计划有效期十分钟。本机窗口显示该计划的具体资源、时间、参加者和取消条件。执行前重新读取余量、规则、账号状态及成员。AWAITING_OFFICIAL 表示需在 official_url 核对座位选择、成员、验证码、表单或校方规则。请在学校页面处理所列事项，随后用 get_action_result 读取平台记录。

已核验的办理结果提供平台记录号、检查时间和刷新对应待办的 todo_query。研讨间等待审批或成员同意时保持 PENDING_APPROVAL。记录暂时不可见、存在多个近似记录、身份无法核实，或取消后仅看到记录消失而缺少学校受理证据时，保留 UNKNOWN 或等待官方处理状态。再次执行同一未决计划只核对结果。座位“用户取消”按已取消筛选；历史快照保留原检查时间。

设置 THU_LIBRARY_WRITE=0 并重新连接可以停用新的图书馆办理，原有只读查询和已发出事务的核对仍可用。实际签到、门禁和借阅续期另按对应业务处理。

