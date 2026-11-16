# v0.2 校园来源证据包

状态：2.3 模块交付，2026-11-16。
上游方案：`plans/v0.2.0/模块/2.3-校园来源证据与业务数据契约.md`。
说明文档：[../../../docs/contracts/README.md](../../../docs/contracts/README.md)。

本目录是来源登记、规范模型、合成夹具和条件适配结论的机器可读入口。它不修改运行时查询注册表，也不发起真实校园、模型、预约、付款或作业提交。

## 文件

| 文件 | 内容 |
|---|---|
| `source-registry.schema.json` | 来源登记行的 JSON Schema |
| `source-registry.json` | 29 个现有/计划来源的访问边界、字段映射、分页、错误和未决项 |
| `source-tasks.json` | 课表、考试、成绩、作业提交、续借、图片输入、来访报备的来源任务 |
| `adapter-readiness.json` | 后续模块的 `CONTRACT_READY` / `BLOCKED_SOURCE_EVIDENCE` 启动结论 |
| `data-models/manifest.json` | 10 个规范模型清单（事件复用 1.1 的 `calendar-event-v1`） |
| `data-models/*.schema.json` | 课程、资源、成绩、订单、附件、作业、提交、续借、图片能力 |
| `module-result.json` | 2.3 的机器可读结果摘要：路径、检查、阻塞和下一步 |
| `validate.py` | 离线校验器：schema、样例、拒绝样例、任务、就绪表、夹具和隐私 |

合成夹具位于 `tests/fixtures/sources/`，测试入口是 `tests/test_source_contracts.py`。

## 证据边界

- `OFFLINE_CONTRACT` 只证明代码、字段和结构可核对，不证明真实站点当前可用。
- `SYNTHETIC_TRANSPORT` 只证明合成传输/受控浏览器路径，不证明真实业务成功。
- 计划中但没有可靠来源的能力保持 `BLOCKED_SOURCE_EVIDENCE`，不能用占位实现或假成功消除。
- 公开文件只含公共入口、字段名、结构、合成样例和摘要；不含原始页面、Cookie、请求头、账号或本地绝对路径。

## 验证

```sh
python contracts/v0.2/sources/validate.py
python -m pytest tests/test_source_contracts.py
```

期望结果：29 sources、10 models、8 tasks、14 readiness、7 fixtures、9 schema samples、21 rejection samples，`ok=true`。
