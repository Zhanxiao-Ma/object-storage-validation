# MinIO SDK 与 AWS S3 SDK 性能实测

后续已追加两次独立测试，结果及修正结论见 [三次复测报告](minio-sdk-performance-repeated.md)。以下保留首次测量数据。

同一台本地 MinIO，Java、C++ 分别做同语言对比。全部性能用例一次执行；4 并发、5 轮正式测量。多对象、读取与分片预热 2 轮，同对象重复操作每项预热 100 次。

正式样本 52,600 条，含预热共 71,640 条；失败 0 条，执行错误 0 条；测试对象和未完成分片均已清理。

C++ MinIO SDK 的 HEAD、LIST、小对象读取更快，五轮结果方向一致。Java MinIO SDK 的 HEAD 和删除更快，4 MiB 对象读取基本相同，8 MiB 同对象重复读取在本次汇总中较慢。
写入与分片上传轮间波动较大，部分场景快慢反转，因此不据此给出稳定的 SDK 优劣结论。

## 主要结果

下表为 MinIO SDK / AWS SDK 的已校验吞吐比；大于 1 表示 MinIO SDK 更快。

| 用例 | Java 吞吐比 | C++ 吞吐比 |
|---|---:|---:|
| 4 KiB 对象读取 | 1.23× | 1.45× |
| 128 KiB 多对象读取 | 1.15× | 1.21× |
| 4 MiB 多对象读取 | 1.00× | 1.05× |
| 8 MiB 同一对象重复读取 | 0.73× | 1.12× |
| HEAD | 1.32× | 1.60× |
| LIST | 1.00× | 1.98× |
| 逐对象 DELETE | 1.30× | 1.48× |

| 语言 | 用例 | AWS p95 ms | MinIO p95 ms |
|---|---|---:|---:|
| java | 4 KiB 对象读取 | 1.218 | 0.781 |
| java | HEAD | 1.107 | 0.727 |
| java | LIST | 8.251 | 8.632 |
| cpp | 4 KiB 对象读取 | 1.105 | 0.686 |
| cpp | HEAD | 0.972 | 0.608 |
| cpp | LIST | 12.366 | 6.400 |

## 每轮一致性

| 语言 | 用例 | 五轮吞吐比最小值 | 中位数 | 最大值 |
|---|---|---:|---:|---:|
| java | 4 KiB 对象读取 | 0.93 | 1.22 | 1.52 |
| java | HEAD | 1.10 | 1.41 | 1.51 |
| java | LIST | 0.91 | 1.01 | 1.09 |
| java | 4 MiB PUT | 1.00 | 1.33 | 2.11 |
| java | 128 MiB 对象 / 5 MiB 分片总上传 | 0.14 | 1.23 | 2.89 |
| cpp | 4 KiB 对象读取 | 1.39 | 1.45 | 1.50 |
| cpp | HEAD | 1.49 | 1.58 | 1.73 |
| cpp | LIST | 1.88 | 1.94 | 2.11 |
| cpp | 4 MiB PUT | 0.60 | 1.74 | 2.85 |
| cpp | 128 MiB 对象 / 5 MiB 分片总上传 | 0.43 | 2.31 | 5.81 |

## 环境与负载

| 项目 | 实测配置 |
|---|---|
| MinIO 服务端 | `osv/minio:RELEASE.2025-02-03T21-03-04Z` |
| 镜像 ID | `sha256:8b8bb027a3a980c6a0d662680dad49182b51ee88212347ce32d11bebfa0b404f` |
| 服务端资源 | 4 CPU，2 GiB 内存上限，容器本地数据目录 |
| 网络 | 本机 HTTP 回环 |
| Java 参数 | 两侧均 `-Xmx256m -XX:ActiveProcessorCount=2` |
| 多对象读写 | 100 个 128 KiB 小对象，256 MiB 数据拆成 64 个 4 MiB 对象；每通道每轮 25 次 LIST |
| 重复访问 | 同一个 8 MiB 对象；GET/PUT/Range/HEAD/LIST 分别每轮 100 次 |
| 读取 | 4/64/128 KiB 小对象；64 MiB 来源对象，64 KiB/1 MiB/4 MiB Range，各连续/随机/同对象并发读取每轮 100 次 |
| 分片上传 | 32/128 MiB 对象 × 5/16 MiB 分片，每组 2 个对象 |

