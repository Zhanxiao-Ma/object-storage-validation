# SDK 配置

按厂商配置 S3 与原生 SDK 的端点、版本和运行库。基础用法见 [README](../README.md)。

## 已有桶与对象权限

功能用例默认使用 `workload.object_scope="prefix"`，在配置桶的独有前缀下验证对象操作。
没有建桶、删桶或桶配置权限时，设置 `workload.bucket_management=false`；仅真正测试桶状态的用例会因此未执行。
前缀模式保留真实 SDK 响应键、分页 token 和版本 ID，预期键名使用对应物理前缀；清理只删除本次前缀内的资源。
已有桶启用版本控制时会清理该前缀的实际历史版本，不改变版本控制开关。
读取桶版本配置不是对象用例的必需权限；读取被拒绝时，先检查真实对象 HEAD；缺少版本 ID 无法区分未启用与暂停状态，需保守地列举并清理本次前缀的历史版本。版本控制启用、暂停或无法确认状态时，需要相应的历史版本列举与删除权限；无法确认清理时会明确报错。
分片用例除创建、上传和完成外，还需要 `AbortMultipartUpload` 与 `ListMultipartUploads`，用于中止失败请求并核实残留；普通 PUT/GET/DELETE 成功不代表这两项权限已具备。
需要不同桶或精确根键名长度的原始用例保留前提条件，不会以单桶前缀测试冒充跨桶或根键边界通过。
专用桶环境可使用 `object_scope="bucket"` 保留原始每用例独立建桶的方式。

## S3 代理与请求校验

Java、C++ S3 驱动均支持显式 `OSV_HTTP_PROXY=http://host:port`，HTTPS 请求通过该 HTTP 代理建立隧道。Java Select 的独立异步客户端也使用此代理和连接、读写超时配置。
代理地址不接受用户名、密码、路径、查询或 fragment；报告中的 `explicit_http_proxy` 记录是否启用。
不要仅凭 shell 的 `HTTPS_PROXY` 已设置就认为这两个固定版本 SDK 已使用代理。

C++ 可用 `OSV_REQUEST_CHECKSUM_CALCULATION=when_required` 仅自动附加协议要求的请求校验；默认值为 `when_supported`。
显式指定算法的校验用例仍发送其原始校验要求。报告记录该配置，便于区分存储能力与 SDK 默认请求编码差异。

## Java SDK 对照

```bash
python3 scripts/build.py java
# 在当前 shell 从自己的凭据管理方式导出 OSV_ACCESS_KEY、OSV_SECRET_KEY。
cp examples/java-oss.json local-oss.json
# 编辑 local-oss.json 的 endpoint、region、bucket、负载；确认同存储与网络后才设置
# same_storage_and_network_confirmed=true。示例中的 0.90/1.10 是演示预算，需先按业务要求制定。
python3 -m osv check local-oss.json
python3 -m osv run local-oss.json --mode performance --output results/oss-java-001
```

OBS/TOS 可沿用示例，把原生 worker 最后一个参数分别改成 `obs` / `tos`，同步 provider、SDK 版本和 endpoint。
TOS S3 与原生 endpoint 不同，不能混用；各侧必须保持同一区域、网络类别、桶与存储类型。
Java 默认 AWS SDK 版本可通过 Maven `-Daws.version=...` 覆盖；报告版本标签也必须同步。

## C++ SDK 验证

安装 AWS SDK for C++（s3 组件）及 nlohmann_json 3.11+ 后：

```bash
python3 scripts/build.py cpp --prefix /path/to/sdk/install
# 可选原生驱动：增加 --oss-source /path/to/oss-sdk --tos-source /path/to/tos-sdk
cp examples/cpp-s3.json local-cpp.json
python3 -m osv run local-cpp.json --mode performance --output results/cpp-s3-001
```

同语言对比可选 `examples/cpp-oss.json` 或 `examples/cpp-tos.json`，修改 endpoint 与实际 SDK 版本。
依赖版本、构建与本地 HTTP 集成测试见 [构建与验证](build-and-test.md)。

build.py 在 Linux 下检查逻辑 CPU 与 MemAvailable，显式限制并行度、CPU affinity、
虚拟地址空间和 I/O 优先级；内存不足、持续换页或 PSI 超阈值会停止编译。不编译整个业务引擎。

## 腾讯云 COS 对照

提供 `examples/java-cos.json` 与 `examples/cpp-cos.json`，分别比较同语言 AWS S3 SDK 和 COS 原生 SDK。
Java 使用官方 `com.qcloud:cos_api:5.6.246`，C++ 使用官方 `cos-cpp-sdk-v5`（验证版本 `v5.5.23`）。

COS 的 `OSV_BUCKET` 必须包含 APPID，例如 `examplebucket-1250000000`。
示例使用同一桶的两种 endpoint：

- S3 驱动：`https://cos.ap-guangzhou.myqcloud.com`，由 AWS SDK 添加桶名，`OSV_PATH_STYLE=false`。
- COS 原生驱动：完整桶域名 `https://examplebucket-1250000000.cos.ap-guangzhou.myqcloud.com`。

替换示例桶名、地域及对应域名；凭据仍继承 `OSV_ACCESS_KEY` / `OSV_SECRET_KEY` / 可选 `OSV_SESSION_TOKEN`。
两个 endpoint 指向同一个桶并不意味着配置已被工具自动核实；确认网络、存储类型后再启用对比条件。
S3 endpoint 与虚拟主机形式参考 [腾讯云官方配置说明](https://cloud.tencent.cn/document/product/436/41284)。

C++ COS 的构建步骤及本地 HTTP 契约测试见 [构建与验证](build-and-test.md)。
整对象读取保留 SDK 默认校验；C++ Range 读取按 SDK 要求关闭与整对象 ETag 的 MD5 比较，
框架仍独立验证所请求字节，校验失败不能进入成功样本。
C++ COS SDK 的连接池参数控制每 host 最大空闲连接数，区别于其他 SDK 的总连接上限，报告中单独注明。
当前每 lane 串行操作，不使用该参数推断整个负载的并发数。

## 华为 OBS：C++ 调用官方 C SDK

`examples/cpp-obs.json` 对比同一 C++ 负载下的 AWS SDK for C++ 与官方 OBS C SDK。
驱动通过流式回调完成读写、Range、HEAD、LIST、DELETE 和分块操作；不重写签名或用 S3 替代原生请求。
固定使用 `OBS_OBS_TYPE`，拒绝 Path Style，避免 C SDK 自动使用 S3 鉴权。

当前验证源码的 SDK 版本为 `3.26.6`，版本及源码提交记录在 `worker_info`。
此 C SDK 没有公开的重试次数设置；原生目标必须使用 `OSV_RETRIES=sdk-default`，不能声称重试已关闭。
S3 侧的重试次数仍按配置生效。比较报告需保留该差异，不能据此推断相同故障重试条件下的性能。

官方 C SDK 及其依赖独立构建，通过 `--obs-source` / `--obs-library` 接入，步骤见 [构建与验证](build-and-test.md)。
原生目标的 `LD_LIBRARY_PATH` 应单独配置为 OBS 运行库目录，避免替换 S3 侧的网络/加密依赖。
