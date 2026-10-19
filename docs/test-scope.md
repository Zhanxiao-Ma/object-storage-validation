# 测试档位、覆盖目录与报告

## 分层与执行

借鉴 JuiceFS objbench：通用场景 → 统一对象操作 → 存储适配器。
本工具的统一场景用 Python 编排，经 JSONL 操作协议调用 Java/C++ SDK worker。
新增语言实现协议；新增场景编写一次即可供各 SDK 复用；新增操作才扩展各驱动。

用例设计参考 objbench 与 Ceph s3-tests，自行实现语言无关的输入、步骤和预期。
不执行或依赖这两个项目的测试程序，不把上游 Go/Python 的运行结果算作 Java/C++ 证据。
来源是场景参考，不表示逐字段复现上游用例。当前目录版本为 1。

## 正常档 normal（默认）

`core_objects`：14 项，其中 13 个兼容性门槛、1 个行为观察。

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

参考 objbench 的基础对象功能；分块取消另加生命周期检查。
与原版 objbench 的差异：不创建桶，不实现文件系统 owner/mode/mtime；不复刻其所有特殊键与参数组合。

## 完善档 complete

包含正常档，增加 `extended_objects` 的 18 项，总计 32 项（29 个门槛、3 个观察）。

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

Ceph 参考函数逐项记录在 `osv/suites.py` 和报告的 coverage.catalog 中。
`list_objects` 为 1001–10000 的整数；达到常见服务端分页阈值不代表已检查原始 token/marker 等所有分页参数。
普通 SDK worker 负责遍历分页，场景验证其最终返回的完整集合。

### 未覆盖范围

“完善档”是本工具的扩展对象语义档位，**不等于 Ceph 全量测试，也不是 S3 认证**。
以下在报告中保留为未覆盖范围，不生成通过结果：

- 桶创建/删除、账户管理；只使用已有桶内本次随机前缀。
- 文件系统 chmod/chown/mtime。
- CopyObject、UploadPartCopy、批量删除、用户元数据、条件请求。
- 版本控制、ACL/Policy、加密、生命周期、对象锁、归档恢复。
- LIST delimiter/marker/token 原始分页参数、响应元数据与排序保证。
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

Suite 可以声明逐项 catalog，执行结果必须与目录逐项对应；重复、遗漏或将必测门槛降为 observation 都失败。
Profile 只组合 Suite，不含语言或厂商分支。插件扩展见 [扩展说明](extensions.md)。

## 性能范围

档位控制兼容性广度；性能阶段由独立 workload 配置驱动，不与档位绑定压力等级。
当前测量 GET、Range、PUT、HEAD、LIST，可配置对象/Range 大小、并发、请求数、预热、轮次和 seed。
兼容性对象在性能前清理，避免完善档的大量对象改变 LIST 的性能数据集。
性能反复操作同一 perf 对象，PUT 覆盖同一键；Range 使用固定 seed 生成偏移。
每 lane 一个持久 SDK 进程/client，不能代表引擎内共享 client/thread pool 的性能。

尚未内置多对象性能数据集、冷读控制、混合读写、业务轨迹回放、独立分块吞吐、CPU/RSS 或长期稳定性。
也未按 Polars 生产采样校准负载。objbench 多小对象、拆块大对象和删除性能未被本档位复刻。

## 报告

每次在新目录生成 report.md 与 report.json，包含：

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
