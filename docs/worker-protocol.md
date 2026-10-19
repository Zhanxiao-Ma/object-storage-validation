# SDK worker protocol v1

Worker 是长驻进程：标准输入、标准输出使用一行一个 JSON，日志必须写 stderr。
每个并发 lane 一个独立进程和 SDK client。协议传输及进程启动不计入 `elapsed_ns`；
计时包含 SDK 操作、本地 payload 文件读取/响应落盘，不是纯服务端延迟。

认证继承环境：`OSV_ACCESS_KEY`、`OSV_SECRET_KEY`、可选 `OSV_SESSION_TOKEN`。
非敏感配置：`OSV_ENDPOINT`、`OSV_BUCKET`、`OSV_REGION`、`OSV_PATH_STYLE`、
`OSV_RETRIES`、`OSV_CONNECTIONS`。禁止将凭据放进 JSON manifest 或命令参数。

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
| head | key | size |
| list | prefix | keys，遍历全部分页 |
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
