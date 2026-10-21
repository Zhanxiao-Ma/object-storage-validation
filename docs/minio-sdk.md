# MinIO SDK 与 AWS SDK 对比

MinIO 官方 SDK 与 AWS SDK 都通过 S3 协议访问同一个端点。Java、C++ 分别对比，报告中的 API 均为 `s3`。
MinIO 驱动当前提供全部性能用例所需的九种操作，不声明完整功能兼容性覆盖。

| SDK | 固定版本 | 驱动 |
|---|---|---|
| AWS Java | 2.20.162 | 原 Java worker，参数 `s3` |
| MinIO Java | 9.0.3 | `drivers/minio/java/target/minio-worker-0.1.0.jar` |
| AWS C++ | 1.11.675 | `drivers/cpp/build/osv-cpp-s3` |
| MinIO C++ | 1.0.0 | `drivers/minio/cpp/build/osv-cpp-minio` |

## 构建

先按 [构建与验证](build-and-test.md) 准备 AWS 驱动。MinIO Java 使用独立依赖目录，保留 SDK 自身 Manifest 及运行时版本，不与 AWS worker 混合依赖：

```bash
python3 scripts/build.py command -- mvn -B -T '{jobs}' -f drivers/minio/java/pom.xml package
```

MinIO Java 的 Maven 项目显式依赖 JVM 版 `okhttp-jvm`。运行 JAR 时保留旁边的 `target/lib` 目录。

MinIO C++ 使用 [官方 SDK 发布包](https://github.com/minio/minio-cpp/releases/tag/v1.0.0) 或同版本的自行构建安装目录。解压对应平台的 Release SDK 后：

```bash
python3 scripts/build.py command -- cmake -S drivers/minio/cpp -B drivers/minio/cpp/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/path/to/minio-cpp-1.0.0-linux-amd64
python3 scripts/build.py command -- cmake --build drivers/minio/cpp/build --parallel '{jobs}'
```

## 测量

查看 [三次独立复测报告](examples/minio-sdk-performance-repeated.md)：共 157,800 条正式样本，包含每次和每轮结果、清理异常；[首次报告](examples/minio-sdk-performance.md) 保留初始测量明细。

从 [四种 SDK 配置示例](../examples/minio-sdk-performance.json) 开始，设置同一个端点、桶、区域与寻址方式，凭据从环境变量继承。Java 两侧使用相同 JVM 参数；同一轮只测一个目标，轮次之间打乱目标顺序。

```bash
python3 -m osv run local-minio-sdk.json --mode performance --output results/minio-sdk-comparison
```

一次执行全部性能用例，分别查看各 SDK 的吞吐和 p50/p95/p99 延迟。比较同一语言、同一用例和参数，不能将跨语言差异归因于 SDK。
MinIO 与 AWS 都使用 S3；现有 `comparisons` 字段面向 S3/厂商原生协议，因此本示例保留四个目标的独立结果，不伪造原生协议身份。

两侧重试均要求为 0，TLS 验证保持默认开启。Java MinIO 配置连接池；C++ MinIO 连接复用由 SDK 默认实现管理，每个 worker 同时只发一个同步请求。
PUT 与 UploadPart 都使用公开的单请求 API，完整读取消费实际响应流。MinIO 两种驱动使用公开的内存数据 API，适配层采用整块文件读取；AWS 使用文件流，计时均包含实际输入/输出文件 IO。Java 9.0.3 低层文件输入路径在本地验证中出现 EOF，因此使用其公开字节数组 API。C++ 部分成功模型不保留 HTTP 状态，适配层按 SDK Result 判断，不构造状态码。

## 回归

安装本地 Moto 测试依赖并构建两种 MinIO worker 后执行：

```bash
OSV_RUN_MINIO_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_minio_sdk -v
```

回归执行真实 SDK 和生产性能调度、校验、报告，覆盖空对象、特殊 Key、Range 首末边界、完整分片读回、错误 ETag 拒绝与取消。
