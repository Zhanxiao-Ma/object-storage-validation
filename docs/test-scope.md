# 测试档位、覆盖目录与报告

## 分层与执行

借鉴 JuiceFS objbench：通用场景 → 统一对象操作 → 存储适配器。
本工具的统一场景用 Python 编排，经 JSONL 操作协议调用 Java/C++ SDK worker。
新增语言实现协议；新增场景编写一次即可供各 SDK 复用；新增操作才扩展各驱动。

用例设计参考 objbench 与 Ceph s3-tests，自行实现语言无关的输入、步骤和预期。
不执行或依赖这两个项目的测试程序，不把上游 Go/Python 的运行结果算作 Java/C++ 证据。
来源是场景参考，不表示逐字段复现上游用例。当前目录版本为 4；上游提交固定，逐项实现映射见运行生成的 `alignment.json`。

## 正常档 normal（默认）

`core_objects` 的 14 项，加 `objbench_objects` 的 10 项：共 24 项，其中 22 个门槛、2 个观察。

| 用例 ID | 断言 |
|---|---|
| object_roundtrip | PUT → HEAD/GET，大小与完整内容准确 |
| empty_roundtrip | 空对象写入、元数据与读取正确 |
| missing_get / missing_head | 不存在对象失败，归一化错误为 not_found |
| range_first / range_last / range_middle | 首字节、末字节及中间片段等于独立输入的对应字节 |
| delete_existing / delete_missing | 删除幂等；随后 GET/HEAD 不存在，LIST 无对象 |
| list_prefix | 精确键集合、无重复，相邻前缀不泄漏 |
| special_key | 中文、空格、加号、百分号及 URL 保留字符读写和列举正确 |
| multipart_complete | 两块不同数据完成后拼接内容及大小正确 |
| multipart_abort | **先上传分块再取消**，HEAD/LIST 均无已提交对象 |
| range_end_overflow | 记录拒绝或正确截断；observation，不作统一门槛 |

新增 objbench 对应检查：

| 新增用例 ID | 对齐内容 |
|---|---|
| bucket_create_idempotent | 配置的桶不存在则创建、已存在则复用，重复执行不报错 |
| hello_partial_ranges | 原版 hello 的 h/o/llo/ll 四组范围 |
| list_object_fields | 列举实际返回的键、大小、修改时间，时间窗口 30 秒 |
| list_after_marker / list_sorted_100 | marker 超过最后键返回空；100 个键按字典序完整列举 |
| special_control_key | 原版中文、JSON 字符、U+001F 控制字符及百分号键，URL 编码列举无损 |
| large_object_put | 默认 256 MiB 单对象上传及大小验证；缩小 large_object_size 时明确记录 |
| slash_suffixed_key | 斜线结尾对象的上传、读取和列举 |
| multipart_grow_shrink | 三个独立真实 SDK 会话并发上传；首块加倍、末块改为 hello 后完成并完整校验 |

原版的 POSIX owner/mode/mtime 功能与对应性能阶段对对象 SDK 不适用，逐项保留 N/A 原因。
bucket_create 针对 manifest 指定的桶，不创建随机测试桶也不删除桶；云凭据需要相应权限。

## 完善档 complete

包含正常档、`extended_objects` 的 18 项、`ceph_object_boundaries` 的 39 项、条件读取 8 项、元数据 4 项和同桶复制 9 项，总计 102 项（98 个门槛、4 个观察）。Ceph 全量迁移尚未完成。

| 新增用例 ID | 断言 |
|---|---|
| overwrite_shorter / overwrite_longer / overwrite_empty | 覆盖后 GET/HEAD 完全反映新数据，不保留旧尾部 |
| delete_recreate | 同一键删除后重建，读取新内容 |
| range_full / range_exact_tail | 整对象 Range 与精确末端范围内容正确 |
| range_start_overflow / range_empty_object | 记录起点越界及空对象 Range 的实际行为；observation |
| key_xml / key_nested | XML 保留字符及嵌套形态键名可准确读写和列举 |
| list_many | 默认 1001 个目标键，验证汇总键集合完整、无重复且前缀隔离 |
| multipart_single | 单块完成后内容与大小正确 |
| multipart_resend | 同编号重传使用最后写入的数据与真实返回的 ETag |
| multipart_out_of_order | 逆序上传，按编号完成，最终内容顺序正确 |
| multipart_invisible | 未完成上传不能通过 HEAD/LIST 看到对象，完成后内容正确 |
| multipart_overwrite | 完成分块上传后替换已提交旧对象 |
| multipart_abort_preserves | 取消新上传不影响旧对象；本工具补充场景 |
| multipart_bad_etag | 错误 ETag 完成失败，不能发布对象 |

