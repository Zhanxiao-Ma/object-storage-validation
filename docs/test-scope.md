# 测试档位、覆盖目录与报告

## 分层与执行

借鉴 JuiceFS objbench：通用场景 → 统一对象操作 → 存储适配器。
本工具的统一场景用 Python 编排，经 JSONL 操作协议调用 Java/C++ SDK worker。
新增语言实现协议；新增场景编写一次即可供各 SDK 复用；新增操作才扩展各驱动。

用例设计参考 objbench 与 Ceph s3-tests，自行实现语言无关的输入、步骤和预期。
不执行或依赖这两个项目的测试程序，不把上游 Go/Python 的运行结果算作 Java/C++ 证据。
来源用于定位场景；只有 alignment.json 中通过源指纹与全部断言审查的 aligned 行表示已完成对应迁移。当前目录版本为 11；上游提交固定，逐项实现映射见运行生成的 `alignment.json`。

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

包含正常档、`extended_objects` 的 18 项、`ceph_object_boundaries` 的 39 项、条件读取 8 项、元数据 4 项、同桶复制 9 项、精确 HTTP Range 6 项、对象标签 8 项和响应字段/对象生命周期 5 项及独立桶 7 项及精确 LIST 74 项及批量删除 4 项及精确分块上传 13 项，分块复制/上传清单 7 项及按分块编号读取 3 项，及版本控制 21 项，及桶配置 24 项及 SSE-C 11 项及桶控制 17 项及 SSE-C 分块 7 项及 SSE-S3/KMS 对象 22 项及多身份策略列举 6 项及 ACL 26 项及桶清单 3 项及原始 V4 请求头 27 项及条件写入/复制 12 项，总计 405 项（401 个门槛、4 个观察）。Ceph 全量迁移尚未完成。

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

### 精确 HTTP Range 与对象标签

`ceph_http_range` 对齐六个源函数：`testcontent` 的闭区间 `4-7`、开放区间 `4-`、后缀 `-7`，
8 MiB 字母数字对象的 `3145728-5242880` 区间，以及普通/空对象上的非法 `40-50` 区间。
正向检查真实响应体、`Content-Range` 和 HTTP 206；负向检查实际 416/InvalidRange。
`get_http` 目前由 Java S3/OSS/OBS/TOS 和 C++ S3/TOS/COS 提供。
Java COS、C++ OSS/OBS 当前不暴露所需的成功 HTTP 状态，相关用例记 `not_run`，不能由请求推断 206。
AWS C++ 使用公开 transport factory 包装官方 Curl 客户端的真实响应，覆盖空响应体；普通性能请求不采集响应头。
TOS C++ 显式使用 `bytes=0-0` 字符串，避免 SDK 将数值 `0,0` 当作未设置 Range。

`ceph_tags` 对齐八个源函数：2/10/11 个标签、128/129 字符键、256/257 字符值、替换和删除。
保留源用例标签顺序、HTTP 200/204 与精确 `InvalidTag` 错误码。
目前标签链路接入 Java/C++ AWS SDK；其余原生驱动尚未接入，记 `not_run`，不表示云服务不支持标签。
Moto 5.1.15 对 11 个标签返回 `BadRequest`，该项如实失败；固定响应合同另验证 SDK 原样保留 `InvalidTag`。
本地文件 reference 只验证工具调度与断言，不能作为云兼容性或真实 HTTP 结果。

`ceph_headers` 新增原版 bar 对象的 HTTP 200 与带引号 MD5 ETag、Cache-Control、Expires（按秒比较），以及零对象 HEAD 和 bar/soup 写读更新删除。
`put_http`/`head_http` 当前接入 Java/C++ AWS SDK，其余原生驱动相关项明确 `not_run`。

### 独立测试桶

`ceph_buckets` 在 Java/C++ AWS SDK 上新增 7 项：独立桶 HEAD、非空桶删除拒绝、创建后删除再删除、
空桶内缺失对象，以及不存在桶的 HEAD/DELETE/LIST V2。
这些用例创建 `osv-<32位 UUID>` 的真实临时桶，使用显式逐请求桶参数；正常档仍使用 manifest 配置桶。
新桶先以实际 HEAD 404 确认不存在，拒绝复用已有名称。断言成功或失败后，都列举并删除自己的对象、删除桶，最后再 HEAD 确认不存在。
JSON 结果记录 `isolated_bucket` 与 `cleanup_confirmed`。原生驱动尚未接入这些桶操作，相关项是 `not_run`。

