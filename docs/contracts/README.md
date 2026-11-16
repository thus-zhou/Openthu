# v0.2 来源证据与业务数据契约

状态：2.3 模块冻结（2026-11-16）。目标版本：Openthu v0.2.0。
机器可读入口：[source-registry.json](../../contracts/v0.2/sources/source-registry.json)、[data-models/manifest.json](../../contracts/v0.2/sources/data-models/manifest.json)、[module-result.json](../../contracts/v0.2/sources/module-result.json)。

本目录回答三个问题：**某项校园能力依据什么来源、返回什么规范数据、现在还缺什么证据**。它不声称任何真实校园账号已完成登录、预约、付款或作业提交。

## 文件

| 文件 | 用途 |
|---|---|
| [source-matrix.md](source-matrix.md) | 现有与计划来源的访问边界、代码状态、证据等级和未决项 |
| [data-models.md](data-models.md) | 课程、资源、成绩、订单、附件等规范结构、稳定标识与未知语义 |
| [academic-sources.md](academic-sources.md) | 课表、考试、成绩的独立来源任务与阻塞结论 |
| [action-boundaries.md](action-boundaries.md) | 上传/草稿/正式提交、续借、付款与取消的边界 |
| [model-image.md](model-image.md) | 模型目录标记与实际图片传输能力的区分 |
| [evidence-policy.md](evidence-policy.md) | 公开证据的脱敏、版本、检查和回退规则 |

## 证据等级

| 等级 | 含义 | 能否证明真实业务成功 |
|---|---|---|
| `NONE` | 没有可用来源证据 | 否 |
| `OFFLINE_CONTRACT` | 代码、字段、合成样例和结构契约可核对 | 否 |
| `SYNTHETIC_TRANSPORT` | 合成传输或受控浏览器测试通过 | 否 |
| `LIVE_READ` | 在明确账号和日期下完成过真实只读 | 只能证明该次读取 |
| `LIVE_WRITE` | 在明确事项和授权下完成过真实写入 | 只能证明该次事项 |

历史验收记录只作为历史事实，不自动成为本轮证据。模块 2.3 未发起真实校园调用、模型调用、预约、付款或作业提交。

## 条件适配启动结论

后续模块必须读取 [adapter-readiness.json](../../contracts/v0.2/sources/adapter-readiness.json)，不能把“契约已就绪”当成“生产适配已实现”。

- `CONTRACT_READY`：内部模型和失败路径可直接开工。
- `CONTRACT_READY_WITH_LIMITS`：可开工，但必须保留来源范围、空/错/部分三态和官方页面限制。
- `BLOCKED_SOURCE_EVIDENCE`：只能做模型、失败路径和合成测试；生产适配不得猜测接口。
- `BLOCKED_ACCOUNT_PERMISSION`：来源存在但当前账号权限不足。
- `NOT_IMPLEMENTED`：代码路径尚不存在。

当前明确阻塞的生产范围：个人课表、考试、课程成绩、作业上传/草稿/正式提交、图书馆续借执行、模型图片传输、亲友来访报备。图书馆/体育的既有办理适配保留合成测试和真实事项授权边界，不因本模块改变。

## 验证

```sh
python contracts/v0.2/sources/validate.py
python -m pytest tests/test_source_contracts.py
```

校验器检查来源登记、字段映射、分页规则、条件适配表、10 个规范模型、7 组合成夹具和公开输出脱敏；它不访问任何校园站点。

## 变更规则

1. 先新增来源任务或更新来源行，再改规范模型；不得用规范字段反推不存在的上游字段。
2. 任何新增真实来源证据只提交字段清单、结构说明、合成样例和摘要哈希；原始页面、Cookie、请求头、账号和个人记录留在使用者控制的环境。
3. 稳定标识只由来源、账号域、学期/用途和来源记录号组成；标题、数组位置和显示顺序不参与。
4. 缺少稳定编号时输出 `null` 和 `MISSING_STABLE_ID`，不得合成 ID 或按标题合并。
5. 来源失败、真实空列表和部分读取分别保留；HTTP 200、登录页、目录命中和模型回答都不等于业务成功。
6. 阻塞项解除时新增证据版本并记录变更原因，不覆盖旧结论；旧适配仍按原版本可回退。