`ceph_object_boundaries` 新增具体 prefix/delimiter/marker/MaxKeys 组合、实际续传令牌遍历、
二进制大小矩阵、严格 InvalidRange，以及分块缺失、错误 ETag、非末块过小。
负面场景要求服务端错误码，能观测 HTTP 状态时也检查；本地驱动类型错误不能算预期服务端拒绝。
SDK 原生 C 回调不暴露的回显字段不会凭请求值伪造，仍保留为逐项对齐缺口。

Ceph 参考函数逐项记录在 `osv/suites.py` 和报告的 coverage.catalog 中。
`list_objects` 为 1001–10000 的整数；达到常见服务端分页阈值不代表已检查原始 token/marker 等所有分页参数。
普通 SDK worker 负责遍历分页，场景验证其最终返回的完整集合。

### 未覆盖范围

“完善档”是本工具的扩展对象语义档位，**不等于 Ceph 全量测试，也不是 S3 认证**。
以下在报告中保留为未覆盖范围，不生成通过结果：

- 桶删除、跨桶和账户管理场景；当前只创建/复用配置桶并使用随机对象前缀。
- 文件系统 chmod/chown/mtime。
- 跨桶复制、UploadPartCopy、批量删除；同桶 CopyObject、元数据和条件 GET 已有具体迁移场景，完整边界仍在继续迁移。
- 版本控制、ACL/Policy、加密、生命周期、对象锁、归档恢复。
- LIST 全部 V1/V2 参数与返回字段组合；已实现一批单页参数、原始元数据与排序检查，尚未全量。
- HTTP 状态和响应头逐字段检查、签名负面用例、故障注入、限流与恢复。

这些功能可通过新增操作、Suite、Profile 扩展。能力缺失记录失败，不能跳过后算通过。
Range 观察项也不证明标准 HTTP 状态正确；成功响应若字节错误仍记录 fail。
所有检查立即验证写后读/列举，没有重试等待一致性；异常是失败证据。

## 选择与扩展

```bash
python3 -m osv check examples/java-oss.json --profile complete
# 输出逐项用例目录，不启动 SDK 或访问云
python3 -m osv catalog examples/java-oss.json --profile complete
python3 -m osv run examples/selftest.json --profile complete --output results/complete-example
```

`workload.profile` 与 `workload.compatibility_suites` 二选一。未指定则使用 normal。
旧配置显式选择 baseline 时仍运行旧套件，报告标记 custom，不冒充新档位。
CLI 的 `--profile` 显式覆盖 manifest 选择。每个场景创建独立数据，不依赖前一个场景成功。
`python3 -m osv alignment MANIFEST --profile complete` 可在不启动 SDK 的情况下输出完整上游映射。

Suite 可以声明逐项 catalog，执行结果必须与目录逐项对应；重复、遗漏或将必测门槛降为 observation 都失败。
Profile 只组合 Suite，不含语言或厂商分支。插件扩展见 [扩展说明](extensions.md)。

## 性能范围

默认 `performance_model=objbench` 对齐多对象负载形态：

1. 100 个 128 KiB 小对象并发 PUT，再 GET 并逐字节散列校验。
2. 1 GiB 数据按 4 MiB 拆成 256 个独立对象并发 PUT/GET；不是单对象 Multipart 性能。
3. 额外执行随机 Range 阶段，用 seed 固定偏移并校验准确字节。
4. 每个有效 lane 一次 LIST，再对全部对象 HEAD、DELETE，最后验证数据集已删除。

`workload.objbench` 可以配置 small_objects、small_object_size、big_object_size、block_size、
list_requests_per_lane、warmup_rounds。块数向上取整，最后一块保持完整块大小；报告记录实际字节数。
有效并发取配置并发、小对象数和块数的最小值；每 lane 独立持久 SDK 进程/client。
读取完整校验，失败数据流不能生成正常的 S3/native 性能比较结论。LIST 同时报告 SDK 调用次数/s 与列举对象数/s。

显式选择 `performance_model=operations` 可保留旧单对象 GET/Range/PUT/HEAD/LIST 模型及插件操作。
兼容性数据在性能前清理，避免扩大 LIST 数据集。性能轮次打乱目标顺序，预热不进入测量统计。
输入文件 IO、进程调度和完整校验开销与 Go objbench 的内存路径不同；不能把数据量/阶段对齐说成计时实现完全相同。

尚未内置：冷读控制、混合读写、Polars 生产轨迹回放、单对象分块吞吐、CPU/RSS 或长期稳定性。
未按 Polars 生产采样校准，不能从合成负载直接推出引擎生产性能。

## 报告

每次在新目录生成 report.md、report.json 与逐项 alignment.json，包含：

1. 调用端语言、SDK 名称/语言、实际版本、重试配置和 worker_info。
2. 档位、目录版本、所选 Suite、逐项来源与预期、明确未覆盖范围。
3. 每个目标计划数与 pass/fail/observation/not run 数量；初始化失败不能冒充用例通过。
4. 逐项兼容性结果、错误与观察行为；目录外的 setup/cleanup 失败另列。
5. 性能成功/总数、P50/P95/P99、有效 MiB/s 与 ops/s、预热计数。
6. 同厂商同语言 S3/native 的吞吐比、P95 比、预算判定或证据不足原因。