运行完善档需要凭据允许在该测试命名空间内创建桶、列举、读写删除对象及删除桶。
创建请求遭遇结果不确定的传输错误时，报告保留测试桶名供核查；不会自动处理所有权不确定的桶。
清理失败保留原断言和具体桶名，阻止性能结论。版本控制测试仍未迁移，当前清理器只处理普通对象。

### 独立桶中的精确 LIST

`ceph_exact_listing` 新增 74 项，分别调用真正的 ListObjects V1 和 ListObjectsV2。
保留源数据集、空/省略 Prefix 和 Delimiter、MaxKeys=0/1/默认值、marker/start-after 回显、
V1 NextMarker、V2 token 回显和优先级、KeyCount、Owner 有无、URL 编码及四组 delimiter 分页遍历。
包含原版 1000 个目录键加 4 个特殊后继键的边界；测试数据实际上传，完成后逐项清理。

Java 使用 SDK 模型的字段有无；C++ 通过官方 Curl 客户端的实际响应观察 XML 字段有无，
随后将同一响应流复位交给真正的 SDK 解码。返回值始终来自 SDK getter，不使用请求参数补回响应值。
这使缺失字段、空字符串、SDK 自身的解码表现可以区分。XML 观察仅用于显式 LIST 兼容操作，不用于性能阶段。

Ceph 原始编码/回显等预期保持不变；当前 Java SDK 的 URL 解码及固定 Moto 的响应差异原样记录为失败，
留待后续预期规则阶段处理。固定响应合同检查适配器观测是否准确，不把这些结果解释为云厂商兼容性结论。
逐项映射中的 `reviewed_implementation` 指明实际完成全部源断言核对的用例，相关但未完整迁移的旧用例仍可同时列在 implementations 中。

### 批量删除

`ceph_delete_many` 新增 V1/V2 列举下的重复批量删除，以及一次提交 1001 键的上限场景。
数据先通过真实 PUT 创建；分页计数、Deleted 数量、Errors/Contents 字段有无及实际 HTTP 400 按源预期验证。
SDK 直接收到完整对象数组，桥接层不分批，也不在本地伪造 400。
固定 HTTP 合同确认 Java/C++ 都发送一次包含 1001 个 Object 的请求，并保留混合成功/失败、版本和 Quiet 信息。
另有真实 Java 写入/C++ 删除及反方向的数据流回归。Moto 接受 1001 键的差异保持为失败，不修改源预期。

### 未覆盖范围

“完善档”是本工具的扩展对象语义档位，**不等于 Ceph 全量测试，也不是 S3 认证**。
以下在报告中保留为未覆盖范围，不生成通过结果：

- 桶命名规则、跨桶和账户管理场景；独立桶创建、HEAD、删除及部分错误场景已经迁移，完整组合仍待补齐。
- 文件系统 chmod/chown/mtime。
- 跨桶复制、UploadPartCopy 及批量删除的完整权限/版本/负面组合；同桶复制、元数据、条件 GET 和基础批量删除已有迁移场景。
- 版本控制、ACL/Policy、加密、生命周期、对象锁、归档恢复。
- LIST 全部 V1/V2 参数与返回字段组合；已实现一批单页参数、原始元数据与排序检查，尚未全量。
- Range 以外的大部分 HTTP 状态/响应头逐字段检查、签名负面用例、故障注入、限流与恢复。

这些功能可通过新增操作、Suite、Profile 扩展。已声明逐项能力的套件将缺失能力记为 `not_run`，其它用例继续；必测项未执行会阻止性能结论，不能算通过。
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

## SDK 未提供操作能力时

内置套件逐项检查握手声明的能力。某项缺少操作会保存为 not_run，并列出缺失操作；不会把不支持当作通过，也不会阻止其他独立检查执行。
必测项未执行会阻止该目标的性能测量和有效对比结论，CLI 以非零状态退出。
可选观察项缺失仍计入 Not run，不计入 Pass；它不阻止已通过必测项所支持的性能阶段。
启动失败、性能核心能力缺失或清理能力缺失仍作为 setup 失败，不能继续生成正常性能样本。

