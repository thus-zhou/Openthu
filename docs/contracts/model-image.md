# 模型图片输入能力边界

相关契约：`model-image-capability-v1`、`media-reference-v1`、`source-evidence-v1`。目标模块：4.5。

## 目录标记不等于传输能力

官方模型目录返回 `supportImage` 布尔值，现有代码把目录状态标为 `LISTED_NOT_PROBED`。这只说明前端目录列出了该标记，不能说明：

- 当前认证代理接受图片内容；
- 该模型通过当前代理接受图片；
- 平台允许的字节数、像素数、图片数量；
- 图片与文本的请求结构；
- 被拒绝时的响应和错误语义。

因此 `catalog_support_image=true` 只能得到 `capability_state=UNVERIFIED`，不能得到 `AVAILABLE`。

## 当前请求与限制

现有 `madmodel.generate` 的请求体是文本消息 `messages`，没有图片内容部分。模型目录的 `supportImage` 被保留为目录字段，但不进入请求构造。工程上限（JPEG/PNG/WebP、单图 8 MiB、每次 4 图、总 16 MiB、单图 1600 万像素）是 Openthu 的本地校验上限；平台若更严格，取较小值。

本地可以做：

- 文件类型、字节数、像素数、哈希和临时目录检查；
- 不合格文件的明确拒绝；
- `UNVERIFIED`、`UNAVAILABLE` 和失败路径；
- 合成图片夹具（不是真实个人图片）。

本地不能做：

- 用目录标记直接把能力标成可用；
- 用个人图片试发来探测格式；
- 把图片二进制、绝对路径或认证 URL 写入公开契约；
- 绕过类型/大小/像素检查。

## 状态判定

| 目录 | 协议 | 能力结论 | 说明 |
|---|---|---|---|
| `LISTED` + `supportImage=true` | `UNVERIFIED` | `UNVERIFIED` | 目录有标记，传输未证明 |
| `LISTED` + `supportImage=true` | `VERIFIED` | `AVAILABLE` | 需要实际请求结构、模型和限制证据 |
| `NOT_LISTED` | 任意 | `UNAVAILABLE` 或 `UNVERIFIED` | 不在目录不等于协议一定不可用，但不能声称可用 |
| 任意 | `MISMATCH` / `UNAVAILABLE` | `UNAVAILABLE` | 目录与代理行为不一致时以更保守结论为准 |

合成夹具：[model-catalog-mismatch.json](../../tests/fixtures/sources/model-catalog-mismatch.json) 固定“目录支持、协议未验证、能力未验证”。

## 解除阻塞

需要官方文档或经用户授权的合成图片探测，记录：请求媒体结构、支持模型、字节/像素/数量限制、响应形式、错误语义。没有这些证据时，4.5 只实现本地媒体校验和不可用/未核对结论，不实现生产图片传输。
