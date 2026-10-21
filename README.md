# Object Storage Validation

面向分析型工作负载的对象存储兼容性与 SDK 性能对照工具。
参考 JuiceFS objbench 的分层设计，用统一用例调用真实 Java/C++ SDK，输出 JSON 原始结果和 Markdown 报告。

## 验证什么

1. 指定 SDK、配置和被测存储上的必需操作是否正确：PUT、HEAD、GET、Range、LIST、DELETE、分块完成与取消。
2. 同厂商、同语言、同存储/网络条件下，S3 与原生 SDK 是否落在事先指定的性能预算内。

这不是“证明所有厂商 S3 完全等价”的工具。未测项、失败和证据不足都必须保留。
默认负载是合成示例，不是任何产品的生产画像；接入业务后需要据真实 API、请求大小、并发与任务链路校准。

## 当前 SDK 驱动

| Language | S3 | Native |
|---|---|---|
| Java | AWS SDK v2 | OSS、OBS、TOS、腾讯云 COS |
| C++ | AWS SDK for C++ | OSS、TOS、腾讯云 COS、华为 OBS C SDK |
| Python reference | 无 | 真实本地文件 IO，仅自检框架，不是 S3 模拟器 |

C++ 的 OBS 原生驱动链接官方 OBS C SDK，报告分别记录调用端语言 `cpp` 与 SDK 语言 `c`。
不能用 Java 或 Go 的结果补填 C++ 对比列。

## 快速自检：不访问云

Python 3.10+，运行时仅标准库。先进入本仓库根目录。

```bash
python3 -m osv check examples/selftest.json
python3 -m osv run examples/selftest.json --output results/selftest-001
python3 -m unittest discover -s tests -v
```

输出目录必须是新目录，避免覆盖已有证据。reference 测试只证明调度、协议、内容断言、清理和报告工作正常。

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
依赖版本、构建与本地 HTTP 集成测试见 [构建与验证](docs/build-and-test.md)。

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

C++ COS 的构建步骤及本地 HTTP 契约测试见 [构建与验证](docs/build-and-test.md)。
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

官方 C SDK 及其依赖独立构建，通过 `--obs-source` / `--obs-library` 接入，步骤见 [构建与验证](docs/build-and-test.md)。
原生目标的 `LD_LIBRARY_PATH` 应单独配置为 OBS 运行库目录，避免替换 S3 侧的网络/加密依赖。

## 独立执行功能与性能测试

```bash
# 默认仅执行功能测试；档位只控制功能用例范围
python3 -m osv run examples/selftest.json --mode functional --profile normal --output results/functional-001
python3 -m osv run examples/selftest.json --mode functional --profile complete --output results/complete-001
# 仅性能：不执行功能用例，不要求已有功能报告
python3 -m osv run examples/selftest.json --mode performance --output results/performance-001
# 同一次运行分别执行两部分；功能失败或跳过不阻止性能测量与 SDK 对比
python3 -m osv run examples/selftest.json --mode all --output results/all-001
# 只查看目录，不访问 SDK 或存储
python3 -m osv catalog examples/java-oss.json --profile complete
```

`workload.mode` 可设置 `functional`、`performance` 或 `all`；默认 `functional`。
CLI `--mode` 优先于 manifest。旧配置未指定 mode 时，现在只跑功能；原组合行为需显式指定 `all`。
功能和性能使用独立对象前缀，分别创建输入和清理；性能准备只检查其自身所需的驱动能力。

功能结果为 `pass`、`fail`、`warning`、`not_run`。保留来源断言、warning 和跳过原因，
不再提供“必过项/观察项”或 `gate` 分类。`warning` 仅用于来源本身的警告，不能将明确断言降级。
例如 JuiceFS 分块检查及部分 Range 偏好保留 warning；Ceph 对应严格错误码检查仍独立断言。

性能请求记录 SDK 错误或内容校验失败；失败请求计入 attempts/failures，不计入有效字节和成功吞吐。
是否接受功能覆盖、厂商差异和性能结果，由调用方决定。组合执行结束时，退出码汇总本次实际失败，
不会提前阻止另一部分；`not_run` 和来源 warning 单独报告，不冒充通过，也不单独令 CLI 失败。
启动、准备、清理等执行错误写入 `execution_errors`。退出码 1 表示实际失败、执行错误或调用方配置的性能预算未满足，
退出码 2 表示配置/命令错误；功能模式不计算性能比较。外部命令仅在显式配置后执行，mode 不替外部程序选择其内部阶段。

## 测试档位

| 档位 | 当前功能用例数 | 参考范围 |
|---|---:|---|
| `normal` 正常档（默认） | 24 | JuiceFS objbench 对象检查与额外基础检查 |
| `complete` 完善档 | 1047，包含正常档 | 已迁移 Ceph 场景，包括高级能力及其显式前置条件 |

档位只选择功能用例，Java/C++ 与每个厂商共享用例和预期；不执行上游测试程序。
完善档不等于 Ceph 全量覆盖；当前 953/975 个适用源函数已对齐，22 个 SigV2 相关源函数仍未完成。
用例数量与源函数数并非一一对应。真实云兼容性以实际执行报告为准。
逐项目录、来源、性能口径与未覆盖项见 [测试范围](docs/test-scope.md)。