### 精确分块上传

`ceph_exact_multipart` 按原始大小执行 13 个 Ceph 场景：30 MiB 上传及重复完成、五组原始分块重传、六种大小、单字节重复完成、内容拼接、覆盖旧对象、取消/不存在的上传、空完成、过小分块、缺失分块和错误 ETag。30 MiB 场景保留 `text/bla`、`foo=bar`，以 1,000,000 和 10,000,000 字节步长逐段读取并核对内容及响应长度；不使用工作负载参数缩小这些输入。

AWS Java/C++ 使用官方 SDK 创建、上传、完成和取消请求；完成 ETag 和 HTTP 状态取真实响应。未完成上传在每条断言路径都执行取消，再清理独立桶。当前 Moto 5.1.15 对空完成/不存在上传的完成返回 500；源用例仍期望 400/MalformedXML 和 404/NoSuchUpload，保持失败结果。文件系统 reference 仅验证调度、断言及清理，不是 S3 服务端证据。

### 服务端分块复制与 PartNumber

`ceph_multipart_copy` 保留跨桶复制、单字节范围、源对象全部复制、四种特殊源键、六种复制大小、越界及六种错误 Range，并列举三个实际上传 ID。复制调用真实 UploadPartCopy，不下载再上传。`ceph_get_part` 保留多分块（5/5/5/1 MiB）、单分块（5 MiB）、普通对象的 PartNumber GET/HEAD，检查完成前 404、实际 PartsCount、整体 ETag、分块长度和字节及越界错误；不能用 Range 请求代替 PartNumber。

精确分块数据保留上游 ASCII 字母、每分块内重复 1 KiB 的形态和原始大小，使用独立固定种子。实际 SDK 清单查询保留两个续传标记和 Owner/Initiator；C++ 的 `uploads=uploads` 与 Java 的空值子资源参数均原样发送。

隔离桶清理同时保留本次真实写入的原始键，以处理 SDK XML 列举丢失空白字符的情况；最终必须实际删除桶并确认 HEAD 404，删除拒绝不能报告已清理。

### 历史版本与删除标记

`ceph_versioning` 新增 21 个原始场景：版本控制启停、六组 5 版本删除遍历、头版本删除、删除标记堆叠、普通 null 版本删除/覆盖、暂停后的 null 覆盖及多版本保留、暂停时跨桶复制、10 版本删除及四种特殊对象名、两个键的版本顺序、批量历史版本/标记删除、原子/30 MiB 分块上传的版本 ID 返回、三次 18 MiB 分块覆盖和三类删除标记响应头。仅对实际创建的独立桶配置版本控制，不更改目标默认桶。

Java/C++ 都调用实际 AWS SDK 的 VersionId、ListObjectVersions、Put/GetBucketVersioning 等 API。SDK 的版本 ID、列表顺序、字段有无和错误响应头保持原样；已有版本和删除标记按实际 ID 清理，不能只删除当前对象。

文件系统 reference 没有实现云端版本控制。这 21 项在本地 reference 上报告 not_run/unsupported_operations，并阻断性能输出及使 CLI 退出 1；不会为获取通过结果而模拟 SDK 版本行为。Java/C++ HTTP 契约与实际 SDK/Moto 测试独立执行；这些本地 HTTP 结果不是厂商云端认证。

当前 Moto 5.1.15/固定 SDK 组合保留了暂停版本历史、批量创建标记字段、分块完成返回 ID 和部分删除标记错误头的失败；C++ 还保留纯空白版本键的 SDK 返回值差异。原始预期不因本地环境失败而改变。

### 桶标签、CORS、生命周期和默认加密

`ceph_bucket_config` 新增 24 个锁定版本场景：桶标签完整生命周期、CORS 创建前/删除后 404 与数组读回、生命周期配置读写删除、生成 ID、原始错误状态/日期/重复或超长 ID、旧 Prefix/现代 Filter/删除标记/非当前版本/分块清理配置，以及 S3/KMS 默认加密的设置、读取、删除。KMS ID 使用 `workload.kms_key_id`；缺省时保留上游 `fool-me-again` 输入。

