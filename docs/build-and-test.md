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
覆盖两种 SDK 进入生产调度/校验/报告的完整流程、多对象 objbench 阶段，以及 Java 写入到 C++ 读取和反向读写。
完善档还保留 Moto 5.1.15 在 MaxKeys=0 时错误返回 IsTruncated=true 的失败证据；测试预期准确发现它并阻止性能阶段，不能将该差异改成通过。
测试代码只生成原始数据和独立预期，存储请求由真实官方 SDK 发出。
未显式开启时跳过；缺少依赖或驱动不算验证成功。

Moto 不验证所有签名、厂商语义或性能。上述通过只证明本地集成流程，
不能证明 OSS/OBS/TOS 原生端点、S3 兼容端点及其鉴权通过。

## 实云对比

选择 `examples/java-oss.json`、`java-obs.json`、`java-tos.json`、`cpp-oss.json` 、`cpp-tos.json`、`java-cos.json` 或 `cpp-cos.json`。
替换示例 endpoint、region、bucket，并从外部凭据管理方式继承认证环境变量。
确认两端连接同一区域、桶、存储类型和网络，再启用对比条件标志。
不要把未经测量的 SDK 或厂商写成已通过；C++ OBS 原生驱动调用官方 C SDK。

## GitHub Actions

`.github/workflows/check.yml` 在 push 与 pull request 时运行框架测试、自检、示例配置检查及 Java 构建。
构建使用与本地一致的资源监控入口；首次授权需要包含 `workflow` scope 才能推送工作流文件。
CI 还会从固定源码构建官方 OBS C SDK 和 C++ 原生驱动，并运行其 HTTP 契约测试。
AWS C++、OSS/TOS/COS C++ 构建及 Moto 集成测试仍需按上述命令单独验证。

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

## 华为 OBS C SDK 与 C++ 原生驱动

