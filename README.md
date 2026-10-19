# Object Storage Validation

面向分析型工作负载的对象存储兼容性与 SDK 性能对照工具。
用同一配置编排真实 SDK worker 与现有开源压测工具，输出 JSON 原始结果和 Markdown 报告。

## 验证什么

1. 指定 SDK、配置和被测存储上的必需操作是否正确：PUT、HEAD、GET、Range、LIST、DELETE、分块完成与取消。
2. 同厂商、同语言、同存储/网络条件下，S3 与原生 SDK 是否落在事先指定的性能预算内。

这不是“证明所有厂商 S3 完全等价”的工具。未测项、失败和证据不足都必须保留。
默认负载是合成示例，不是任何产品的生产画像；接入业务后需要据真实 API、请求大小、并发与任务链路校准。

## 当前 SDK 驱动

| Language | S3 | Native |
|---|---|---|
| Java | AWS SDK v2 | OSS、OBS、TOS |
| C++ | AWS SDK for C++ | OSS、TOS；OBS 原生尚未实现 |
| Python reference | 无 | 真实本地文件 IO，仅自检框架，不是 S3 模拟器 |

**C++ OBS 原生 SDK 对照尚未实现。**不能用 Java 或 Go 的结果补填 C++ 对比列。
华为原生侧通常使用 OBS C SDK；即使从 C++ 调用，也必须如实标注 SDK 类型。

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
python3 -m osv run local-oss.json --output results/oss-java-001
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
python3 -m osv run local-cpp.json --output results/cpp-s3-001
```

同语言对比可选 `examples/cpp-oss.json` 或 `examples/cpp-tos.json`，修改 endpoint 与实际 SDK 版本。
依赖版本、构建与本地 HTTP 集成测试见 [构建与验证](docs/build-and-test.md)。

build.py 在 Linux 下检查逻辑 CPU 与 MemAvailable，显式限制并行度、CPU affinity、
虚拟地址空间和 I/O 优先级；内存不足、持续换页或 PSI 超阈值会停止编译。不编译整个业务引擎。

## 集成已有开源工具

参考 `examples/external.json`。自行安装上游二进制/环境后，使用参数数组调用，禁止 shell 拼接。

| Tool | 用途 | 证据边界 |
|---|---|---|
| [JuiceFS objbench](https://juicefs.com/docs/zh/community/performance_evaluation_guide/) | S3/原生 Go 后端基础功能与吞吐初筛 | Go 的端到端实现，不是 Java/C++ SDK 结论 |
| [MinIO Warp](https://github.com/minio/warp) | 并发、混合与随机 Range S3 压测 | MinIO Go 客户端，不是 AWS Java/C++ |
| [Ceph s3-tests](https://github.com/ceph/s3-tests) | 筛选协议边界用例 | Boto 用例，不等价于业务生产路径 |

上游 stdout/stderr 独立保存，只作为 supplementary evidence，不自动转换为 SDK 通过结果。
不 vendoring 或改写这些项目源码；它们分别遵循各自许可证。

## 结果与限制

- `report.json`：每条测量、轮次、成功/失败、SDK worker 信息、兼容性断言、对比结论。
- `report.md`：兼容性矩阵、p50/p95/p99、已校验吞吐、S3/native 比值。
- warmup 不进入统计；每轮打乱 target 顺序；Range 偏移由 seed 固定，可重复执行。
- SDK 延迟包含响应体完整消费；有效吞吐包含进程协议与内容校验开销，不能冒充纯 SDK 峰值吞吐。
- 错误、缺少 native 配对、跨语言、样本不足或网络条件未确认时，不给出“等价”结论。
- `within_budget` 是本次观测预算检查，不是统计等价证明；目前未提供置信区间、负载回放、缓存控制、CPU/RSS 分析。
- 每个并发 lane 一个进程，不能等价于引擎内共享 client/thread pool 的性能；需另做生产链路验证。
- 只在随机 `osv/<run-id>/` 前缀读写与清理，不清空整个桶，不创建或删除用户桶。
- 分块完成/取消有功能验证，尚未提供独立分块吞吐压测；复制、批量删除和故障注入尚未内置。
- 本地与模拟器通过不代表 OSS/OBS/TOS 已通过；未提供云凭据前没有实云结论。

扩展真实 SDK 和生产路径见 [worker protocol](docs/worker-protocol.md)。

## License

Apache-2.0。SDK 及外部工具保持各自许可证；本项目不包含内部业务代码、云凭据或生产采样数据。

## 扩展性

语言驱动通过版本化 JSONL 协议接入，不限制为 Java/C++。测试功能通过 `Operation` 与 `Suite`
注册，新增场景无需改调度器；需要新 SDK 操作时再扩展驱动能力。能力缺失产生明确失败证据。
接口、插件示例及兼容策略见 [扩展说明](docs/extensions.md)。

CI 模板保存在 `ci/github-actions.yml`，当前未启用 GitHub Actions；启用方式见构建文档。
