# 性能测试

性能测试一次运行全部用例，无需选择模型或性能档位。`--mode performance` 只运行性能，`--mode all` 执行功能与性能；功能失败不阻断性能。normal/complete 只影响功能范围。

```bash
cp examples/performance.json local-performance.json
# 修改存储端点、区域、桶等目标信息后执行
python3 -m osv run local-performance.json --mode performance --output results/performance-001
```

默认参数可直接使用，以下参数仅用于按需调整负载。旧版 `performance_model` 字段已移除；旧配置删除该字段即可运行全部用例。

## 大小对象读写及元数据操作

每轮依次执行多对象 PUT/GET、Range、LIST、HEAD、逐对象 DELETE。默认 100 个 128 KiB 小对象，以及总计 1 GiB、每个 4 MiB 的 256 个独立数据块对象。`workload.objbench` 用于调整这些数据量，详细字段见 [测试范围](test-scope.md#性能范围)。这些是独立对象，分片上传由后续用例单独测量。

## 单对象重复访问

默认对同一个 Key 分别重复 PUT、GET、Range、HEAD、LIST，样本使用 `repeat_put`、`repeat_get`、`repeat_range`、`repeat_head`、`repeat_list` 名称，避免与多对象结果混合。大小使用 `object_size` 和 `range_size`；正式轮次与请求数使用 `rounds` 和 `requests`；预热请求数使用 `warmup_requests`。插件可通过 `operations` 添加自己的重复操作；该字段不会关闭任何内置用例。

## 小对象、连续与随机读取

从 [统一配置示例](../examples/performance.json) 开始，修改真实 S3 目标。

| `workload.read` 参数 | 默认值 | 含义 |
|---|---|---|
| `small_object_sizes` | `[4096, 65536, 131072]` | 每种大小单独统计完整 GET，单位字节 |
| `objects` | `8` | 每个普通读取场景的独立对象数；hot 场景只使用一个对象 |
| `object_size` | `67108864` | Range 来源对象大小（64 MiB） |
| `range_sizes` | `[65536, 1048576, 4194304]` | 64 KiB、1 MiB、4 MiB 各自统计 |
| `patterns` | `["sequential", "random", "hot"]` | 连续、随机、同一对象的并发随机读取 |
| `warmup_rounds` | `0` | 独立预热轮数，不计入正式统计；示例显式设为 1 |

每种大小/模式每轮执行 `workload.requests` 次（默认 1000），不是每个线程各执行 1000 次。
`workload.concurrency` 控制客户端数量（默认 4），`workload.rounds` 控制正式轮数（默认 8）。
连续读取按对象轮转，每个对象按连续窗口推进，末尾读取剩余字节，再从零开始；随机位置由 seed 固定。
hot 使用同一个 Key，其余模式在多个独立 Key 间轮转。每次下载与原始输入文件的对应字节比较长度和完整 SHA-256；错误只记录摘要，避免大对象内容进入报告。
数据生成和初始 PUT 位于读取测量外；吞吐包含请求分配、下载、进程通信和内容校验。

阶段命名如 `read_small_4096`、`read_sequential_65536`、`read_random_1048576`、`read_hot_4194304`。
各阶段独立统计，避免不同大小/访问模式混在同一分位数里。原始样本保留 Key、offset、length、轮次和结果。

## 并发分片上传

从 [统一配置示例](../examples/performance.json) 开始。实际调用 CreateMultipartUpload → UploadPart → CompleteMultipartUpload。

| `workload.multipart` 参数 | 默认值 | 含义 |
|---|---|---|
| `object_sizes` | `[33554432, 134217728]` | 32 MiB、128 MiB 两档完整对象 |
| `part_sizes` | `[5242880, 16777216]` | 5 MiB、16 MiB 两档分片，末片可以更小 |
| `objects` | `16` | 每个对象大小/分片大小组合上传的对象数 |
| `warmup_rounds` | `0` | 独立预热轮数；示例显式设为 1 |

遍历对象大小与分片大小的笛卡尔积。每组最多同时上传 `min(objects, concurrency)` 个对象；
所有对象共用一个并发分片任务池，真正同时发往存储的请求数不超过 `workload.concurrency`。
`requests` 不控制分片数或对象数；分片数由对象大小与分片大小决定。正式轮数使用 `workload.rounds`。
每个 lane 对应独立 SDK 进程和客户端，不声明共享客户端/连接池争用已经测量。

分别输出 `multipart_create_<对象字节数>_<分片字节数>`、`multipart_part_...`、`multipart_complete_...` 和 `multipart_total_...`。

- create/part/complete 仅报告 SDK 延迟与成功/失败次数，不计算吞吐；不同对象的阶段会交错，不能把阶段的首尾包络当作独立吞吐窗口。
- total 的延迟使用创建上传到完成合并的 wall time，包括排队和分片调度，不包含输入生成、读回校验和清理。
- total 的 `latency_scope` 为 `upload_wall`，`sdk_mean_ms` 为 null，避免把流程耗时误称 SDK 单请求耗时。
- 上传吞吐使用整个组合的实际上传 wall time，包含创建、排队、分片调度与完成合并；数据生成、读回校验和清理位于测量外。所有对象上传结束后才开始读回，避免校验请求争用上传客户端。
- 总流程每组每轮的样本数等于 `objects`（默认 16），不是分片数；比较预算的 `min_samples` 按对应阶段的实际样本数计算。
- 每个对象使用独立内容，完成后真实 GET，校验长度及完整 SHA-256。未通过校验的对象和其分片不会计入已校验上传量。
- 任意分片/完成失败后取消已知 upload_id；清理失败写入 execution_errors。单请求失败和整个流程失败分别保留，不能将多层统计简单相加当成独立业务请求数。

## 边界与结果

读取与分片用例单个输入/分片不超过 256 MiB；每个列表最多 8 个互异大小；每组数据集不超过 2 GiB；对象数 1–64、预热轮数 0–10。
Multipart 分片参数至少 5 MiB，末片按实际剩余大小上传。Range 不超过来源对象大小。
所有参数先校验再启动 SDK。缺少所需驱动操作时明确报告 performance_setup 错误，继续执行该目标支持的其他用例；不以 reference 替代。

报告提供 p50/p95/p99、成功/失败次数、已校验 MiB/s 和操作数/s。预热样本保留但排除在 summary 外。
读取与分片用例可与同语言 S3/native 性能预算比较复用；比较要求两侧场景、存储及网络条件匹配。
这些用例可表达不同的 S3 访问形状，实际对象分布、读写比例和并发度由使用方配置。

## 框架与真实 SDK 验证

```bash
python3 -m unittest discover -s tests -p test_io_bench.py -v
# 按构建指南准备真实 Java/C++ SDK 和本地 Moto 测试依赖后
OSV_RUN_SDK_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_s3_sdk.SdkIntegrationTest.test_read_patterns_and_multipart_performance_use_real_sdks -v
```

单元回归使用真实文件存储生产者与消费者，包含短尾窗口、随机位置、内容损坏、错误 ETag、失败取消及清理。
SDK 回归通过真实 Java/C++ AWS SDK 发请求，读取与分片最终结果都进入生产校验和报告。

### 计时与轮次

多对象用例在计时外准备输入文件和预期摘要；读取计时保留返回数据的长度与 SHA-256 校验，避免每次请求重建完整随机数据。所有目标按同一个用例交错执行，正式轮次轮换目标顺序；不会同时压测不同目标。

报告同时给出汇总吞吐、逐轮最小/最大吞吐和 CV（标准差/均值）；JSON 保留每轮耗时、吞吐和成功样本数。CV 不是置信区间。短批次和少量样本仍可能波动，不能只凭汇总比值判定 SDK 优劣；少于 100 个成功样本的 p99 仅作样本描述。

默认及性能示例使用 8 个正式轮次、每种读取/重复操作 1000 次请求；分片默认每组 16 个对象，最大组合为 2 GiB。没有新增性能档位或模型配置，一次仍执行全部用例。
