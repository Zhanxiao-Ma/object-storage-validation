# Object Storage Validation

[![CI](https://github.com/Zhanxiao-Ma/object-storage-validation/actions/workflows/check.yml/badge.svg)](https://github.com/Zhanxiao-Ma/object-storage-validation/actions/workflows/check.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](LICENSE)

通过真实 Java / C++ SDK，测试对象存储的 **S3 功能兼容性、数据正确性和读写性能**。
支持独立执行功能测试、性能测量，以及同厂商 S3 SDK 与原生 SDK 的性能对照。

[快速开始](#快速开始) · [测试范围](#测试范围) · [报告示例](docs/examples/rustfs-normal.md) · [构建指南](docs/build-and-test.md) · [SDK 配置](docs/sdk-configuration.md)

## 主要功能

- **统一功能用例**：Java、C++ 及不同存储使用相同场景和预期；普通档 96 项，通用完整档 780 项。
- **检查最终结果**：验证读回字节、元数据、复制结果、分页以及删除后的对象状态；保留实际 HTTP 状态和 SDK 错误。
- **独立性能测量**：支持多对象负载、大小对象读写、Range、HEAD、LIST 和 DELETE，输出分位延迟与已校验吞吐。
- **SDK 性能对照**：支持同厂商、同语言的 S3/native 对比，自定义吞吐与 p95 延迟预算。
- **可追溯报告**：Markdown 展示失败、警告和具体未执行原因；JSON 保留测量、运行时 SDK 信息与原始证据。
- **可扩展驱动**：通过版本化 JSONL 协议接入 SDK，通过操作、测试组和档位注册新用例。

## 快速开始

需要 Python 3.10+。从源码运行框架仅依赖 Python 标准库；真实存储测试还需要构建相应 SDK 驱动。

```bash
git clone https://github.com/Zhanxiao-Ma/object-storage-validation.git
cd object-storage-validation

# 本地文件 IO 自检，无需云凭据或 SDK
python3 -m osv check examples/selftest.json
python3 -m osv run examples/selftest.json --output results/selftest-001
```

打开 `results/selftest-001/report.md` 查看报告。每次运行使用新的输出目录。
本地 reference 驱动用于验证工具本身，未实现的 S3 能力会明确显示为 `not_run`。

### 测试一个 S3 端点

先按 [构建指南](docs/build-and-test.md) 安装 JDK 17、Maven 并构建 Java 驱动：

```bash
python3 scripts/build.py java
cp examples/java-s3.json local-s3.json
```

修改 `local-s3.json` 中的 `provider`、`OSV_ENDPOINT`、`OSV_REGION`、`OSV_BUCKET` 和寻址方式。
凭据由当前 shell 的 `OSV_ACCESS_KEY`、`OSV_SECRET_KEY`、可选 `OSV_SESSION_TOKEN` 传入。

```bash
python3 -m osv check local-s3.json

# 普通档：96 项常用 S3 功能检查
python3 -m osv run local-s3.json --profile normal --output results/s3-normal-001

# 完整档：780 项通用 S3 检查
python3 -m osv run local-s3.json --profile complete --output results/s3-complete-001

# 只查看用例目录，不启动 SDK 或访问存储
python3 -m osv catalog local-s3.json --profile complete
```

使用专用测试桶。基础检查可能创建配置中尚不存在的桶；桶级及高级场景会创建独立测试桶，并清理本次创建的资源。
相应权限、第二身份、KMS 等前置条件不足时，会在报告中列明具体原因。

C++ 从 [examples/cpp-s3.json](examples/cpp-s3.json) 开始，构建及运行库设置见 [构建指南](docs/build-and-test.md)。

### 单独测量性能

```bash
# 仅性能，不要求先通过功能测试
python3 -m osv run local-s3.json --mode performance --output results/s3-performance-001

# 同一次运行执行功能与性能；功能失败不阻断性能阶段
python3 -m osv run local-s3.json --mode all --profile normal --output results/s3-all-001
```

默认执行模式为 `functional`。CLI 的 `--mode`、`--profile` 覆盖 manifest 中的对应配置。
执行性能测试会自动运行全部性能用例，无需选择模型或档位。
对象大小、并发度、轮次、预热及随机种子有默认值，可按需调整。

## 性能测试

| 用例 | 测量内容 |
|---|---|
| 大小对象读写 | 多对象 PUT、GET、Range、LIST、HEAD、逐对象 DELETE |
| 单对象重复访问 | 对同一对象重复 PUT、GET、Range、HEAD、LIST |
| 不同读取方式 | 多档小对象完整读取，多档 Range 连续/随机读取，以及同一对象并发读取 |
| 并发分片上传 | 不同对象与分片大小，多个对象及分片并发上传，分别统计创建、分片、完成和整个上传流程 |

```bash
cp examples/performance.json local-performance.json
# 修改 endpoint、region、bucket 等实际参数后，一次运行全部性能用例
python3 -m osv run local-performance.json --mode performance --output results/performance-001
```

读取校验真实返回字节；分片完成后完整读回，内容一致才计入已校验上传量，失败时取消未完成上传。
每个并发通道使用独立 SDK 客户端。分片单请求延迟与整个上传流程耗时分别记录。
详细参数、默认负载和吞吐口径见 [性能测试说明](docs/performance.md)。

## 测试范围

| 档位 | 用例数 | 覆盖内容 |
|---|---:|---|
| **`normal`**（默认） | **96** | 读写、Range、复制、特殊 Key、批量及静默删除、桶、V2 分页、分片及未完成分片生命周期配置 |
| **`complete`** | **780** | 包含普通档，扩展版本控制、条件请求、权限、加密、对象锁、生命周期及访问日志配置等通用 S3 场景 |

`normal` 是 `complete` 的严格子集，复用相同实现和断言。通用两档不执行 Ceph/RGW 专有协议。
生命周期和访问日志的标准配置测试检查配置写入、读回、更新及删除/停用；不据此认定后台对象处理或日志交付已完成。

功能设计参考 [JuiceFS objbench](https://github.com/juicedata/juicefs/blob/29c60bbbeea410c6c0cb7a49540044e0c099dffb/cmd/objbench.go)
与 [Ceph s3-tests](https://github.com/ceph/s3-tests/tree/5b3987116eca0b4b8fa57d6b10f3db57bab196b6)，使用固定源码版本追溯场景。
本工具通过自身驱动发出真实 SDK 请求。逐项范围、前置条件和迁移缺口见 [测试范围](docs/test-scope.md)。

## 支持的 SDK

| 调用语言 | S3 驱动 | 原生 SDK 驱动 |
|---|---|---|
| Java | AWS SDK for Java v2 | 阿里云 OSS、华为 OBS、火山引擎 TOS、腾讯云 COS |
| C++ | AWS SDK for C++ | 阿里云 OSS、火山引擎 TOS、腾讯云 COS、华为 OBS C SDK |
| Python | 本地文件 reference，用于框架自检 | — |

当前固定 AWS SDK 默认版本：Java **2.20.162**；C++ 开发验证版本 **1.11.675**。
报告记录运行时实际 SDK 版本与重试配置；修改 SDK 时需同步依赖、manifest 版本并重新验证。
OBS 的 C++ 驱动调用官方 C SDK，报告分别记录调用语言和 SDK 语言。
原生 SDK 的可用操作以驱动握手为准，缺少操作不会计作通过。

同语言 S3/native 对照示例：
[Java OSS](examples/java-oss.json) · [Java OBS](examples/java-obs.json) · [Java TOS](examples/java-tos.json) · [Java COS](examples/java-cos.json) ·
[C++ OSS](examples/cpp-oss.json) · [C++ OBS](examples/cpp-obs.json) · [C++ TOS](examples/cpp-tos.json) · [C++ COS](examples/cpp-cos.json)。
端点、运行库、重试等厂商差异见 [SDK 配置](docs/sdk-configuration.md)。

## 如何阅读报告

查看 [真实 RustFS 普通档报告](docs/examples/rustfs-normal.md)：使用 Java/C++ AWS SDK 执行 96 项用例，报告由工具直接生成。

| 文件 | 内容 |
|---|---|
| `report.md` | 运行概览、驱动配置、各目标汇总、失败与警告、未执行原因、可展开的完整明细及性能结果 |
| `report.json` | 结构化功能结果、测量样本、SDK 身份、执行错误与性能预算对比 |
| `alignment.json` | 执行功能时生成：固定上游场景的实现映射与未覆盖项 |

| 功能状态 | 含义 |
|---|---|
| `pass` | 本条用例的断言通过 |
| `fail` | 请求失败或实际结果不符合用例预期，需进一步定位服务端、SDK 或请求适配原因 |
| `warning` | 用例定义的行为警告，保留实际响应与差异 |
| `not_run` | 未执行，报告列出缺失操作、配置或未通过的前置探测 |

性能统计排除预热；失败请求单独计数，不计入成功吞吐和有效字节。
SDK 延迟包含响应体完整消费，已校验吞吐包含调度与数据校验开销。
同语言 S3/native 对比需明确确认相同存储、存储类别、区域和网络，并事先配置性能预算。
`within_budget` / `outside_budget` 表示观测结果是否满足预算；`inconclusive` 表示当前条件或样本不足以比较。

退出码：`0` 表示本次无失败；`1` 表示功能失败、执行错误或性能预算未满足；`2` 表示配置/命令错误。
`warning`、`not_run` 不单独令命令失败，应同时查看报告中的执行范围。

## 开发与文档

```bash
python3 -m unittest discover -s tests -v
```

真实 SDK 集成测试需要先构建驱动，并显式启用对应测试开关，详见 [构建与验证](docs/build-and-test.md)。
构建入口会检查 CPU、可用内存、换页和内存压力，限制并行度并降低 CPU/I/O 优先级。
GitHub Actions 在 push 与 pull request 时执行已配置的框架、构建及 SDK 集成检查。

- [构建与验证](docs/build-and-test.md)：依赖、固定版本、资源限制及集成测试。
- [本地 MinIO HTTPS 测试](docs/build-and-test.md#本地-minio-https-测试)：临时证书、真实 SDK 与 SSE-C 验证。
- [性能测试](docs/performance.md)：读取/分片矩阵、计时和数据校验。
- [测试范围](docs/test-scope.md)：逐项范围、负载参数、前置条件和覆盖边界。
- [SDK 配置](docs/sdk-configuration.md)：各厂商端点、寻址、重试与动态库配置。
- [驱动协议](docs/worker-protocol.md)：版本化 JSONL 请求、响应和能力声明。
- [扩展说明](docs/extensions.md)：新增语言驱动、操作、测试组和档位。

## License

[Apache-2.0](LICENSE)。SDK 和参考项目的许可证信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
