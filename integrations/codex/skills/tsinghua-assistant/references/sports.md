# 体育场馆

清华助手使用体育站的官方登录会话。用 describe_service("sports") 查看接口，query_service 的 service_id 填 sports。

| 想做的事 | 操作与参数 |
|---|---|
| 找项目 | scenes 支持 query。保留学校名称和 scene_id。名称相近时向用户展示候选。 |
| 查一天的余量 | availability 传 scene_id 或项目名称 name，date 采用北京时间。多个房间时从 candidates 选 room_id。mode 可选 SPORT_GROUP（团体场地）或 SPORT_PERSON（个人票务）。省略时登记实际读到的模式。 |
| 看本人预约 | orders 支持 page、limit，每页最多 50 条。next_page 表示下一来源页，覆盖范围为本人的场地预约。 |
| 核对一笔详情 | order_detail 传本人订单列表返回的 record_id。保留预约、审批、付款、退款和付款截止各自的状态。 |
| 汇总待办 | get_todos 支持 services=["sports"] 或 ["sports.orders"]。默认读取体育预约并按本次查询窗口过滤。 |

按 session_id、开始/结束时间及平台逐场次状态判断。空列表、自由时段、分时表缺失或未知状态按 warnings/complete 解释。费用 unit_fee_fen 为平台单价，单位为分。可约、容量和费用均以检查时间为准。多页使用 query_service 快照游标，再按 next_page 取得下一来源页。请求频繁时尊重 retry_after_ms。

## 预约与取消

prepare_service_action 的 service_id 为 sports。create_reservation 需要 scene_id、room_id、site_id、session_id、date、mode、members、payment_method。单人填 members="self"，付款方式填写 FREE、PAY_ONLINE 或 PAY_OFFLINE。可给 expected_total_fen 绑定用户确认的应付总额。多人、个人票务、付费、表单、滑块验证等场景在预览中标出官方处理项目。当前学校启用了官方验证。cancel_reservation 单独需要 record_id，以当前本人记录读取场地、时间、取消期限和已付金额。

先展示具体事项，用户明确授权这一笔操作后，execute_service_action 使用 plan_id 和本次授权说明。体育适配器通过 Windows 本机确认窗口绑定计划、账号、字段和有效期。十分钟内价格、时间、余量或规则改变时重新准备。AWAITING_OFFICIAL 时打开结果的 official_url，依预览在学校页面核实并由本人完成所需步骤。保留 plan_id，返回后用 get_action_result 核对，必要时用 orders 和 order_detail 查询。不得用验证码识别或第三方打码代替本人操作。付款在官方界面完成。

结果包含平台记录号、核对时间及可核实的付款、退款状态。PENDING_PAYMENT 应给出官方入口和平台付款期限；退款单独查看 refund_status。远端资料不足、参加者无法唯一匹配、价格凭据缺失或多笔相似订单时保留待核对状态。重复执行同一个未决计划只读取记录。THU_SPORTS_WRITE=0 停用新的办理，保留查询和既有事务核对。THU_SPORTS_READ=0 停用专用查询和新办理。

