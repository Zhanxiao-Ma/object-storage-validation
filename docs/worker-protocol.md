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