非当前版本迁移场景要求 `workload.storage_classes` 至少包含三种配置值；缺少时保留源用例的跳过前提，报告 `not_run/missing_prerequisites` 和具体缺项，阻断完整档性能结论。不会臆造云厂商支持的存储类型。文件系统 reference 不实现这些云端配置，新增 24 项连同版本控制共 45 项报告缺能力。

配置由 Java/C++ 官方 SDK 的真实请求模型序列化，响应值取实际 SDK getter。当前固定 C++ SDK 模型不再提供旧 LifecycleRule.Prefix，相关请求保留明确的客户端模型错误；不会删除该字段、改成 Filter 或伪造服务端拒绝。Java 对无效日期的客户端解析失败，以及 Moto 接受超长/重复 ID、小写状态的结果同样保留，不能据此直接判定云端 S3 不兼容。

### 对象 SSE-C 原始请求头

`ceph_sse_customer` 迁移 11 个固定源码场景：1B/1KiB/1MiB/13B 读写、HEAD 无密钥拒绝与带密钥成功、GET 无密钥/错误密钥、错误 MD5、缺 MD5、缺密钥、缺算法。保留原始密钥、MD5、A 字节长度和错误断言；原 helper 注释提到 B 覆盖，但实际函数只有 A 写读，因此按实际函数迁移。

Java 使用公共逐请求 header override，C++ 使用公共 AdditionalCustomHeaders；不调用会自动补 MD5 的 SSE-C 模型参数。实际 HTTP 夹具核对字段缺失/空值、签名 SignedHeaders、错误响应与请求间隔离。只为 AWS S3 worker 声明此能力，尚未实现的原生 worker 和本地参考 worker 显式 not_run。

本地 Moto 下正向传输通过，7 个错误输入场景未按原预期拒绝；两个 SDK 都如实记录失败并清理独立桶。这不是实际云厂商验证结果，不改判定规则。

### 桶控制与 Object Lock 配置

`ceph_bucket_controls` 增加公共访问阻止 4 项、所有权控制 2 项、Object Lock 桶配置 11 项。覆盖四个布尔值读回、未配置错误、删除/幂等删除、精确 GetBucketPublicAccessBlock 拒绝策略、创建时启用 Object Lock、配置 GOVERNANCE/COMPLIANCE、Days/Years 冲突、零/负周期、非法大小写枚举/启用状态、版本控制开启/暂停和配置读回。

Java/C++ 经实际 SDK 模型与传输执行；创建启用头、原始非法值及独立服务响应由 HTTP 合约检查。所有场景使用独立空桶，Object Lock 场景不上传保留对象，最终确认桶已删除。此批不包含对象级保留、法律保留或跨身份 ACL 场景，不用桶配置能力冒充这些覆盖。

本地 Moto/SDK 下每种语言 9 项保留为失败，包括 400/409 差异、未拒绝非法输入、读回额外 Years=0 和策略行为；C++ 策略请求还保留其真实 SDK 解析错误。适用性与预期协调后续单独处理。

### SSE-C 分块与按分块读取

`ceph_sse_multipart` 增加 7 项：原始 30 MiB 对象与 5 MiB 分块、5 MiB+1 非对齐分块、错误分块密钥/MD5、错误下载密钥、加密多分块按编号读取及加密普通对象按编号读取。保留 1,000,000/10,000,000/分块大小±1 的 Range 步长、text/plain、foo=bar、16 MiB 四分块及重发第三块。

各阶段均由实际 Java/C++ SDK 签名和传输，不用本地 Range 替代服务端 PartNumber。源用例完成前及越界多分块读取故意不带 SSE-C 请求头，这一细节也保留。失败时实际 AbortMultipartUpload，删除原始对象键并确认独立桶不存在。

本地 Moto 下两个大对象传输/Range 场景通过；错误分块密钥/MD5、错误下载密钥、HEAD 分块 ETag 和普通对象越界状态形成每种语言 5 项原预期失败，均保留。实际 HTTP 夹具另外覆盖服务端正确拒绝错误分块时的源路径和 Abort 清理。

