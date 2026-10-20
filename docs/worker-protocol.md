# SDK worker protocol v1

Worker 是长驻进程：标准输入、标准输出使用一行一个 JSON，日志必须写 stderr。
每个并发 lane 一个独立进程和 SDK client。协议传输及进程启动不计入 `elapsed_ns`；
计时包含 SDK 操作、本地 payload 文件读取/响应落盘，不是纯服务端延迟。

认证继承环境：`OSV_ACCESS_KEY`、`OSV_SECRET_KEY`、可选 `OSV_SESSION_TOKEN`。
非敏感配置：`OSV_ENDPOINT`、`OSV_BUCKET`、`OSV_REGION`、`OSV_PATH_STYLE`、
`OSV_RETRIES`、`OSV_CONNECTIONS`、可选 `OSV_CNAME`。禁止将凭据放进 JSON manifest 或命令参数。

首条请求 `{"op":"info"}` 返回 `ok`、language、backend、SDK 信息。
语言名称是开放字符串；新增语言无需修改调度器或报告器。身份必须来自实际驱动，不能改标签冒充。
新驱动应同时声明 `protocol_version: 1` 和 `capabilities: ["put", "get", ...]`。
能力名对应 worker 请求的 `op`；Range 由 `get` 的 offset/length 表达。
历史 v1 驱动省略能力列表时，仅按本文冻结的九个基础操作处理，不能推断其支持新增操作。
未知协议版本拒绝启动。所选场景缺少能力时产生失败证据，不会静默跳过或算通过。
新增功能应使用新的操作名及可选字段；改变既有字段语义需要升级协议版本。
后续所有响应必须返回 `ok: bool` 和 `elapsed_ns: 非负整数`。
失败必须返回 `error_kind` 与 `error`；找不到对象使用 `not_found`，不得把无权限转换为不存在。

| Operation | Request fields | Response fields besides ok/elapsed_ns |
|---|---|---|
| put | key, body_path | — |
| get | key, output_path; optional offset,length | —，实际响应必须完整写到 output_path |
| head | key | size；新驱动另返回 key、last_modified_ms、etag |
| list | prefix | keys，遍历全部分页 |
| list_page | prefix；可选 delimiter、marker、token、max_keys、encoding_type | keys、objects:[{key,size,last_modified_ms,etag}]、common_prefixes、truncated、next_token；SDK 能观测时返回实际 prefix/delimiter/max_keys |
| bucket_create | —（使用配置桶） | 缺失则创建、已有则复用；重复调用成功，不删除桶 |
| delete | key | — |
| mp_create | key | upload_id |
| mp_part | key,upload_id,part_number,body_path | etag |
| mp_complete | key,upload_id,parts:[{part_number,etag}] | — |
| mp_abort | key,upload_id | — |

配置中的 SDK 名称与版本由配置者声明，`worker_info` 是运行时补充证据。发布报告前必须核对
真实构建版本及依赖，不得仅修改标签把 Go、C 或本地 reference 结果标成 Java/C++。

自定义 worker 可用于复用既有项目的生产存储接口，但必须真实调用生产代码；本项目自带的
独立 SDK worker 不代表业务引擎完整链路已通过。

C++ 调用 OBS C SDK 时，`language=cpp` 描述调用端，`sdk_language=c` 描述实际 SDK。
`worker_info` 保留 SDK 版本、源码提交、鉴权模式和不可配置的重试策略；不得把 C SDK 标为厂商 C++ SDK。

服务端错误尽可能保留 `error_code`，SDK 暴露 HTTP 状态时另返回 `status_code`；不得由本地类型错误、
无效 JSON、流错误或不支持操作制造服务端错误码。未知信息不能根据请求值伪造。
OBS C SDK 的 list callback 不提供回显 prefix/delimiter/max_keys，这些字段保持缺失。

分块并发兼容检查使用独立的真实 worker 会话，不能把多个请求同时写进一个无关联 ID 的 JSONL 通道。
worker 提前退出时记录经过凭据脱敏、长度受限的 stderr，避免把初始化错误隐藏成一个无原因的失败。

OBS Java 原生链路要求 SDK 初始化后仍为 OBS 签名；IP、path-style 或 CNAME 导致的 AWS V2 回退必须报错。
TOS C++ 提前拒绝上游 SDK 不接受的 IP、显式端口和 S3 端点。诊断保留在脱敏的初始化错误中。
SDK 的 stdout 日志重定向到 stderr，协议 stdout 只包含 JSONL，避免 SDK 日志破坏帧。

## 条件读取、元数据和复制

新增能力必须在握手中明确声明，旧 worker 不能忽略参数后假装支持：

