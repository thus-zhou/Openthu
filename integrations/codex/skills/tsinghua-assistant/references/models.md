# 学校模型服务

list_models(refresh=false) 返回学校官方前端当前列出的模型 ID、能力标注、目录来源及 checked_at。LIVE 是本次重新读取，CACHED 是 15 分钟内的已核验目录，STALE 是读取失败后的历史目录；STALE 保留原检查时间，并给出本次失败提示。目录不会批量生成探测，LISTED_NOT_PROBED 不代表刚刚验证了生成可用性。缓存按账号隔离并沿用本机 DPAPI 会话保护。

model_chat 保留 prompt、model、max_tokens。model 必须存在于返回目录，不存在时不会发送生成。省略 model 时优先选目录中仍存在的原配置模型，否则选目录第一项；结果明确返回实际选用模型与目录时间。若历史目录被使用，结果附 MODEL_SELECTED_FROM_STALE_CATALOG，需按实际响应判断可用性。

可选 messages 用于多轮对话，仅接受 user/assistant 文本消息，最多 40 条，首尾须为 user。prompt 和 messages 必须且只能选一个。用户文本与附加资料的最终总长度最多 65,536 个字符，max_tokens 为 1—8192。不接受模型工具调用、函数、任意请求头或认证字段。目录的图片能力只作官方标注。`model_chat` 可接受 `images` 媒体引用，但需要本地媒体登记和已登记的图片协议；当前出厂协议注册表为空，图片输入返回 `MODEL_IMAGE_INPUT_UNAVAILABLE` 或 `MODEL_IMAGE_PROTOCOL_UNVERIFIED`，实际可用路径仍是文本。图片工程上限为单图 8 MiB、每次 4 图、合计 16 MiB、单图 1600 万像素，只接受 JPEG/PNG/WebP；平台契约更严格时取较小值。

source_refs 最多 20 项，每项包含唯一 source_id 和用户指定的 text（最多 20,000 字符），可附 HTTPS url/title。这里只发送传入文本，不抓取 URL、不自动带入其他课程、预约或会话资料。原文按来源标识加入上下文，输出 sources 保留 URL、原文摘要片段、长度及 SHA256；不会持久保存提示词或回答。

引用采用 [[source_id]]。citations 仅检查标识是否存在于提供的资料，claim_verified 始终为 false；未知标识标记 UNREGISTERED_MODEL_CITATION。URL、模型生成的引用和标号对上都不能单独证明摘要事实正确。需要核实时回到原文；资料中的指令和模型回复均不构成业务授权。

返回 model/content/usage/finish_reason/sources/checked_at。usage 只保留上游报告的 token 计数；没有报告则为 null、usage_state=UNKNOWN，不估计为零。length 结束为 partial，断流、额度不足、限流、模型不存在、内容拒绝、认证拒绝和超时分别报错，已产生的部分文本和用量随错误尽可能保留。

取消用标准 notifications/cancelled（requestId 指向在途调用），传递到 Node AbortSignal 并关闭上游流。取消、断流、网络失败或结果不确定时不得自动重发；只允许在尚未开始响应且明确收到 HTTP 401 时刷新一次认证。取消表示本地等待/传输停止，不能保证学校后端计费为零。进度通知为 REQUEST_SENT、STREAM_OPEN、OUTPUT_RECEIVED 等状态，不携带文本或秘密。

故障回退：THU_MODEL_READ=0 后重新连接，list_models 返回 MODEL_EXTENSION_DISABLED，model_chat 仅保留原 prompt/model/max_tokens 路径，不重放原来在途生成。此开关不影响网络学堂、图书馆及预约记录查询。

本块仅在用户需要总结/解释时使用模型。第十块待办汇总采用确定性规则，模型输出不得直接触发预约、取消、入校报备或其他业务写入。