manifest 可设置 `workload.profile`；原有 `workload.compatibility_suites` 保留为自选模式，不能同时设置。
CLI `--profile` 显式覆盖 manifest 中的两种选择。默认性能模型为 `objbench`，使用多对象数据集；可显式选择 `workload.performance_model=operations` 保留单对象操作模型。
负载大小、并发与轮次仍可配置，参数缩放写入报告。完善档列举用例默认写入 1001 个对象，可配置 `list_objects` 为 1001–10000。

历史外部命令入口保留用于用户自定义辅助任务（`examples/external.json`），不是档位的实现或执行依赖。

## 结果与限制

- `report.json`：每条测量、轮次、成功/失败、SDK worker 信息、兼容性断言、对比结论。执行功能时另生成 `alignment.json`，逐项列出锁定上游的实现映射和缺口。
- `report.md`：兼容性矩阵、p50/p95/p99、已校验吞吐、S3/native 比值。
- warmup 不进入统计；每轮打乱 target 顺序；Range 偏移由 seed 固定，可重复执行。
- SDK 延迟包含响应体完整消费；有效吞吐包含进程协议与内容校验开销，不能冒充纯 SDK 峰值吞吐。
- 错误、缺少 native 配对、跨语言、样本不足或网络条件未确认时，不给出“等价”结论。
- `within_budget` 是本次观测预算检查，不是统计等价证明；目前未提供置信区间、负载回放、缓存控制、CPU/RSS 分析。
- 每个并发 lane 一个进程，不能等价于引擎内共享 client/thread pool 的性能；需另做生产链路验证。
- 只在随机 `osv/<run-id>/` 前缀读写与清理，不清空整个桶，不创建或删除用户桶。
- 已有拆块对象的并发性能阶段；它是多个独立对象，不是单对象 Multipart 吞吐。复制、批量删除和故障注入尚未内置。
- 本地与模拟器通过不代表 OSS/OBS/TOS 已通过；未提供云凭据前没有实云结论。

扩展真实 SDK 和生产路径见 [worker protocol](docs/worker-protocol.md)。

## License

Apache-2.0。SDK 及外部工具保持各自许可证；本项目不包含内部业务代码、云凭据或生产采样数据。

## 扩展性

语言驱动通过版本化 JSONL 协议接入，不限制为 Java/C++。测试功能通过 `Operation`、`Suite` 与 `Profile`
注册，新增场景无需改调度器；需要新 SDK 操作时再扩展驱动能力。能力缺失产生明确失败证据。
接口、插件示例及兼容策略见 [扩展说明](docs/extensions.md)。

GitHub Actions 工作流位于 `.github/workflows/check.yml`，在 push 与 pull request 时执行框架测试、Java 构建/COS 契约测试及 OBS C SDK/C++ 驱动构建与契约测试。

实际测试范围、与 objbench/s3-tests 的覆盖差异和报告口径见 [测试范围与报告](docs/test-scope.md)。

## 对齐基准与多对象性能配置

基准固定为 JuiceFS `29c60bbbeea410c6c0cb7a49540044e0c099dffb`、Ceph s3-tests
`5b3987116eca0b4b8fa57d6b10f3db57bab196b6`，避免用上游移动分支冒充固定范围。

```bash
python3 -m osv alignment examples/java-oss.json --profile complete
```

Ceph 清单包含八个测试文件、979 个定义，其中 4 个同名定义被后续定义覆盖，实际函数数为 975；
这不是 pytest 参数展开后的执行数。当前 Ceph 仍有未迁移项，`complete` 档位名称不代表迁移已完成。

默认 `objbench` 性能模型为 100 个 128 KiB 小对象、1 GiB 数据按 4 MiB 拆为独立对象、每 lane 一次列举。
依次执行 smallput/smallget/put/get、额外 Range、LIST/HEAD/DELETE，完整校验读取内容和最终删除。
可通过 `workload.objbench` 设置 `small_objects`、`small_object_size`、`block_size`、`big_object_size`、
`list_requests_per_lane` 和 `warmup_rounds`。向上取整生成完整块，实际数据量、有效并发和参数均进入报告。
正常档功能检查默认另上传 256 MiB 单对象，可设置 `large_object_size` 缩小自检，报告明确标记缩放。

`examples/selftest.json` 使用缩小参数，只用于框架自检。`bucket_create` 对配置的桶执行“缺失则创建、已有则复用”，不删除桶。

## 用例设计参考

功能用例设计参考 [JuiceFS objbench](https://github.com/juicedata/juicefs/blob/main/cmd/objbench.go)
和 [Ceph s3-tests](https://github.com/ceph/s3-tests)。工具通过自身的 Java/C++ 驱动执行用例。
面向阅读的 `report.md` 使用中性的功能名称，展示预期、实际结果与差异，不展示来源项目名称或迁移进度。
`report.json` 和 `alignment.json` 保留机器标识及来源映射，便于问题定位和开发审计。