### SSE-S3/KMS 对象与默认加密

`ceph_encryption_objects` 增加 22 项：AES256 默认/显式加密各 1B/1KiB/1MiB/8MiB、KMS 默认加密相同四个长度、KMS 显式传输 1B/1KiB/1MiB/13B、KMS HEAD/普通读取、SSE-S3 默认 HEAD，以及缺 KMS key、只给 key 未声明加密、普通对象 GET 附带 KMS 声明。保留原始 A 字节、testkey-1/testkey-2 和实际响应头/拒绝断言。

`workload.kms_key_id` 提供有效 KMS 资源标识；缺失时 10 个依赖主 KMS key 的场景明确 not_run，保留缺失要求并阻止性能统计，不填造有效 key。其中 8 项源码显式 skip，HEAD/普通读取 2 项对应其主 KMS 环境前提。错误输入 3 项照原输入执行，不因为没有有效 key 跳过。

Java/C++ 公共逐请求头入口由 `EncryptionHeaders`/同等 C++ helper 统一维护 SSE-C、SSE-S3、KMS 字段；保持原 SSE-C 操作兼容，普通性能操作不走头映射。真实 HTTP 检查签名、缺失/空值、请求隔离及独立响应字段。配置本地测试 key 时两种 SDK 的正向传输均完成；Moto 未拒绝的 5 项原预期失败保留。缺配置时每种语言额外 10 项未执行；均不是实际 KMS 云认证。

### 独立身份与策略列举

AWS Java/C++ worker 支持逐请求 `identity`：`main`、`alt`、`tenant`、`bad_auth`、`anonymous`。每个身份使用单独缓存的官方 SDK 客户端，端点、region、重试和连接配置一致。匿名用官方匿名凭据，不回退主身份。主身份仍使用原环境变量；其他身份分别读取 `OSV_ALT_ACCESS_KEY/SECRET_KEY/SESSION_TOKEN`、`OSV_TENANT_*`、`OSV_BAD_AUTH_*`。私密凭据不进入握手元数据。未配置的身份不声明相应 `identity_*` 能力，直接调用会明确报错。

`ceph_bucket_policy` 迁移 V1/V2 策略列举及跨桶复用策略 4 项，保留原始 asdf/abcd、Principal、Action、资源 ARN 和另一身份列举。跨桶场景直接消费真实 GetBucketPolicy 输出，不自行重建策略。真实 HTTP 检查不同签名凭据/会话令牌、匿名头缺失、回到主身份后的隔离以及策略字节从生产者进入消费者。

缺另一个身份凭据时四项明确 not_run，阻止完善档性能统计。本地配置另一个测试身份时 Java 完成四项，C++ 保留本地服务返回导致的真实 SDK 策略解析错误。该本地检查不认证云端权限行为；ACL、跨租户授权与 IAM/STS/SNS 场景仍按清单显示迁移状态。

### ACL 与桶清单批量迁移

`ceph_acl` 增加 26 项：桶/对象默认与创建时 public-read、private 往返、public-read-write、authenticated-read，五种对象显式权限，组/用户 ID/邮件授权及不存在身份，private 幂等设置、撤销全部授权后恢复，以及另一身份的 READ/READ_ACP/WRITE/WRITE_ACP/FULL_CONTROL 权限向量。SDK 原生模型保留 Grantee 的 xsi:type、Owner、Grants；创建时 ACL 用真实创建请求，ACL 修改直接消费真实读回值。

`workload.identities` 提供独立预期身份元数据，例如 `{"main":{"canonical_id":"configured-main-id","display_name":"configured-main-name"},"alt":{"canonical_id":"configured-alt-id","display_name":"configured-alt-name","email":"configured-alt@example.invalid"}}`。缺失的预期和 SDK 身份能力分别显示明确原因，不能从被测接口读回值构造预期。原 check_grants 的条件排序/字段规则及局限保持，暂不协调上游预期。撤销全部 ACL 和 Deny 策略的场景均在失败路径恢复配置，避免清理被拒绝。

`ceph_bucket_inventory` 增加五桶创建后清单成员、创建时间下界、真正匿名列举共 3 项。独立桶清理不删除其他已存在的桶；匿名结果不筛选。策略组另补 Deny 覆盖 authenticated-read ACL 的 V1/V2 两项，仍要求 403/AccessDenied。

