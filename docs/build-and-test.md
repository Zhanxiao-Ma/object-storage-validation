# 构建与验证

## Java

需要 Linux、Python 3.10+、JDK 17 和 Maven。驱动编译目标为 Java 11。

```bash
python3 scripts/build.py java
```

当前依赖版本在 `drivers/java/pom.xml` 固定：AWS v2 2.20.162、OSS 3.18.1、OBS 3.25.10、TOS 2.9.19。
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
CI 模板也使用此入口，资源不足的 runner 需要更换或增加内存，不能取消保护后继续构建。

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

选择 `examples/java-oss.json`、`java-obs.json`、`java-tos.json`、`cpp-oss.json` 或 `cpp-tos.json`。
替换示例 endpoint、region、bucket，并从外部凭据管理方式继承认证环境变量。
确认两端连接同一区域、桶、存储类型和网络，再启用对比条件标志。
不要把未经测量的 SDK 或厂商写成已通过；当前没有 C++ OBS 原生驱动。

## GitHub Actions 模板

`ci/github-actions.yml` 保存框架测试与 Java 构建的工作流模板，当前未启用自动 CI。
具有 GitHub Actions 工作流写入权限后，可复制到 `.github/workflows/check.yml` 启用。
当前发布登录凭据不包含 `workflow` scope，因此仓库只保留模板。
模板不包含 C++ 构建与可选 SDK 集成测试；这两部分需要准备依赖并按上述命令单独验证。