| 操作 | 请求 | 返回 |
|---|---|---|
| get_conditional | key/output_path；可选 if_match、if_none_match、if_modified_since_ms、if_unmodified_since_ms；可组合 offset/length | 实际读取结果或 SDK 的真实拒绝；日期单位为 epoch 毫秒，HTTP 精度为秒 |
| put_metadata | key/body_path、content_type、metadata 字符串映射 | 实际上传结果 |
| head_metadata / get_metadata | key，GET 另需 output_path | 实际 content_type、metadata、size、etag、last_modified_ms；GET 读取完整字节 |
| copy / copy_replace | key 为目的键，source_key；可选 source_bucket；replace 另需 metadata/content_type | 实际复制结果的 etag；跨桶适用性尚未完整验证 |

JuiceFS warning 偏好结果作为 observation，并在 `report.json` 的 evidence 中保存 `source_matches` 等信息；不能当作来源断言通过。
COS Java 显式开启 SDK 的 throw304Directly/throw412Directly，避免默认返回 null 丢失真实服务端异常。
TOS C++ SDK 2.6.30 的 CopyObject 虽有 Content-Type setter，却未将其写入请求；适配器通过 SDK 的公开 request-header API 设置该头，保留为已知适配行为。
TOS Java CopySource 使用 form 风格的空格编码；复制源测试区分空格的 + 与原始加号的 %2B，并检查只解码一次。

## HTTP Range 与对象标签扩展

`get_http` 接受可选的 `range_header`，原样交给实际 SDK；成功结果包含实际 `status_code` 和键名小写的 `http_headers`。
不得根据是否请求 Range 推断状态。未获取到实际状态则不能宣称该能力。
当前支持 Java S3/OSS/OBS/TOS、C++ S3/TOS/COS，其余链路明确缺失。

`tags_put` 接受 `tags: [{"key":"...","value":"..."}]`；`tags_get` 返回同形数组；`tags_delete` 删除标签。
成功结果保留真实 HTTP 状态和响应头，失败保留 SDK 错误码与状态；驱动不预先模拟服务端数量或长度限制。
目前接入 Java/C++ AWS SDK，原生驱动仍待接入。

`put_http` 接受 `body_path` 及可选 `cache_control`/`expires_ms`，返回实际 ETag、HTTP 状态和响应头。
`head_http` 返回实际大小、HTTP 状态和响应头。两项当前接入 Java/C++ AWS SDK；不能由本地计算出的 MD5 替代服务端 ETag。


## 独立桶扩展

Java/C++ AWS worker 接受可选 `bucket`，缺省使用 manifest 的 `OSV_BUCKET`，不会改变会话默认值。
`bucket_create_raw` 是实际 CreateBucket，接受可选 `location`；`bucket_head`、`bucket_delete`、`bucket_location` 分别调用实际 SDK 对应操作。
成功保留真实 HTTP 状态/响应头，location 返回 SDK 实际位置约束（默认区域可能是 null）。
这与正常档的 `bucket_create`（确保配置桶存在）是不同操作。
声明这些新能力的驱动必须同时在对象操作中正确处理逐请求 bucket，不能把独立桶请求路由到默认桶。


## 精确 LIST 观察扩展

`list_v1_page` 与 `list_v2_observed` 当前由 Java/C++ AWS worker 提供，使用实际 SDK 方法、单次请求且不自动翻页。
两者接受可选 `bucket`/`prefix`/`delimiter`/`max_keys`/`encoding_type`。
V1 接受 `marker`；V2 接受 `start_after`/`token`/`fetch_owner`。省略和空字符串原样交给 SDK。
返回 `keys`/`objects`/`common_prefixes`、实际 `prefix`/`delimiter`/`max_keys`/`truncated`，以及已观测标准字段名数组 `fields`。
V1 另返回 `marker`/`next_marker`，V2 另返回 `token`/`next_token`/`start_after`/`key_count`；字段缺失以 null 和 fields 中无该名称表示。
对象 `owner` 仅在 SDK 实际解码了 Owner 时出现，不能按 fetch_owner 请求值生成或删除。

C++ 观察实际 XML 字段有无后复位原流，SDK 仍负责数据解码；Java 保留 SDK 模型暴露的字段和 URL 解码结果。
不重编码 SDK 值来迎合上游断言。当前固定 SDK 的 FetchOwner=false 序列化，Java 为 false、C++ 为 0；合同测试记录实际请求。


## 批量删除扩展

Java/C++ AWS worker 的 `delete_many` 接受 `objects: [{"key":"...","version_id":"可选"}]` 和可选 `quiet`。
`delete_many_versions` 是相同真实 SDK 操作的版本能力入口；版本字段原样交给 SDK。
返回实际 `deleted`、`errors`、`errors_present`、HTTP 状态与响应头。
删除条目可含 `key`/`version_id`/`delete_marker`/`delete_marker_version_id`，错误条目可含 `key`/`version_id`/`code`/`message`。
这些值来自 SDK；不按输入制造确认，也不把单对象失败藏在整体 HTTP 200 中。
一次调用对应一次完整 SDK DeleteObjects，不做自动拆分或桥接层数量拒绝。
文件参考驱动仅为工具自检实现基础非版本删除；版本请求明确拒绝且不声明版本能力。