| 语言 | SDK | 实际握手版本 |
|---|---|---|
| java | aws-sdk-java-v2 | 2.20.162 |
| java | minio-java | 9.0.3 |
| cpp | aws-sdk-cpp | 1.11.675 |
| cpp | minio-cpp | 1.0.0 |

每个并发通道使用独立客户端；同一时刻只测一个目标，按轮次打乱目标顺序。吞吐按各场景实际测量时间计算，预热不计入汇总。PUT/UploadPart 使用单请求 API；分片 total 从创建到完成合并，读回校验和清理不计入上传时间。
MinIO 两种驱动使用 SDK 公开的内存输入接口，包含一次整块文件读入；AWS 驱动使用文件流。两侧 SDK 各自的 HTTP、签名和校验实现也属于本次客户端路径的差异。
原始结果保存在仓库本地 `results/minio-sdk-comparison-20261021/`：`report/report.json`、`comparison.json`、`validation.json`。驱动和配置见 [MinIO SDK 对比说明](../minio-sdk.md)。

<details>
<summary>全部用例的吞吐和 p95 延迟</summary>

| 语言 | 用例 | 指标 | AWS 吞吐 | MinIO 吞吐 | MinIO/AWS | AWS p95 ms | MinIO p95 ms |
|---|---|---|---:|---:|---:|---:|---:|
| java | delete | verified_ops_s | 3661.65 | 4771.59 | 1.30 | 1.297 | 0.919 |
| java | get | verified_mib_s | 551.96 | 550.05 | 1.00 | 5.564 | 5.637 |
| java | head | verified_ops_s | 4616.84 | 6114.70 | 1.32 | 1.107 | 0.727 |
| java | list | verified_objects_s | 104191.19 | 104192.94 | 1.00 | 8.251 | 8.632 |
| java | multipart_complete_134217728_16777216 | verified_ops_s | 8.95 | 10.37 | 1.16 | 6.053 | 9.549 |
| java | multipart_complete_134217728_5242880 | verified_ops_s | 13.28 | 4.98 | 0.38 | 5.416 | 20.233 |
| java | multipart_complete_33554432_16777216 | verified_ops_s | 501.81 | 346.78 | 0.69 | 8.336 | 8.256 |
| java | multipart_complete_33554432_5242880 | verified_ops_s | 25.69 | 27.07 | 1.05 | 9.492 | 9.236 |
| java | multipart_create_134217728_16777216 | verified_ops_s | 473.34 | 308.05 | 0.65 | 9.634 | 8.572 |
| java | multipart_create_134217728_5242880 | verified_ops_s | 614.93 | 386.69 | 0.63 | 4.334 | 8.104 |
| java | multipart_create_33554432_16777216 | verified_ops_s | 548.58 | 486.01 | 0.89 | 7.880 | 8.517 |
| java | multipart_create_33554432_5242880 | verified_ops_s | 469.57 | 349.41 | 0.74 | 9.820 | 8.582 |
| java | multipart_part_134217728_16777216 | verified_mib_s | 662.55 | 503.70 | 0.76 | 359.720 | 387.404 |
| java | multipart_part_134217728_5242880 | verified_mib_s | 355.45 | 274.03 | 0.77 | 359.052 | 398.099 |
| java | multipart_part_33554432_16777216 | verified_mib_s | 286.53 | 406.40 | 1.42 | 415.016 | 494.835 |
| java | multipart_part_33554432_5242880 | verified_mib_s | 295.67 | 425.22 | 1.44 | 373.957 | 67.082 |
| java | multipart_total_134217728_16777216 | verified_mib_s | 650.87 | 493.16 | 0.76 | 855.117 | 737.779 |
| java | multipart_total_134217728_5242880 | verified_mib_s | 351.71 | 270.12 | 0.77 | 1620.896 | 1415.953 |
| java | multipart_total_33554432_16777216 | verified_mib_s | 278.12 | 385.23 | 1.39 | 420.247 | 502.855 |
| java | multipart_total_33554432_5242880 | verified_mib_s | 287.13 | 396.78 | 1.38 | 419.882 | 209.532 |
| java | put | verified_mib_s | 194.42 | 290.76 | 1.50 | 332.841 | 51.475 |
| java | range | verified_mib_s | 11.17 | 11.13 | 1.00 | 2.101 | 1.554 |
| java | read_hot_1048576 | verified_mib_s | 1174.83 | 1201.31 | 1.02 | 2.629 | 2.537 |
| java | read_hot_4194304 | verified_mib_s | 1793.55 | 1752.13 | 0.98 | 5.279 | 5.702 |
| java | read_hot_65536 | verified_mib_s | 143.99 | 179.43 | 1.25 | 2.180 | 1.514 |
| java | read_random_1048576 | verified_mib_s | 1133.54 | 1222.96 | 1.08 | 2.924 | 2.437 |
| java | read_random_4194304 | verified_mib_s | 1779.85 | 1650.12 | 0.93 | 5.410 | 6.250 |
| java | read_random_65536 | verified_mib_s | 127.01 | 161.61 | 1.27 | 2.584 | 2.068 |
| java | read_sequential_1048576 | verified_mib_s | 1140.07 | 1197.71 | 1.05 | 2.784 | 2.612 |
| java | read_sequential_4194304 | verified_mib_s | 1703.96 | 1674.30 | 0.98 | 6.188 | 6.154 |
| java | read_sequential_65536 | verified_mib_s | 143.47 | 185.10 | 1.29 | 1.998 | 1.459 |
| java | read_small_131072 | verified_mib_s | 346.66 | 451.17 | 1.30 | 1.428 | 0.975 |
| java | read_small_4096 | verified_mib_s | 14.41 | 17.77 | 1.23 | 1.218 | 0.781 |
| java | read_small_65536 | verified_mib_s | 202.14 | 250.05 | 1.24 | 1.301 | 0.892 |
| java | repeat_get | verified_mib_s | 2070.13 | 1504.56 | 0.73 | 10.936 | 32.606 |
| java | repeat_head | verified_ops_s | 4833.12 | 6278.08 | 1.30 | 0.954 | 0.654 |
| java | repeat_list | verified_ops_s | 3286.30 | 3069.60 | 0.93 | 1.520 | 1.590 |
| java | repeat_put | verified_mib_s | 830.43 | 639.48 | 0.77 | 72.717 | 91.323 |
| java | repeat_range | verified_mib_s | 132.64 | 168.25 | 1.27 | 2.103 | 1.739 |
| java | smallget | verified_ops_s | 2245.67 | 2575.78 | 1.15 | 1.461 | 1.016 |
| java | smallput | verified_ops_s | 47.59 | 57.72 | 1.21 | 478.114 | 344.415 |
| cpp | delete | verified_ops_s | 3705.00 | 5468.97 | 1.48 | 1.181 | 0.812 |
| cpp | get | verified_mib_s | 544.12 | 573.46 | 1.05 | 6.504 | 4.957 |
| cpp | head | verified_ops_s | 4271.98 | 6822.99 | 1.60 | 0.972 | 0.608 |
| cpp | list | verified_objects_s | 65773.25 | 130273.67 | 1.98 | 12.366 | 6.400 |
| cpp | multipart_complete_134217728_16777216 | verified_ops_s | 12.95 | 10.50 | 0.81 | 8.462 | 6.918 |
| cpp | multipart_complete_134217728_5242880 | verified_ops_s | 13.84 | 12.72 | 0.92 | 4.769 | 47.177 |
| cpp | multipart_complete_33554432_16777216 | verified_ops_s | 320.76 | 520.27 | 1.62 | 6.762 | 6.821 |
| cpp | multipart_complete_33554432_5242880 | verified_ops_s | 9.66 | 49.93 | 5.17 | 314.932 | 5.762 |
| cpp | multipart_create_134217728_16777216 | verified_ops_s | 436.29 | 419.59 | 0.96 | 6.369 | 8.731 |
| cpp | multipart_create_134217728_5242880 | verified_ops_s | 474.48 | 633.81 | 1.34 | 8.740 | 6.995 |
| cpp | multipart_create_33554432_16777216 | verified_ops_s | 431.10 | 457.34 | 1.06 | 7.456 | 8.470 |
| cpp | multipart_create_33554432_5242880 | verified_ops_s | 333.03 | 478.45 | 1.44 | 7.900 | 7.424 |
| cpp | multipart_part_134217728_16777216 | verified_mib_s | 546.81 | 461.47 | 0.84 | 440.294 | 450.498 |
| cpp | multipart_part_134217728_5242880 | verified_mib_s | 343.86 | 726.54 | 2.11 | 327.684 | 47.723 |
| cpp | multipart_part_33554432_16777216 | verified_mib_s | 396.01 | 524.57 | 1.32 | 532.338 | 322.566 |
| cpp | multipart_part_33554432_5242880 | verified_mib_s | 200.50 | 637.21 | 3.18 | 332.965 | 42.842 |
| cpp | multipart_total_134217728_16777216 | verified_mib_s | 539.01 | 456.40 | 0.85 | 639.152 | 976.100 |
| cpp | multipart_total_134217728_5242880 | verified_mib_s | 340.62 | 696.60 | 2.05 | 1491.703 | 558.210 |
| cpp | multipart_total_33554432_16777216 | verified_mib_s | 377.88 | 496.35 | 1.31 | 538.945 | 336.405 |
| cpp | multipart_total_33554432_5242880 | verified_mib_s | 194.44 | 600.78 | 3.09 | 602.567 | 154.197 |
| cpp | put | verified_mib_s | 154.74 | 243.01 | 1.57 | 365.478 | 299.849 |
| cpp | range | verified_mib_s | 11.12 | 11.08 | 1.00 | 1.900 | 1.517 |
| cpp | read_hot_1048576 | verified_mib_s | 1172.43 | 1306.49 | 1.11 | 2.831 | 2.310 |
| cpp | read_hot_4194304 | verified_mib_s | 1684.44 | 1860.84 | 1.10 | 6.426 | 5.186 |
| cpp | read_hot_65536 | verified_mib_s | 143.87 | 196.84 | 1.37 | 1.884 | 1.370 |
| cpp | read_random_1048576 | verified_mib_s | 1032.35 | 1210.31 | 1.17 | 4.705 | 3.495 |
| cpp | read_random_4194304 | verified_mib_s | 1603.33 | 1841.55 | 1.15 | 8.649 | 5.186 |
| cpp | read_random_65536 | verified_mib_s | 98.55 | 135.38 | 1.37 | 7.058 | 5.263 |
| cpp | read_sequential_1048576 | verified_mib_s | 1166.81 | 1222.46 | 1.05 | 2.892 | 4.746 |
| cpp | read_sequential_4194304 | verified_mib_s | 1647.73 | 1790.76 | 1.09 | 7.035 | 11.118 |
| cpp | read_sequential_65536 | verified_mib_s | 129.81 | 170.85 | 1.32 | 5.129 | 2.451 |
| cpp | read_small_131072 | verified_mib_s | 356.39 | 439.22 | 1.23 | 1.349 | 0.861 |
| cpp | read_small_4096 | verified_mib_s | 13.59 | 19.65 | 1.45 | 1.105 | 0.686 |
| cpp | read_small_65536 | verified_mib_s | 205.02 | 268.97 | 1.31 | 1.191 | 0.800 |
| cpp | repeat_get | verified_mib_s | 1524.72 | 1700.88 | 1.12 | 28.059 | 30.395 |
| cpp | repeat_head | verified_ops_s | 4101.89 | 6398.28 | 1.56 | 0.939 | 0.608 |
| cpp | repeat_list | verified_ops_s | 3680.60 | 5943.52 | 1.61 | 1.145 | 0.714 |
| cpp | repeat_put | verified_mib_s | 753.73 | 907.14 | 1.20 | 75.608 | 68.261 |
| cpp | repeat_range | verified_mib_s | 135.56 | 176.84 | 1.30 | 2.165 | 1.666 |
| cpp | smallget | verified_ops_s | 2248.79 | 2722.14 | 1.21 | 1.337 | 0.928 |
| cpp | smallput | verified_ops_s | 41.67 | 35.54 | 0.85 | 399.229 | 399.141 |

</details>
