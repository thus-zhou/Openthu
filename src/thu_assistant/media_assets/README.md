# media_assets：附件与图片的本地隐私处理（3.3）

本包是 v0.2 唯一的本地素材边界。多模态输入（4.5）、课程资料（4.2）和业务门面（6.1）
只能通过这里登记、解析和取用素材；不得另开一条绕过检查的本地读取路径，也不得接受任意
图片 URL 参数。

## 入口

```python
from thu_assistant.media_assets import MediaAssets

assets = MediaAssets(temp_root)          # 受控临时目录；必须显式给出
ref = assets.register_user_file(
    path, account_scope=scope, task_id=task, purpose="USER_REVIEW",
    source_evidence_id="local.user_selected",
)
descriptor = ref.to_reference()          # media-reference-v1，可直接进公开结果
data = assets.read_bytes(ref.media_id, account_scope=scope, task_id=task)
```

- `register_user_file` / `register_downloaded_attachment` / `register_synthetic_file`
  分别对应“用户明确选定”“已授权下载的课程附件”“公开合成夹具”三种来源，调用者必须
  明确选择，不存在自动推断。
- `manifest()` 返回可检查的素材清单：asset_id、account_scope、purpose、MIME、字节数、
  哈希、过期时间、来源与转换说明；不含绝对路径。
- `source_info()` 给资料模块提供来源字段（source_evidence_id、源哈希、display_name）。
- `prepare_model_bundle()` → `authorize_model_send()` → `consume_model_bundle()`
  是唯一的外发路径：先预览，再用本次请求的一次性授权取字节。

## 文件边界

读取前逐项检查：普通文件、非符号链接/重解析点、当前用户可读、非空、大小在限、打开后
`fstat` 身份一致；读取量有界，文件在读取过程中变长会被拒绝。路径字段里出现 URL、`data:`
或 NUL 字节直接失败——路径与远程地址不共用字段。

类型由内容签名决定：PNG/JPEG/WebP/PDF/ZIP/GZIP/JSON/纯文本。扩展名或浏览器声明的 MIME
与内容不一致即拒绝；可执行文件、HTML/SVG/脚本无论叫什么名字都拒绝。`MZ` 只在 DOS 头的
`e_lfanew` 指向合理 PE 偏移时才视为可执行文件，`BM` 不再作为魔数，避免普通文本被误伤。`.docx/.pptx/.xlsx`
按 ZIP 容器识别，深层 OOXML 结构不做业务校验（本模块只存储，不执行）。

## 图片处理

- 限制来自 `contracts/v0.2/media-reference.schema.json`：单图 8 MiB、每次 4 张、
  合计 16 MiB、单图 1600 万像素；平台契约更严格时取较小值（`MediaLimits.stricter`）。
- 另有 20000 边长上限，防止“像素数勉强合规、形状异常”的解码开销；这是工程限制，不是
  学校规定。
- 解码前后都检查像素与边长；Pillow 的进程级炸弹阈值保持不变，只在本模块调用内局部
  抑制其警告，拒绝原因由显式限制决定。
- 通过检查的图片一律按方向校正后重新编码：EXIF、GPS、XMP、ICC、注释和容器尾随数据全部
  丢弃；派生结果重新打开复核，残留元数据属于内部失败。原文件不覆盖。JPEG 重新编码会写入
  默认 JFIF 密度标记（1×1，编码器结构默认值，不来自源文件）。
- 引用记录派生内容的哈希；原文件哈希单列在 `source_sha256`，避免把原图哈希标到派生内容上。
- 动图（多帧 WebP/APNG）不接受：只发首帧会歪曲内容。
- 图片不能以 `ATTACHMENT` 登记（会返回 `MEDIA_TYPE_UNSUPPORTED`）：课程资料里的图片必须用
  `kind="IMAGE"` 走重编码路径，这样元数据才会被清除。
- PNG 重编码先用 `compress_level=6`，超过单图上限时再试 9；JPEG 依次尝试质量 90/80/70/60。
- 派生文件写入受控目录（POSIX 0700/0600），经 `.part` + 原子改名；磁盘满、取消或超时
  都会删除半成品且不登记引用（取消发生在派生写入之后也会回滚该文件）。
