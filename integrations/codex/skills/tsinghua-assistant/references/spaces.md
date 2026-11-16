# 座位与研讨间查询

第 8 块于 2026-11-05 接入，使用学校官方页面的只读接口。统一入口为 query_service(service_id="library")，先 describe_service 查看参数。

| 操作 | 用法 |
|---|---|
| seat_areas | 列出官网当前可见馆区、楼层、阅览区；parent_id 按上一级筛选，query 搜完整路径。capacity 是目录容量，不是当前余量。 |
| seat_availability | 用 area_id 或精确 name（也可用完整路径），date 默认北京时间今天。非末级区域返回下一级候选。start_time/end_time 必须成对。返回平台可用日期、时段、座位 ID 和所查时段状态。 |
| room_types | 列出研读、研讨等空间类别，query 可筛名称。 |
| room_availability | kind_id 或精确 name，date、起止时间、people 可选；page 默认 1，limit 默认 10、最大 50。返回房间、人数范围、开放与占用时段、平台规则和处罚记录是否存在。 |
| reservations | system 必须是 seat 或 room，分别查询。seat 按 page 读取全部历史的固定 20 条一页，不接受日期/limit/status_mask。room 支持日期范围（默认今天）、page/limit、status_mask（默认 6，即待生效/有效，不包括所有状态）。 |

资源重名返回 NEEDS_USER_ACTION 和 candidates；不得自行选取。NOT_FOUND、NOT_OPEN、AUTH_REQUIRED、FORBIDDEN、接口失败与 EMPTY 分开解释。只有经过校验的成功查询才可报告空列表。

座位仅核验平台 offered_slots 给出的完整时段。官方座位图会调整自定义开始时间，因此不能把任意小时间段的查询当成已核验能力；不匹配平台时段时返回 PLATFORM_TIME_SLOT_REQUIRED 及实际提供的时段，需重新选择。研讨间可按选定起止时间检查占用交集和时长限制。

每次不带 cursor 的查询都会重新读取官方来源，不缓存余量。checked_at 是本次检查时间，所有时间使用 Asia/Shanghai。next_cursor 只续读同一检查时刻的快照；next_page 用新的 page 查询来源下一页。complete 仅指本次来源页及其快照已完整，next_page 非空时不能说已读完全部记录。游标与当前账号绑定，十分钟失效，不能把不同时间的快照拼接为同一次完整查询。

FREE_TIME_ONLY 或 free=true 仅描述该时间范围的占用情况；eligibility=UNVERIFIED 表示未证明本人能够预约。PARTY_TOO_SMALL/PARTY_TOO_LARGE、BUSY、NOT_OPEN、DURATION_OUT_OF_RANGE 分别呈现，不推断所有限制已满足。占用时间已去除其他用户姓名、账号、预约主题。没有处罚记录也不等于具备全部预约资格。

规则来自本次官方页面/API，规则来源与 checked_at 一起呈现。座位入口首页余量不是某个时段的余量。研讨间 resvRule 的原始字段与分钟值保留，不能把第三方文档的时间/罚则当作当前规定。更详细的校验、跨天预约及办理范围见 [图书馆预约](library-bookings.md)。

个人预约含平台记录号、资源、起止时间、状态及取消条件说明。座位列表读取“我的预约”官方表格；研讨间默认仅查询平台状态掩码 6，其他状态要明确指定。取消入口可见不代表保证取消成功。本块不执行预约、取消、签到、签离或修改。

失败时可通过 browser_open 进入对应官方页面继续读取，不能关闭写入保护。THU_SPACE_READ=0 仅停用本块操作，THU_LIBRARY_READ=0 停用全部图书馆专用查询。均不影响官方已有预约。



第十二块已接入座位和研讨间的预约、取消预览及记录核对。具体办理范围与写入验收状态见 [图书馆预约](library-bookings.md)。
