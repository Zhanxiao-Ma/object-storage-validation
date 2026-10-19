# 构建与验证

## Java

需要 Linux、Python 3.10+、JDK 17 和 Maven。驱动编译目标为 Java 11。

```bash
python3 scripts/build.py java
```

当前依赖版本在 `drivers/java/pom.xml` 固定：AWS v2 2.20.162、OSS 3.18.1、OBS 3.25.10、TOS 2.9.19、COS 5.6.246。
JAR 将真实依赖版本用于运行时握手，不能只改 manifest 标签。

## C++

需要 C++17 编译器、CMake 3.20+、OpenSSL 与 libcurl 开发包。
以下依赖从各自官方仓库获取，单独保留许可证，不复制到本项目。
开发验证使用的版本：

| Dependency | Version | Source revision |
|---|---|---|
| [AWS SDK for C++](https://github.com/aws/aws-sdk-cpp) | 1.11.675 | c8468a337c1cf457c4f27ee135f2b101d40ca264 |
| [nlohmann/json](https://github.com/nlohmann/json) | 3.11.3 | tag v3.11.3 |
| [OSS C++ SDK](https://github.com/aliyun/aliyun-oss-cpp-sdk) | 1.10.1 | 1106f4c004564e63173b3fde00546a01370d740e |
| [TOS C++ SDK](https://github.com/volcengine/ve-tos-cpp-sdk) | 2.6.30 | 07d81b3c6d9780442d56e0272b2f4a83c7b5559e |

先按上游说明获取 SDK 源码（AWS 需要递归子模块），以下路径均替换为实际路径。
所有本机构建都通过资源监控入口运行。例如构建及安装 AWS s3 组件：

```bash
python3 scripts/build.py command -- cmake -S /path/to/aws-sdk-cpp -B .deps/aws-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/path/to/sdk-install '-DBUILD_ONLY=s3' -DENABLE_TESTING=OFF -DAUTORUN_UNIT_TESTS=OFF -DENABLE_UNITY_BUILD=OFF
python3 scripts/build.py command -- cmake --build .deps/aws-build --parallel '{jobs}'
python3 scripts/build.py command -- cmake --install .deps/aws-build
```

将 nlohmann_json 的 CMake package 安装到相同 prefix，再构建驱动：

```bash
python3 scripts/build.py cpp --prefix /path/to/sdk-install
# 同时编译 OSS、TOS 原生驱动时：
python3 scripts/build.py cpp --prefix /path/to/sdk-install --oss-source /path/to/aliyun-oss-cpp-sdk --tos-source /path/to/ve-tos-cpp-sdk
```

运行时需让动态链接器找到安装目录的 SDK 库，例如设置 `LD_LIBRARY_PATH=/path/to/sdk-install/lib`。
性能测量应使用 Release 构建，并在保存的测试说明中记录编译器、构建模式和运行环境。
本地开发时的 Debug 构建结果仅用于功能测试，不能作为云端性能结论。

资源入口使用 `min(max(1, CPU-2), 8, floor((MemAvailable-12GiB)/2GiB))` 设置并行度，
结果小于 1 则不构建。构建进程限制 CPU affinity、地址空间、累计进程组 RSS，并降低 CPU/I/O 优先级。
可用内存低于 8 GiB、连续 10 秒平均换页超过 64 MiB/s 或内存 PSI full avg10 超过 5% 时终止。
CI 也使用此入口，资源不足的 runner 需要更换或增加内存，不能取消保护后继续构建。

## 无云端依赖的框架测试

```bash
python3 -m unittest discover -s tests -v
python3 -m osv check examples/selftest.json
python3 -m osv run examples/selftest.json --output results/selftest-001
```

## 真实 AWS SDK 的本地 HTTP 集成测试

先构建 Java 和 C++ S3 worker。另建虚拟环境安装可选测试依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r tests/integration/requirements.txt
OSV_RUN_SDK_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration -v
```

测试启动仅监听 loopback 的 Moto，使用假凭据，随机分配端口。
必须存在两种真实 SDK 驱动，否则测试失败，不能以 reference 替代。
覆盖两种 SDK 进入生产调度/校验/报告的完整流程，以及 Java 写入到 C++ 读取和反向读写。
测试代码只生成原始数据和独立预期，存储请求由真实官方 SDK 发出。
未显式开启时跳过；缺少依赖或驱动不算验证成功。

Moto 不验证所有签名、厂商语义或性能。上述通过只证明本地集成流程，
不能证明 OSS/OBS/TOS 原生端点、S3 兼容端点及其鉴权通过。

## 实云对比

选择 `examples/java-oss.json`、`java-obs.json`、`java-tos.json`、`cpp-oss.json` 、`cpp-tos.json`、`java-cos.json` 或 `cpp-cos.json`。
替换示例 endpoint、region、bucket，并从外部凭据管理方式继承认证环境变量。
确认两端连接同一区域、桶、存储类型和网络，再启用对比条件标志。
不要把未经测量的 SDK 或厂商写成已通过；当前没有 C++ OBS 原生驱动。

## GitHub Actions

`.github/workflows/check.yml` 在 push 与 pull request 时运行框架测试、自检、示例配置检查及 Java 构建。
构建使用与本地一致的资源监控入口；首次授权需要包含 `workflow` scope 才能推送工作流文件。
当前 CI 不包含 C++ 构建与可选 SDK 集成测试；这两部分需要准备依赖并按上述命令单独验证。

## 腾讯云 COS C++ 原生 SDK

官方源码：[tencentyun/cos-cpp-sdk-v5](https://github.com/tencentyun/cos-cpp-sdk-v5)。
验证的 SDK 宏版本为 `v5.5.23`，源码提交 `3373ca3d44accde9aa2b3a8ea1b48d2b4510f03b`。
使用 Poco 1.12.4（Foundation、Net、NetSSL、Crypto、Util、JSON、XML）及 OpenSSL。
Poco 与 SDK 都应通过 `scripts/build.py command` 构建，限制并行度并监控内存。
Poco 可按官方说明安装到与 AWS SDK/nlohmann_json 相同的 prefix，之后：

```bash
python3 scripts/build.py command -- cmake -S /path/to/cos-cpp-sdk-v5 -B .deps/cos-build -DCMAKE_BUILD_TYPE=Release -DBUILD_DEMO=OFF -DBUILD_UNITTEST=OFF '-DCMAKE_CXX_FLAGS=-I/path/to/sdk-install/include -include cstdint'
python3 scripts/build.py command -- cmake --build .deps/cos-build --parallel '{jobs}'
python3 scripts/build.py cpp --prefix /path/to/sdk-install --cos-source /path/to/cos-cpp-sdk-v5 --cos-library /absolute/path/to/.deps/cos-build/lib/libcossdk.a
```

`-include cstdint` 兼容当前 SDK 头文件在较新编译器下缺少整数类型声明的问题，不修改上游源码。
COS SDK 上游 CMake 使用 `CMAKE_SOURCE_DIR`，故单独构建后再链接，不能直接当作本工具的子目录。
CMake 4 环境构建旧版上游脚本时可能还需 `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`。
运行时让动态链接器找到 Poco 与 AWS SDK，例如设置 `LD_LIBRARY_PATH=/path/to/sdk-install/lib`。
指定的 SDK 库与源码头文件必须来自同一版本，不能混用。

## COS 本地 HTTP 契约测试

构建两种 COS 驱动后执行（不依赖 Moto 或云端）：

```bash
OSV_RUN_COS_TESTS=1 python3 -m unittest discover -s tests/integration -p test_cos_sdk.py -v
# 只验证已构建的 Java 驱动时：
OSV_RUN_COS_TESTS=1 OSV_COS_TEST_LANGUAGES=java python3 -m unittest discover -s tests/integration -p test_cos_sdk.py -v
```

真实官方 SDK 连接 loopback HTTP fixture，检查请求内容、Range、分页 marker、分块参数和错误分类。
fixture 仅回放独立指定的固定响应，不实现对象存储状态机；测试不构成 COS 服务功能、鉴权或性能结论。
覆盖零字节对象、整对象读取、Range、重复分页 marker、分块参数、404/403 区分。
C++ SDK 的 `SetCheckMD5(false)` 仅用于 Range：整对象 ETag 不能与局部响应体 MD5 比较；
完整 GET 仍使用 SDK 默认校验，并有错误 ETag 的失败测试。框架性能测试继续独立校验实际读取字节。
真实 COS S3/native 对照仍需配置实际端点和凭据。Java 契约测试已纳入 CI，C++ 需本地构建后验证。