本地 Moto 使用独立配置的固定账户元数据执行全部 ACL：private 幂等/撤销恢复通过，其余 24 项保留缺 DisplayName、权限/列表规则及非法授权接受等诊断。桶成员和创建时间通过，匿名桶数与策略 Deny 不符合原预期的结果保持失败。另有真正 SDK HTTP 合约核验模型、请求头和 Owner/Grants 生产者到消费者链路。以上均不代表云厂商认证。


### 原始 V4 请求头

`ceph_request_headers` 增加 `test_headers.py` 的 27 个通用 V4 场景：Content-MD5 三种非法值和删除，Expect 非标准值/空值/删除，Content-Length 空值/负数/删除，ContentType 文本/空值/未设置，Authorization 空值/删除，Date 与 X-Amz-Date 组合，以及 ACL 非法预置值/删除长度。保留原始空请求体、bar、Bucket-only 创建、后续普通写入和精确 HTTP/错误码断言。负长度通过真实 SDK 的 ContentLength 参数设置；C++ 使用官方 WHEN_REQUIRED 校验和配置，Java 固定版本尚无可选 trailer 默认行为，均有独立 HTTP 合约防止误入另一种编码。

`wire_headers` 在官方 SDK 序列化后、签名前应用；Java 使用 ExecutionInterceptor，C++ 使用委托官方签名器的 provider。SDK/HTTP 客户端随后重写请求头的行为不隐藏：例如删除 Authorization 后官方签名器重新签名，原始 403 预期仍然失败。HTTP 头值为文本，源 Expect 整数 200 表达为文本 200。原生 SDK 未实现该入口时明确 not_run，不伪装成 S3 链路通过。21 个显式 SigV2 场景未迁移，不以 V4 替代。

兼容性客户端单独设置总请求超时（`OSV_HEADER_REQUEST_TIMEOUT_MS`，默认 30000）；普通性能客户端不安装头修改器。C++ JSONL 控制管道与 C stdin 隔离，避免 libcurl 在空请求体/非法长度时默认从控制管道读取，保持官方传输与签名。真实超时/客户端拒绝不会冒充服务端 400。

每个场景确认独立 UUID 桶原先不存在。创建失败但桶出现时，仅在实际 ACL Owner 与另一个创建成功的独立控制桶相同后清理；未知归属、拒绝或不确定 HEAD 保留诊断，不删除未确认桶。该读回值仅用于清理归属，不用于构造功能预期。

本地 Moto 两种语言均保留 13 项原预期失败：MD5/长度未拒绝、SDK 本地拒绝或重写、非法 canned ACL 的本地服务错误；其余 14 项通过，所有已确认桶清理完毕。真实 HTTP 合约另外覆盖头值/删除、签名顺序、校验和、上下文隔离、总超时、RPC 输入隔离和清理归属边界。以上是本地 SDK 链路证据，不是云端兼容性认证。


### 条件写入与条件复制

`ceph_conditional_writes` 增加 8 项条件 PUT 和 4 项条件 CopyObject：If-Match/If-None-Match 的实际 ETag、错误 ETag、星号、对象存在/不存在，以及复制源匹配/不匹配。保留源码 GET 返回的 ETag 去引号与 PUT 返回 ETag 原样传递两种路径，保留 bar/zar、ABCORZ 的引号区别、412/PreconditionFailed、缺对象 404/NoSuchKey 和后续读取全部断言。源 fails_on_aws/rgw 等标记继续显示，不以当前 AWS 语义改写源码。

复用签名前请求头能力，官方 Java/C++ SDK 实际执行 PUT/GET/CopyObject；源响应直接进入条件请求。独立 HTTP 合约验证头值、引号、签名与完整读回，并故意破坏生产 ETag 传递确认测试失败。本地 Moto 两种语言均保留 3 项原预期失败：已存在对象 If-Match 星号、去引号 If-None-Match ETag、复制源 If-Match 不匹配；其余 9 项通过，独立桶全部清理。缺少请求头能力的 SDK 明确 not_run；没有实云结论。