- 读取阶段的 I/O 失败统一映射为 `MEDIA_FILE_UNREADABLE`，不会把裸 `OSError` 抛给调用方。

## 引用与授权

引用绑定 `account_scope`（2.2 的账号伪名，必须是 `acct_...`；由 `SecureStore.account_scope()` 派生，
2.2 表示“未绑定账号”的 `none` 会被拒绝，不得用它代替账号作用域）、`task_id`、`purpose` 和 `expires_at`
（默认 900 秒）。账号、任务、用途或时限任一不符即拒绝；引用不是下载地址，也不自动
授予另一次请求使用同一文件的权限——新请求必须重新登记。派生文件被替换或改写时，
`read_bytes` 在返回前重新计算哈希并拒绝。

模型外发额外要求：登记时显式 `external_transfer_allowed=True`、用途为 `MODEL_INPUT`、
`prepare_model_bundle` 生成预览（模型、图片数量、总字节、文本长度与截断预览），
`authorize_model_send` 绑定同一 `request_id`，`consume_model_bundle` 只能成功一次。
能力探测是纯函数，不解析引用，因此不会读取或发送真实图片；它的输出必须能通过
`model-image-capability-v1`（`model_id`/`source_id` 模式、`warning` 截断到 300 字符）。
`consume` 在读取前就烧掉授权：读取失败也要重新 `prepare`，这是刻意的失败关闭取舍。

## 清理与加密状态

- **一个受控根目录只能有一个活动实例**：`MediaAssets` 在根目录持有 `.media-lock`
  排他锁，第二个实例（同进程或别的进程）得到 `MEDIA_TEMP_IN_USE`，不会去清理不属于
  自己的文件。需要并行的进程应各用各的根目录，或经由业务门面取字节。
- `cancel_task(task_id)` 撤销该任务的全部引用并删除派生文件；之后的解析返回
  `MEDIA_REFERENCE_REVOKED`。
- `pending_cleanup()` 给出仍持有派生文件的引用清单（asset_id、retention、expires_at、
  derived_present、uses），不含路径，作为未完成清理的明确清单。
- `cleanup()` 删除本模块命名规则内、未被“活动且未过期”引用使用的文件；过期引用不再保护
  其派生文件，`PROJECT_CACHE` 保留项和目录中的外来文件不动。
- 保留期：`SESSION` 在进程退出时删除；`EXPLICIT_DELETE` 与 `PROJECT_CACHE`
  跨进程保留（后者还对常规 `cleanup()` 免疫），由显式清理、取消或调用者删除。
- 进程退出时 `close()`/atexit 做同样的清理。清理是“关闭句柄 + 删除文件”，不宣称对
  所有存储介质完成物理擦除。
- 可选的 `state_store` 使用 2.2 的加密提供者持久化引用表；只保存文件名，不保存绝对
  路径，账号绑定在认证数据里，换账号读取会认证失败。记录 ID 同时绑定账号与受控根目录
  （`state_record_id`），两个安装目录不会互相覆盖。`SESSION` 引用不写入加密状态
  （进程退出即失效），重启后能恢复的是 `EXPLICIT_DELETE`/`PROJECT_CACHE`；恢复时解析
  失败或文件缺失的条目计入 `audit_report()["state_skipped"]`，不再静默吞掉。

## 明确不做

- 不发起任何网络请求；本包不导入 socket/urllib/requests/http。
- 不写普通日志、不打印、不缓存图片字节；错误详情只含稳定的枚举词。
- 不修改原文件、不写入用户下载目录、不删除用户文件。
- 不判断校园业务含义，也不代表学校规则；限制只是 Openthu 的工程约束。
- Windows 上目录保护依赖用户目录 ACL；本模块只拒绝重解析点并检查可写性，不重复实现
  2.2 的 ACL 判定。

## 验证

```sh
python -m pytest -o addopts='' -q tests/test_media_assets.py
```

全部夹具为本地合成：带 GPS 的 PNG、带设备信息的 JPEG、伪装扩展名、截断 JPEG、
声明 20000×20000 的小文件、多帧 WebP、HTML/SVG/可执行文件、PDF/DOCX/Markdown。