JSON 另含运行环境、负载、逐请求原始测量、轮次与墙钟时间。
SDK 延迟包括本地 payload IO 和响应完整消费；吞吐包括调度/校验开销。
失败读取不能进入成功样本。元数据操作用 ops/s，数据操作用 MiB/s；预热不进入统计。

within_budget 仅表示本次观测落在预定预算内，不是统计等价证明。
兼容失败、未确认比较条件、样本不足或缺少匹配数据时为 inconclusive。
本地文件自检与 Moto/HTTP fixture 只验证框架或真实 SDK 的特定请求处理，**不构成任何厂商的实云通过或性能结论**。

## 参考来源

- [JuiceFS objbench 设计与场景](https://github.com/juicedata/juicefs/blob/main/cmd/objbench.go)
- [JuiceFS 对象接口](https://github.com/juicedata/juicefs/blob/main/pkg/object/interface.go)
- [Ceph s3-tests 主测试文件](https://github.com/ceph/s3-tests/blob/master/s3tests/functional/test_s3.py)

本项目未复制上游测试实现，不要求安装其测试环境；覆盖目录里的 reference 是场景映射，而非全量移植声明。

## 固定上游与计数

- JuiceFS：29c60bbbeea410c6c0cb7a49540044e0c099dffb，16 个功能项与 10 个性能项。
- Ceph：5b3987116eca0b4b8fa57d6b10f3db57bab196b6，八个文件，979 个源码定义；4 个同名定义被后续定义覆盖，实际函数数为 975。
- 这些是源码定义/阶段数，不是 pytest 参数展开后的执行数。不能以本工具 case 数量除以上游函数数声称覆盖率。
- Ceph 行引用匹配默认只标 partial；只有逐项核对输入与全部断言、且源码指纹与 `ceph_reviews.json` 完全一致的记录才标 aligned。上游源码变化后回退 partial；实现映射不代表实云通过。
- 本地 Moto 5.1.15 在 MaxKeys=0 时返回 IsTruncated=true，与锁定 Ceph 的 false 断言不符。
  完善档会如实失败并阻止该目标的性能阶段；回归测试专门验证此差异没有被隐藏。这不是任何云厂商的实测结论。

LIST 已迁移普通/下划线数据集的四组分页遍历、纯公共前缀页、斜线结尾对象、替代分隔符（字母、百分号、空格、点、换行）和不存在的前缀/分隔符组合。V1/V2 全部响应断言仍需继续核对，不按名称匹配认定完整对齐。

吞吐量使用各阶段的真实墙钟时间，包含框架生成输入、读回校验及本地文件 I/O，不能解释为纯 SDK 网络吞吐。
样本的 SDK elapsed_ns 不包含 Python 生成/校验及 JSONL 序列化，但包含实际 SDK 调用与驱动文件 I/O。
判断 SDK 差异时须同时查看延迟分位数、吞吐量、CPU/网络瓶颈和实际数据集参数；本地自检不作为性能证据。

## 两个来源的预期差异

已确认的 Range 起点越界差异：JuiceFS `ObjectStorage.Get` 希望成功返回空内容，偏离时上游为 warning；Ceph 的原始 S3 Range 要求 416/InvalidRange。
正常档的 `juicefs_range_start_overflow` 单独记录 `source_expected`、`source_matches`、`source_severity` 和实际错误；它是 observation，不计作符合来源预期的 pass。
完善档的 `ceph_range_invalid_start` 独立检查严格的服务端错误。一次 416 可以同时造成前一项的来源偏好不匹配、后一项的严格语义通过。
绝不使用“返回空或报错均通过”的合并断言。成功响应若返回错误字节，仍作为完整性失败。

另需区分判定级别：JuiceFS 将 multipart 检查中的错误整体降为 warning；本工具把已选择负载所必需的分块内容正确性作为门槛。这是本工具的判定策略，不声称完全复制上游分级。
`bucket_create` 是“缺失则创建、已有则复用”的对象层操作；它会先检查已有桶，不能用该结果代替 Ceph 对原始重复 CreateBucket 返回值的检查。后者仍需独立迁移。

新增条件读取覆盖实际 ETag 的匹配/不匹配、历史/未来修改时间，拒绝必须是可核对的服务端 304/412 或明确错误码。
元数据覆盖普通值、空值、覆盖为空及普通 PUT 清除旧元数据；读回检查真实 HEAD/GET 元数据。
复制覆盖零对象、16 MiB、同桶复制、Content-Type、原地复制拒绝/替换、3 字节与 1 MiB 两组元数据保留/替换、缺失来源对象。