`mp_create_metadata` 使用实际 SDK 创建分块上传，可选 `content_type`/`metadata`；返回真实 `upload_id`。`mp_complete_observed` 接收调用者原始 `parts` 顺序，返回实际 `etag`、`status_code`、`http_headers`；不排序或合成 ETag。`mp_complete_empty` 省略完成请求体，用于原始 Ceph MalformedXML 场景。`mp_abort_raw` 保留实际 NoSuchUpload 等错误，不能转换为幂等成功。以上精确能力目前为 AWS Java/C++ 链路；其余 worker 不宣称支持。

`mp_copy`：`key` 为目的键，`source_bucket`/`source_key` 为未编码的原始源对象，`upload_id`/`part_number` 为真实上传标识；可选原始 `range_header`。调用 SDK UploadPartCopy，返回服务端 `etag`/HTTP 字段，不通过 GET/PUT 模拟。C++ 由 SDK 序列化 CopySource，不能再次编码。版本化分块复制尚未接入 C++。

`mp_list`：单页真实 ListMultipartUploads，支持 `prefix`、`delimiter`、`key_marker`、`upload_id_marker`、`max_uploads`；返回实际 `uploads`（键、ID、时间及 SDK 暴露的 owner/initiator）、`common_prefixes`、截断状态与两个后续标记。

`get_part` / `head_part`：调用 SDK 的 PartNumber 参数，输入 `key`/`part_number`，GET 另需 `output_path`；返回 SDK 实际 `parts_count`、`etag`、`size`，保留缺失值和服务错误。以上能力当前为 AWS Java/C++。

版本控制操作（目前 AWS Java/C++）：`versioning_get` 返回服务端存在的 `versioning_status`/`mfa_delete`；`versioning_put` 发送 `status` 及可选 `mfa_delete`。`put_version` 读取 `body_path` 上传，`get_version`/`head_version` 接收可选 `version_id`，`delete_version` 可按实际 ID 删除或创建删除标记；只返回服务端暴露的 `version_id`/`delete_marker`，不从请求推测结果。GET 另需 `output_path`。`versions_list` 是单页 ListObjectVersions，可选 `prefix`/`delimiter`/`key_marker`/`version_id_marker`/`max_keys`，返回 SDK 顺序的 `versions`/`delete_markers`、实际字段有无、截断状态及两个后续标记。成功与失败 HTTP 响应头均保留。分块完成也返回实际存在的版本 ID。

`api.<Operation>` 提供独立的配置模型调用命名空间，例如 `api.PutBucketCors`。`input` 使用大小写敏感的 API 成员名和嵌套 JSON 结构；作用域请求会写入本次独立桶，冲突的 `input.Bucket` 被拒绝。已接入标签、CORS、生命周期、策略、网站、默认加密共 18 个 API；缺少实现的 worker 不声明这些能力。

Java 使用 SDK 公开 SdkField 描述及 builder/getter；C++ 根据 `drivers/schema/bucket-api.json` 生成真实 SDK setter/getter 调用。时间戳输入接受 UTC 毫秒或 ISO 时间，输出为实际 SDK 的毫秒值。未知成员、SDK 整数不可表示的输入在发送前明确报错，不生成 HTTP 状态。显式 null 可选成员按 SDK 的未设置值处理。响应中缺失集合保留 SDK 模型的缺失语义；C++ 顶层字段有无从实际响应 XML/头观察，值仍来自 SDK。策略字符串使用 SDK 的实际请求/响应流。

模型描述来源是固定 SDK 接口元数据，不是 AWS 官方测试代码。更新模型后执行 `python3 scripts/generate_bucket_api.py`，并用 `--check` 验证生成物。常规 get/put 性能操作继续直接调用已有 SDK 方法，不经过配置模型转换。


## 签名前请求头修改

AWS S3 Java/C++ 声明 `wire_headers` 能力。兼容性 RPC 可附 `"wire_headers":{"Content-MD5":"bad-value","Expect":null}`：字符串覆盖、null 删除；名称按 HTTP token 校验，禁止 CR/LF、非字符串值和大小写重复名称。在序列化之后、官方签名之前修改；签名器和 HTTP 层仍可正规化/重写，报告保留实际结果。不接受事后伪造 HTTP 错误，也不实现替代签名器。未附该字段的普通性能请求不经过修改器。

负 ContentLength 的源场景附 `checksum_when_required:true`，要求握手声明相应能力。`put_http.content_length` 是有符号长度，交给真实 SDK 模型而非仅修改文本头。`OSV_HEADER_REQUEST_TIMEOUT_MS` 指兼容性客户端总超时，默认 30000ms，握手记录 `header_request_timeout_ms`。请求总超时与 C++ 低速超时分别设置；JSONL 输入使用私有管道副本，SDK 默认 stdin 只读到 EOF，以免上传意外消费控制消息。以上均不修改普通客户端连接/重试设置。
