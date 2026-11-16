# Openthu v0.2 契约

状态：已冻结（1.1 版本基线、架构契约与调度冻结）
目标版本：Openthu v0.2.0
契约版本：`*-v1`（见每个文件中的 `x-contract-version`）

本目录是 v0.2 各模块共享的唯一接口基线，包含 **8 类契约 + 1 份兼容基线**。MCP、CLI、本地看板、查询、事务、存储和媒体模块都必须读取这里的定义，不得各自发明一套字段或状态。每个契约文件都带 `x-owner`，当前 owner 为 `1.1/Hermes`；后续变更必须同时更新负责表。

## 八类契约与兼容基线

| 文件 | 契约版本 | 负责内容 |
|---|---|---|
| `result-envelope.schema.json` | `result-envelope-v1` | 统一结果信封；保留旧字段，新增可选实现/运行/证据状态 |
| `error-classification.json` | `error-classification-v1` | 错误类别、重试语义和禁止自动重放规则 |
| `storage-header.schema.json` | `secure-storage-header-v1` | 跨平台加密文件头、用途、账号域、服务域和密钥策略 |
| `source-evidence.schema.json` | `source-evidence-v1` | 来源证据、读取范围、完整性、失败/空/部分三态 |
| `media-reference.schema.json` | `media-reference-v1` | 图片/附件引用、图片上限和本地隐私边界 |
| `calendar-event.schema.json` | `calendar-event-v1` | 日期级/时刻级事件、稳定标识和导出边界 |
| `business-facade.schema.json` | `business-facade-v1` | 查询、健康、准备、执行、结果核对、请求取消 |
| `local-read-api.schema.json` | `local-read-api-v1` | 本地只读 API 路径、分页、监听和同源安全边界 |
| `compatibility-baseline.json` | `compatibility-baseline-v1` | 29 个 MCP 工具、6 个入口、旧结果字段、旧错误形状和默认限制 |

`contracts/v0.2/samples/` 提供来源失败/空/部分、未知来源阻塞和同名不同编号稳定标识的合成夹具。`tests/test_v020_contracts.py` 把校验器接入 `scripts/dev.py test` 和 CI。

## 冻结规则

1. 每个契约都有版本、必填字段、未知值表示、兼容样例、拒绝样例和 owner。修改必须登记影响模块，并保留旧版本可解析性。
2. 未知值使用 `"UNKNOWN"`；数值或可选对象未知使用 `null`。`null` 不表示成功，空数组只有在 `status=EMPTY`、`source_complete=true`、`complete=true` 时才是真实空结果。
3. 来源失败、真实空列表和部分读取是三组不同夹具。HTTP 200、目录命中、页面可读或模型回答都不等于业务成功。
4. 旧工具名称、参数、结果字段和导入路径保持兼容。新增字段默认可选，旧客户端不传也能工作。旧错误对象只有 `code`/`message` 时，facade 按 `error-classification-v1` 补齐分类字段；新代码直接输出分类错误。
5. 写操作只通过 `business-facade` 的 `prepare` / `execute` / `result`。`authorization` 字符串只是用户意图记录，可信宿主授权是独立回调，不能由 MCP 参数、网页内容或模型输出伪造。
6. 本地看板只读，不提供写入、付款、密钥配置、模型生成或任意上游代理接口。默认只绑定回环地址，使用同源策略和 `no-store`。
7. 图片首版只接受经本地检查的 JPEG/PNG/WebP；单图 8 MiB、每次 4 图、总 16 MiB、单图 1600 万像素。平台契约更严格时取较小值。
8. 加密存储的用途、账号域和服务域必须分开。Windows DPAPI 旧记录保留原格式读取，不自动迁移，也不把 Windows 凭据导入云端。
9. 所有样例都是合成数据，不含真实账号、Cookie、密码、模型凭据、个人记录、主机名或绝对路径。

## 验证

```sh
python contracts/v0.2/validate.py
python -m pytest tests/test_v020_contracts.py
```

校验器使用 JSON Schema Draft 2020-12 校验兼容样例，并断言每个拒绝样例按声明被拒绝；不能由 JSON Schema 表达的规则（例如结束早于开始、未知来源不计为已实现）由显式语义检查和合成夹具覆盖。2026-11-16 审计修复后的结果为：9 个兼容样例通过、35 个拒绝样例全部拒绝、8 个额外夹具通过。

## 变更流程

新增字段或枚举时，先更新本目录契约并递增 `x-contract-version`，再登记受影响的模块、兼容样例和拒绝样例。未完成影响分析前，不得宣布相关批次完成。模块可以针对冻结契约写显式夹具并行开发，但夹具不能进入生产默认路径。