从 [官方仓库](https://github.com/huaweicloud/huaweicloud-sdk-c-obs) 获取源码。
本次验证固定提交 `927c5320d10c9dfc14e4b20fd2eb210daeca166e`；`inc/request.h` 中的 SDK 版本及请求 User-Agent 均为 `3.26.6`。
该快照顶层 CMake 的 `LIBOBS_VER_*` 字段与 User-Agent 版本不同，报告采用实际请求使用的 SDK 版本，并记录源码提交。
验证环境为 Linux x86_64、GCC 13.3；官方 C SDK 的预编译依赖单独保留其许可证，不纳入本仓库。

```bash
python3 scripts/build.py command -- cmake -S /path/to/obs-sdk -B .deps/obs-sdk-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
python3 scripts/build.py command -- cmake --build .deps/obs-sdk-build --parallel '{jobs}'
python3 scripts/obs_runtime.py --source /path/to/obs-sdk --library .deps/obs-sdk-build/lib/libeSDKOBS.so --output .deps/obs-runtime
python3 scripts/build.py cpp --prefix /path/to/sdk-install --obs-source /path/to/obs-sdk --obs-library /absolute/path/to/.deps/obs-runtime/libeSDKOBS.so
```

`obs_runtime.py` 在新目录中链接该 SDK 的现有依赖，不覆盖系统库。SDK 源码与构建出的库必须来自同一快照。
C++ 包含上游 C 头文件时，针对其中的成员/类型同名问题，仅在该头文件范围屏蔽 GCC 13+ 的 `-Wchanges-meaning` 诊断。
未修改官方 SDK 源码，也未对整个驱动启用 `-fpermissive`。

仅构建原生驱动时可加 `--without-s3`，无需安装 AWS SDK，但仍需 nlohmann_json 3.11+。
C SDK 的全部动态依赖必须可被加载；在 manifest 的原生目标中设置 `LD_LIBRARY_PATH` 为上述运行库目录的绝对路径。
S3 目标继承或配置它自己的 AWS SDK 库路径，两侧不共享替换后的 libcurl/OpenSSL。

原生配置约束：

- `OSV_ENDPOINT` 默认是服务域名 URL，例如 `https://obs.cn-north-4.myhuaweicloud.com`。
- `OSV_PATH_STYLE=false`。这版 C SDK 的 Path Style 会选 S3 API，驱动直接拒绝该配置。
- 使用完整桶自定义域名时设置 `OSV_CNAME=true`；loopback 测试使用此模式，不依赖外部 DNS。
- `OSV_RETRIES=sdk-default`。SDK 内置重试不可通过公开 options 配置，源码循环预算不是每次实际请求次数；不提供故障重试性能结论。
- 开启 TLS 证书链与主机名校验；自定义信任根可通过 `OSV_CA_BUNDLE` 指定。
- `OSV_CONNECTIONS` 设置 SDK/libcurl 缓存连接数，不能将其解释为整个压测的并发上限。

配置同桶对照使用 `examples/cpp-obs.json`，保持地域、桶、网络及存储类型一致，保留重试策略差异。

```bash
OSV_RUN_OBS_TESTS=1 OSV_OBS_LIBRARY_PATH=/absolute/path/to/.deps/obs-runtime python3 -m unittest discover -s tests/integration -p test_obs_sdk.py -v
```

七项契约测试使用真实 C++ 驱动与官方 C SDK，覆盖 OBS 鉴权/临时凭据、完整读取、首末/中间 Range、
零字节对象、HEAD、删除、404/403、分页与重复 marker、分块参数、回调写入失败以及无效重试/Path Style 配置。
固定 HTTP fixture 不实现对象存储状态机，不能作为 OBS 实云兼容性、鉴权成功或性能结论。

## 分层测试与档位回归

```bash
python3 -m unittest discover -s tests -v
python3 -m osv run examples/selftest.json --profile normal --output results/normal-selftest
python3 -m osv run examples/selftest.json --profile complete --output results/complete-selftest
# 已安装 AWS SDK 时构建共享响应流回归；--without-s3 可单独构建标准库回归
python3 scripts/build.py cpp --without-s3 --with-tests
ctest --test-dir drivers/cpp/build --output-on-failure
```

真实 AWS Java/C++ SDK 对 Moto 的集成测试现在执行 complete 档，包括 1001 个键的汇总分页、
空对象读写、覆盖写和分块边界。按前文安装 Moto 并构建 worker 后：

```bash
OSV_RUN_SDK_TESTS=1 python3 -m unittest discover -s tests/integration -p test_s3_sdk.py -v
```

默认需要 java 与 cpp；可设 `OSV_S3_TEST_LANGUAGES=java` 仅验证 Java，跨语言互读测试会明确跳过。
此测试通常数十秒到数分钟，逐请求受配置的 timeout_seconds 限制；不访问实云。
GitHub Actions 自动执行两档框架自检、Java AWS SDK 完善档、COS Java 契约与 OBS C SDK 契约测试。

共享 C++ 响应保存回归覆盖空流、二进制、恰好一个缓冲区、多缓冲区、旧文件截断、
输入 bad/fail 状态、无法打开输出文件与 /dev/full 写入失败。
这是 worker 的 IO 正确性验证，不是模拟对象存储协议。

## 十条 SDK 链路的通用 HTTP 契约

构建 Java 五种 SDK 和 C++ 五种驱动后：

```bash
OSV_RUN_PAGE_TESTS=1 OSV_OBS_LIBRARY_PATH=/path/to/obs-runtime python3 -m unittest discover -s tests/integration -p test_page_sdk.py -v
# 可选择已构建的真实链路；没有选择的链路不算通过：
OSV_RUN_PAGE_TESTS=1 OSV_PAGE_TEST_TARGETS=java:s3,java:oss,java:obs,java:tos,java:cos python3 -m unittest discover -s tests/integration -p test_page_sdk.py -v
```

测试逐条验证真实 PUT/GET/Range/HEAD/DELETE、实际鉴权前缀、单页 LIST 参数和响应元数据，
以及错误 ETag 确实发往服务端、返回 InvalidPart。固定响应中 TOS 使用其原生 JSON，其余接口使用对应 XML。
负面测试不接受本地 JSON 类型错误、worker 崩溃或不支持操作冒充服务端拒绝。
HTTP fixture 处理标准 chunked 和 AWS streaming 请求体；不实现存储引擎或替换 SDK。
SDK 计时从 JSON 请求解析完成后开始，包含请求映射、实际 SDK 调用和文件流读写；进程/协议往返另行记录。

两项已验证的原生端点限制：

- OBS Java SDK 3.25.10 在 IP/path-style/CNAME 下自动将 OBS 签名改为 AWS V2。本工具拒绝此隐式回退，避免把它作为原生协议比较；使用标准 DNS 端点。可通过 `OSV_HTTP_PROXY=http://host:port` 设置 OBS Java 的显式 HTTP 代理。本地契约测试通过代理将标准 DNS 请求送到回环服务，不依赖外部 DNS 或修改 hosts。
- TOS C++ SDK 2.6.30 拒绝 IP、显式端口及 S3 端点。本工具提前输出诊断，避免上游 SDK 早退后空指针崩溃。测试使用标准端口的 localhost 端点，通过回环 HTTP 代理回放响应，不修改 SDK。实云使用 TOS 原生 DNS 端点。

这些测试证明适配层发出的真实请求和解析结果，不能证明云服务接受签名或给出性能结论。

新增空白分隔符场景还发现固定 AWS C++ SDK 1.11.675 将 LIST 响应中的纯空格/换行 Delimiter 解析为空字符串。
Java SDK 在相同响应下保留原值；独立固定 XML 回放能复现，因而这两项不能归因于 Moto 或云厂商。
完善档仍要求原值准确，C++ 链路对应两项必须报失败。集成回归断言的是工具准确检出差异，绝不把失败门槛改成通过。

## 条件读取、元数据与复制契约

```bash
OSV_RUN_CONDITION_TESTS=1 OSV_RUN_METADATA_TESTS=1 OSV_RUN_COPY_TESTS=1 OSV_OBS_LIBRARY_PATH=/path/to/obs-runtime python3 -m unittest discover -s tests/integration -p 'test_*_sdk.py' -v
```

可通过 OSV_PAGE_TEST_TARGETS 选择已构建的真实链路。上述测试验证四个条件头和日期、实际 412 拒绝、PUT/HEAD/GET 元数据、复制源一次编码、Content-Type 与元数据替换。
固定响应只验证 SDK 请求及解析；实际复制的生产者→消费者数据流另由 Moto 和本地 reference 场景验证，不据此声称云端复制成功。
OBS C SDK 发出的条件日期使用 UTC 字样，其余当前链路使用 GMT；测试保留实际拼写，不能把 SDK 请求格式与云端接受该请求混为一谈。
已通过故意移除 TOS C++ 复制 Content-Type 请求头的适配，确认真实 SDK 契约测试会失败，恢复后通过；来源预期区分也有故意破坏后的失败验证。


### Ceph 精确响应断言回归

构建所选真实 SDK 后，可设置 `OSV_RUN_HTTP_TESTS=1`、`OSV_RUN_TAG_TESTS=1`、`OSV_RUN_HEADER_TESTS=1`
运行 `tests/integration/test_http_sdk.py`、`test_tag_sdk.py`、`test_header_sdk.py`。
HTTP 测试用 `OSV_PAGE_TEST_TARGETS` 选择链路，标签/写入和 HEAD 字段测试用 `OSV_S3_TEST_LANGUAGES=java,cpp` 选择 AWS SDK。
这些固定响应合同检查真实 SDK 的线缆请求及响应保留；Moto 端到端用例检查真实写入、读取和报告，不是云认证。

当前 TOS C++ 2.6.30 使用数值 `0,0` 设置 Range 会省略请求头；worker 改用公开的字符串 Range API。
它把零字节成功 GET 表示为空指针流；worker 只在实际 HTTP 200、显式 Content-Length=0 且对象长度为零时写出空文件，仍检查文件 IO 错误。
AWS C++ Curl 的响应头回调只在收到响应体数据时触发，无法覆盖空 PUT/HEAD/DELETE；worker 通过公开 HTTP factory 委托官方 Curl 客户端并检查实际响应。
无 HTTP 响应的连接错误保留 SDK 原错误，不构造成功状态。


精确桶/列举合同测试分别使用 `OSV_RUN_BUCKET_TESTS=1` 和 `OSV_RUN_LIST_TESTS=1`，
通过 `OSV_S3_TEST_LANGUAGES=java,cpp` 选择已经构建的实际 AWS SDK workers。
`test_s3_sdk.py` 还在独立 Moto 服务中执行桶生命周期和完整 LIST 场景，检查失败路径清理与默认桶数据隔离。
固定 Moto 不等同于标准或云厂商；断言中的源预期不因本地服务返回值而修改。

批量删除合同使用 `OSV_RUN_DELETE_TESTS=1` 运行 `test_delete_many_sdk.py`，检查一次性 1001 键请求、混合结果、版本标记和 Quiet。

分块 SDK 原始请求契约：`OSV_RUN_MULTIPART_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_multipart_sdk -v`。覆盖元数据头、完成 XML 顺序、服务端 ETag/请求 ID、空请求体及精确错误响应。

版本 SDK 契约：`OSV_RUN_VERSION_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_versioning_sdk -v`。覆盖配置 XML、字段缺失、VersionId 查询参数、真实下载体、版本/标记列表顺序及错误头。完整 reference 自测现在应出现 45 项版本控制/配置 API not_run 并退出 1；单测检查此预期以及性能阻断，CI 另导出完整目录。真实版本用例在 SDK 集成测试中执行。

配置模型 SDK 契约：`OSV_RUN_API_MODEL_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_api_models_sdk -v`。覆盖真实嵌套 XML、独立服务响应、策略流、默认加密、布尔 false、32/64 位整数端点、溢出/错误类型、null 及作用域冲突；期望客户端错误无 HTTP 请求/状态。生成完整性检查：`python3 scripts/generate_bucket_api.py --check`。


签名前请求头合约（实际 AWS Java/C++ worker，回环 HTTP，无云凭据）：

```bash
OSV_RUN_WIRE_HEADER_TESTS=1 python3 -m unittest discover -s tests/integration -p test_wire_headers_sdk.py -v
```

覆盖官方签名后的实际头、无效输入零 HTTP、身份/请求上下文隔离、WHEN_REQUIRED 校验和、持续进展响应的总超时、非法长度下控制输入隔离，以及实际 ACL 所有者读回进入异常创建清理。只验证一种语言时设置 `OSV_S3_TEST_LANGUAGES=java` 或 `cpp`。这组定向测试通过后再运行批次末的完善档回归。


策略条件、元数据边界和摘要/属性的定向回归（需真实 SDK 构建及本地 Moto 依赖）：

```bash
PYTHONPATH=tests/integration:. OSV_RUN_POLICY_CONDITION_TESTS=1 OSV_RUN_METADATA_EDGES_TESTS=1 OSV_RUN_CHECKSUM_ATTRIBUTE_TESTS=1 python3 -m unittest test_policy_conditions_sdk test_metadata_edges_sdk test_checksum_attributes_sdk -v
```

这些测试核对真实签名请求、源函数的生产者输出到后续请求、缺字段及清理；固定 SDK/Moto 的诊断差异保留为 fail/not_run，不用于云厂商性能比较。


版本条件、公共控制和完整复制的定向回归：

```bash
PYTHONPATH=tests/integration:. OSV_RUN_CONDITIONAL_VERSIONS_TESTS=1 OSV_RUN_PUBLIC_CONTROL_TESTS=1 OSV_RUN_COPY_CONTRACT_TESTS=1 python3 -m unittest test_conditional_versions_sdk test_public_controls_sdk test_copy_contracts_sdk -v
```

版本复制的原始请求边界测试要求查询分隔符保持字面 `?versionId=`，路径和版本值分别编码。批量删除输入测试要求所有非法项在发 HTTP 前被拒绝，包含有效首项后接非法项，避免执行部分删除。


本地 Moto 每个实例运行于独立 spawn 进程，避免其模块级全局账户/桶状态跨测试泄漏；服务端仍串行处理，客户端并发保持原样。隔离回归同时验证同账户/同桶/同键的两个服务读回不同原始字节，以及前一个服务停止后新服务账户为空：

```bash
PYTHONPATH=tests/integration:. OSV_RUN_MOTO_TESTS=1 python3 -m unittest test_local_moto -v
```


共享客户端并发、状态、剩余策略和标准日志控制回归：

```bash
PYTHONPATH=tests/integration:. OSV_RUN_STATE_BOUNDARY_TESTS=1 OSV_RUN_BASIC_BOUNDARY_TESTS=1 OSV_RUN_REMAINING_POLICY_TESTS=1 OSV_RUN_LOGGING_CONTROL_TESTS=1 python3 -m unittest test_state_boundaries_sdk test_basic_boundaries_sdk test_policy_remaining_sdk test_logging_controls_sdk -v
```

本地固定凭证和区域的 HTTP/Moto fixtures 设置 `AWS_EC2_METADATA_DISABLED=true`，防止 C++ 默认配置构造等待无关 EC2 metadata 探测。生产 runner 环境不作此改动；本地启动耗时下降不作为云存储性能证据。


## Select、日志扩展与异步链路的定向验证

完成真实 SDK 构建后，可单独运行以下测试。通过 `OSV_S3_TEST_LANGUAGES=java` 或 `cpp` 选择已构建的链路；C++ 同时需要正确的 `LD_LIBRARY_PATH`。IAM/STS 的 C++ 回归需要 `--with-control`；SNS/S3Control 回归需要 `--with-auxiliary`（同时启用控制面）。上游 AWS SDK 应安装对应组件，例如配置 `-DBUILD_ONLY=s3;iam;sts;sns;s3control` 后，再通过资源守卫运行 `python3 scripts/build.py cpp --prefix /path/to/sdk-install --with-auxiliary`。完整 Moto 固定诊断回归以启用这些桥接的 SDK 构建为基准。

```bash
OSV_RUN_SELECT_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_select_sdk -v
OSV_RUN_CEPH_LOGGING_WIRE_TESTS=1 OSV_RUN_LOGGING_REMAINING_EXTENSION_TESTS=1 OSV_RUN_LOGGING_REMAINING_PARALLEL_TESTS=1 OSV_RUN_LOGGING_REMAINING_LAST_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_ceph_logging_sdk test_logging_remaining_extensions_sdk test_logging_remaining_parallel_sdk test_logging_remaining_last_sdk -v
OSV_RUN_ASYNC_SDK_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_async_sdk -v
OSV_RUN_AUXILIARY_TESTS=1 PYTHONPATH=tests/integration:. python3 -m unittest test_auxiliary_sdk -v
```

Select 使用官方 Java 异步 SDK / C++ SDK 解码真实 eventstream；固定响应测试提供独立 CRC 帧，不实现 SQL 引擎。Records 按实际事件分别解码 UTF-8，保留 Progress/Stats/End 与原生错误。原 Ceph `s3select_assert_result` 只检查非空，不比较结果相等，因此它通过不能证明 SQL 结果正确。Moto 上随机 SQL 的 pass/fail 单独记录，不属于旧版固定 Moto 诊断集合的兼容认证；结束后仍检查实际测试桶已清理。

两个原聚合源包含固定桶 `testbuck2`，必须显式配置 `workload.ceph_literal_bucket_names_authorized: true`，源执行前及创建前均以真实 HEAD404 确认不存在；不会替换为 UUID。未授权或已存在时在任何源写入前 `not_run`。原输出格式 TODO 用例始终为 `not_run upstream_todo`。

CreateBucket 已提交但响应丢失时，原 SDK 错误仍使源失败。候选桶存在且配置了独立 `workload.identities.main.canonical_id` 后，必须让实际候选 ACL、新建独立控制桶 ACL 与该 ID 全部匹配才清理；否则报告包含未知桶名与 `cleanup_pending`，不会删除所有权未经证明的候选。

Ceph 日志扩展要求显式启用 `workload.ceph_logging_wire_extensions`，表示使用官方 SDK 公共 hook 的协议适配，不代表 stock AWS SDK 原生提供这些字段。并行源使用同一实际 SDK client 的异步请求，原300次PUT与30次flush源要求真实连接池至少100，以及实际握手 `async_jobs_limit>=330`；当前驱动声明512。旧驱动未声明时按64处理，该源保持 `not_run`，避免降低原交错空间。SNS 与账号级 S3Control 源要求独立端点、身份及隔离授权；不能回退到 S3 主身份或修改共享账号。

这些组均使用独立测试开关，OBS 原生 SDK 的 discover 不会因此启动 Java/C++ S3 或辅助服务测试。普通 CI 明确设置 `OSV_RUN_STS_EXPIRY_TESTS=0`；原900秒过期源仅在显式启用时等待真实900秒，不能缩短时间作为替代证据。
