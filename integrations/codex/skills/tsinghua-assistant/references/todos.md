# 统一待办

使用 get_todos() 按规则汇总网络学堂当前学期作业、当前借阅、座位预约、研讨间预约和体育场馆预约，不调用学校模型，不创建定时任务或提醒。

## 查询与范围

- 默认北京时间今天至第 7 天（含首尾），包含未完成的逾期作业和借阅。只给日期的截止保留 YYYY-MM-DD，不能解释为当天零点。
- start_date/end_date 可指定闭区间，最长相差 366 天；include_completed=true 显示窗口内已交/已批作业及已取消、已结束、被拒绝等预约。
- services 可选 learn、library、sports，或细分 learn.assignments、library.loans、library.seat、library.room，体育可用 sports 或 sports.orders。亲友入校待第十三块接入。
- 默认每个来源最多读取 3 页，max_source_pages 可设为 1—50。座位只能读取全历史，不能假设历史按开始日期排序后提前结束。达到上限时 sources_status 会列 SOURCE_PAGE_LIMIT、next_upstream_page，complete=false；不要把过滤后空列表说成没有预约。用户需要完整记录时可提高上限重新查询，或沿 query_service 的 next_page 继续核对。
- 作业仅当前学期；教务日历没有与作业的可靠关联，不纳入待交作业。不存在关联证据时不按同名合并；同课程、同标题、同截止但不同 ID 标记 possible_duplicate_of。
- 研讨间按请求日期和全部状态位读取。过去的预约不会自动视为未完成逾期事项。已过开始时间也不能据此判定未签到。

## 结果和续页

返回 items、sources_status、source_complete、complete、next_cursor、window、groups。每项保留 event_id、service、source、source_id、title、start_at/due_at、date_precision、status、source_url、checked_at。原始入口只用于核对，不自动办理业务。

一次最多 200 项。续页须携带同样参数和 cursor，不要补写先前未指定的日期；游标绑定账号、查询参数和十分钟内的固定快照。即使跨午夜，续页仍属于原来的查询窗口。groups 是整个聚合快照的分组计数，不只是本页。snapshot_complete 只表示快照读完；source_complete=false 不能靠翻完聚合页变成 true。

sources_status 分别报告 SUCCESS、PARTIAL、FAILED、范围、读取页数/条数、失败和检查时间。全体来源成功且确认为空才返回 EMPTY。缺失日期进入 unscheduled；缺失状态保持 UNKNOWN，不推断已完成或待处理。冲突的重复记录标未知并降低完整性；已交待批作业不再列为待交，取消预约不再列为待参加，座位官方“已使用”映射为 ATTENDED 并在 include_completed=true 时展示，PENDING_APPROVAL/PENDING_PAYMENT 不是已批准或已支付。

## 历史缓存

默认每次重新查询。allow_stale=true 仅允许读取失败来源显示最近一次、同账号同源查询范围的 DPAPI 加密缓存（最多七天）；不覆盖已有实时部分结果。freshness=HISTORICAL、原 checked_at 和 refresh_attempted_at 必须同时说明，聚合完整性仍为 false。新查询失败绝不返回旧数据冒充实时；DPAPI 不可用不保存明文。进程中的续页快照也是原检查时间的记录，不能用来证明预约实时状态。

## 停用

设 THU_TODOS=0 并重新连接插件即可停用聚合工具；各站点 query_service 保持可用。缓存位于校园状态目录的 todos-v1，不属于事务日志；不修改认证或未决预约记录。

