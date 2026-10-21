# 对象存储测试报告

## 运行概览

- 运行 ID：817d23b4f5c645b8aff0d671188ce980
- 开始时间：2026-10-21T06:48:16.783640+00:00
- 结束时间：2026-10-21T06:49:27.903569+00:00
- 执行模式：functional
- 结果状态：pass = 通过；fail = 失败；warning = 行为警告；not_run = 未执行。

## 驱动配置

| Target | Caller | SDK | SDK language | Version | Retry policy |
|---|---|---|---|---|---|
| rustfs-java-s3 | java | aws-sdk-java | java | 2.20.162 | 0 |
| rustfs-cpp-s3 | cpp | aws-sdk-cpp | cpp | 1.11.675 | 0 |

完整驱动配置与原始结果见 report.json。

## 测试档位与覆盖

档位：**normal**；目录版本：29。
覆盖对象读写、Range、复制、批量及静默删除、桶、V2 分页、分块上传和未完成分块生命周期配置。

| Target | Planned | Pass | Fail | Warning | Not run |
|---|---|---|---|---|---|
| rustfs-java-s3 | 96 | 92 | 3 | 1 | 0 |
| rustfs-cpp-s3 | 96 | 87 | 8 | 1 | 0 |

### 不在本档位覆盖范围

- 文件系统 chmod/chown/mtime 等接口不在对象存储测试范围内。
- 尚未覆盖基于旧版签名（Signature V2）的相关场景。
- 当前档位不包含版本控制、权限策略、加密及其它生命周期和访问日志配置。

档位控制功能用例范围；性能负载单独配置。

## 失败与警告

| 测试目标 | 测试用例 | 状态 | 预期 | 实际结果 |
|---|---|---|---|---|
| rustfs-java-s3 | Range 起点越界处理 | warning | 记录是否成功返回空内容；416 不得混记为符合 对象接口 预期 | 接口预期：成功读取且内容为空；实际 HTTP 416；错误码 InvalidRange；实际行为与接口预期不同 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 encoding_basic | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('keys', ['asdf+b']) != ('keys', ['asdf%2Bb']) |
| rustfs-java-s3 | 对象存储 精确分块上传 missing_upload | fail | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | unexpected result: 400 != 404 |
| rustfs-java-s3 | 静默批量删除现存和不存在对象并重复请求 | fail | 对象确已删除，Deleted/Errors 均为空，特殊键完整往返 | unexpected result: [('never-created', None), ('plain', None), ('special <&+% 中文', None)] != [] |
| rustfs-cpp-s3 | Range 起点越界处理 | warning | 记录是否成功返回空内容；416 不得混记为符合 对象接口 预期 | 接口预期：成功读取且内容为空；实际 HTTP 416；错误码 InvalidRange；实际行为与接口预期不同 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_whitespace | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('delimiter', '') != ('delimiter', ' ') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('delimiter', '') != ('delimiter', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('prefix', '') != ('prefix', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 startafter_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('start_after', '') != ('start_after', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 fetchowner_notempty | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | Unable to parse ExceptionName: InvalidArgument Message: invalid query: fetch-owner: 1 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 fetchowner_empty | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | Unable to parse ExceptionName: InvalidArgument Message: invalid query: fetch-owner: 0 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 missing_upload | fail | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | unexpected result: 400 != 404 |
| rustfs-cpp-s3 | 静默批量删除现存和不存在对象并重复请求 | fail | 对象确已删除，Deleted/Errors 均为空，特殊键完整往返 | unexpected result: [('never-created', None), ('plain', None), ('special <&+% 中文', None)] != [] |

## 兼容性

<details>
<summary>完整功能明细（192 条结果）</summary>

| 测试目标 | 测试用例 | 状态 | 预期 | 实际结果 |
|---|---|---|---|---|
| rustfs-java-s3 | 普通对象读写与元数据 | pass | 读取内容完全一致且 HEAD 大小正确 | 符合预期 |
| rustfs-java-s3 | 空对象完整生命周期 | pass | 空内容且 HEAD 大小为零 | 符合预期 |
| rustfs-java-s3 | 读取不存在对象 | pass | 失败且归一化为 not_found | 符合预期 |
| rustfs-java-s3 | 查询不存在对象 | pass | 失败且归一化为 not_found | 符合预期 |
| rustfs-java-s3 | 读取首字节 | pass | 等于独立输入的首字节 | 符合预期 |
| rustfs-java-s3 | 读取末字节 | pass | 等于独立输入的末字节 | 符合预期 |
| rustfs-java-s3 | 读取中间片段 | pass | 等于输入对应切片 | 符合预期 |
| rustfs-java-s3 | 删除后不可读取或列举 | pass | 重复删除成功且 GET/HEAD 返回 not_found | 符合预期 |
| rustfs-java-s3 | 删除不存在对象 | pass | 重复删除成功 | 符合预期 |
| rustfs-java-s3 | 列举已写入对象并隔离相邻前缀 | pass | 键集合准确、无遗漏或重复 | 符合预期 |
| rustfs-java-s3 | 特殊键名的签名与编码 | pass | 空格、中文、百分号及加号内容与列举一致 | 符合预期 |
| rustfs-java-s3 | 多分块拼接 | pass | 按 part number 拼接且完整读取正确 | 符合预期 |
| rustfs-java-s3 | 取消已有分块的上传 | pass | 取消后对象不存在且不可列举 | 符合预期 |
| rustfs-java-s3 | 记录 Range 末端超界行为 | pass | 返回对象末端的截断内容 | 接口预期：成功读取对象末端的截断内容；返回 1 字节 |
| rustfs-java-s3 | Range 起点越界处理 | warning | 记录起点越界时是否返回空内容 | 接口预期：成功读取且内容为空；实际 HTTP 416；错误码 InvalidRange；实际行为与接口预期不同 |
| rustfs-java-s3 | 创建桶操作重复调用 | pass | 不存在则创建，已有桶不报错 | 符合预期 |
| rustfs-java-s3 | hello 四种部分读取组合 | pass | h/o/llo/ll 四组内容准确 | 符合预期 |
| rustfs-java-s3 | 列举键、大小、时间与 HEAD 信息 | pass | 键和大小正确，修改时间在 30 秒窗口内 | 符合预期 |
| rustfs-java-s3 | marker 超过最后一个键 | pass | 返回空列表且不截断 | 符合预期 |
| rustfs-java-s3 | 100 个键按字典序完整列举 | pass | 精确顺序和完整集合 | 符合预期 |
| rustfs-java-s3 | 中文/JSON/控制字符/百分号键 | pass | 内容与 URL 编码后的列举键无损 | 符合预期 |
| rustfs-java-s3 | 256 MiB 单对象上传 | pass | 上传成功且 HEAD 大小准确；缩小参数单独记录 | bytes=268435456; reference_bytes=268435456; scaled=False |
| rustfs-java-s3 | 斜线结尾的对象键 | pass | 上传、读取与列举均保留末尾斜线 | 符合预期 |
| rustfs-java-s3 | 并发三分块后首块加倍、末块缩短 | pass | 完成后内容与最新分块拼接一致 | 符合预期 |
| rustfs-java-s3 | 复制零字节对象 | pass | 复制对象长度为零且可完整读取 | 符合预期 |
| rustfs-java-s3 | 复制 16 MiB 对象 | pass | 目的对象长度正确且全部字节一致 | 符合预期 |
| rustfs-java-s3 | 同桶复制 | pass | 复制后的完整内容为 foo | 符合预期 |
| rustfs-java-s3 | 复制保留 Content-Type | pass | text/bla 与原始 foo 内容均准确 | 符合预期 |
| rustfs-java-s3 | 无元数据变更的原地复制 | pass | 实际拒绝 InvalidRequest，可观测状态为 400，原对象不变 | 符合预期 |
| rustfs-java-s3 | 原地复制替换元数据 | pass | 元数据为 foo=bar 且对象内容不变 | 符合预期 |
| rustfs-java-s3 | 复制保留元数据 | pass | 3 字节和 1 MiB 两组均保留类型、元数据与长度 | 符合预期 |
| rustfs-java-s3 | 复制替换元数据 | pass | 两组大小均得到新 metadata 和 audio/mpeg，不混入旧字段 | 符合预期 |
| rustfs-java-s3 | 复制不存在的来源对象 | pass | 真实 not_found，不能发布目标对象 | 符合预期 |
| rustfs-java-s3 | 对象存储 独立桶生命周期 head | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-java-s3 | 对象存储 独立桶生命周期 delete_nonempty | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-java-s3 | 对象存储 独立桶生命周期 create_delete | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-java-s3 | 对象存储 独立桶生命周期 missing_key | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-java-s3 | 对象存储 不存在桶 head | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-java-s3 | 对象存储 不存在桶 delete | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-java-s3 | 对象存储 不存在桶 list_v2 | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_percentage | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_whitespace | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_dot | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_unreadable | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_unreadable | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_delimiter_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_delimiter_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_delimiter_prefix_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_delimiter_delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 prefix_delimiter_prefix_delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 maxkeys_zero | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 maxkeys_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 encoding_basic | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('keys', ['asdf+b']) != ('keys', ['asdf%2Bb']) |
| rustfs-java-s3 | 对象存储 原始 LIST V2 many | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 maxkeys_one | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_prefix | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_prefix_underscore | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 delimiter_prefix_ends_with_delimiter | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 startafter_unreadable | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 startafter_not_in_list | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 startafter_after_list | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 continuationtoken_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 continuationtoken_walk | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 continuationtoken_both | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 fetchowner_notempty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 fetchowner_defaultempty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 fetchowner_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 原始 LIST V2 key_count | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-java-s3 | 对象存储 批量删除 V2 重复删除 | pass | 保留原始一次性请求、删除确认、Errors/Contents 字段有无和实际 HTTP 400 断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 批量删除 V2 1001 键上限 | pass | 保留原始一次性请求、删除确认、Errors/Contents 字段有无和实际 HTTP 400 断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 full | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 small | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 resend | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 sizes | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 contents | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 overwrite | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 abort | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 abort_missing | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 empty | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 missing_upload | fail | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | unexpected result: 400 != 404 |
| rustfs-java-s3 | 对象存储 精确分块上传 too_small | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 missing_part | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 对象存储 精确分块上传 etag | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-java-s3 | 验证标准生命周期配置：abort_multipart | pass | 通过真实 PUT/GET 校验规则，再验证替换与删除；不使用加速时钟、不宣称后台已执行 | 符合预期 |
| rustfs-java-s3 | 静默批量删除现存和不存在对象并重复请求 | fail | 对象确已删除，Deleted/Errors 均为空，特殊键完整往返 | unexpected result: [('never-created', None), ('plain', None), ('special <&+% 中文', None)] != [] |
| rustfs-java-s3 | 显式非静默批量删除对照 | pass | 每个现存或不存在键均返回 Deleted，重复请求幂等 | 符合预期 |
| rustfs-cpp-s3 | 普通对象读写与元数据 | pass | 读取内容完全一致且 HEAD 大小正确 | 符合预期 |
| rustfs-cpp-s3 | 空对象完整生命周期 | pass | 空内容且 HEAD 大小为零 | 符合预期 |
| rustfs-cpp-s3 | 读取不存在对象 | pass | 失败且归一化为 not_found | 符合预期 |
| rustfs-cpp-s3 | 查询不存在对象 | pass | 失败且归一化为 not_found | 符合预期 |
| rustfs-cpp-s3 | 读取首字节 | pass | 等于独立输入的首字节 | 符合预期 |
| rustfs-cpp-s3 | 读取末字节 | pass | 等于独立输入的末字节 | 符合预期 |
| rustfs-cpp-s3 | 读取中间片段 | pass | 等于输入对应切片 | 符合预期 |
| rustfs-cpp-s3 | 删除后不可读取或列举 | pass | 重复删除成功且 GET/HEAD 返回 not_found | 符合预期 |
| rustfs-cpp-s3 | 删除不存在对象 | pass | 重复删除成功 | 符合预期 |
| rustfs-cpp-s3 | 列举已写入对象并隔离相邻前缀 | pass | 键集合准确、无遗漏或重复 | 符合预期 |
| rustfs-cpp-s3 | 特殊键名的签名与编码 | pass | 空格、中文、百分号及加号内容与列举一致 | 符合预期 |
| rustfs-cpp-s3 | 多分块拼接 | pass | 按 part number 拼接且完整读取正确 | 符合预期 |
| rustfs-cpp-s3 | 取消已有分块的上传 | pass | 取消后对象不存在且不可列举 | 符合预期 |
| rustfs-cpp-s3 | 记录 Range 末端超界行为 | pass | 返回对象末端的截断内容 | 接口预期：成功读取对象末端的截断内容；返回 1 字节 |
| rustfs-cpp-s3 | Range 起点越界处理 | warning | 记录起点越界时是否返回空内容 | 接口预期：成功读取且内容为空；实际 HTTP 416；错误码 InvalidRange；实际行为与接口预期不同 |
| rustfs-cpp-s3 | 创建桶操作重复调用 | pass | 不存在则创建，已有桶不报错 | 符合预期 |
| rustfs-cpp-s3 | hello 四种部分读取组合 | pass | h/o/llo/ll 四组内容准确 | 符合预期 |
| rustfs-cpp-s3 | 列举键、大小、时间与 HEAD 信息 | pass | 键和大小正确，修改时间在 30 秒窗口内 | 符合预期 |
| rustfs-cpp-s3 | marker 超过最后一个键 | pass | 返回空列表且不截断 | 符合预期 |
| rustfs-cpp-s3 | 100 个键按字典序完整列举 | pass | 精确顺序和完整集合 | 符合预期 |
| rustfs-cpp-s3 | 中文/JSON/控制字符/百分号键 | pass | 内容与 URL 编码后的列举键无损 | 符合预期 |
| rustfs-cpp-s3 | 256 MiB 单对象上传 | pass | 上传成功且 HEAD 大小准确；缩小参数单独记录 | bytes=268435456; reference_bytes=268435456; scaled=False |
| rustfs-cpp-s3 | 斜线结尾的对象键 | pass | 上传、读取与列举均保留末尾斜线 | 符合预期 |
| rustfs-cpp-s3 | 并发三分块后首块加倍、末块缩短 | pass | 完成后内容与最新分块拼接一致 | 符合预期 |
| rustfs-cpp-s3 | 复制零字节对象 | pass | 复制对象长度为零且可完整读取 | 符合预期 |
| rustfs-cpp-s3 | 复制 16 MiB 对象 | pass | 目的对象长度正确且全部字节一致 | 符合预期 |
| rustfs-cpp-s3 | 同桶复制 | pass | 复制后的完整内容为 foo | 符合预期 |
| rustfs-cpp-s3 | 复制保留 Content-Type | pass | text/bla 与原始 foo 内容均准确 | 符合预期 |
| rustfs-cpp-s3 | 无元数据变更的原地复制 | pass | 实际拒绝 InvalidRequest，可观测状态为 400，原对象不变 | 符合预期 |
| rustfs-cpp-s3 | 原地复制替换元数据 | pass | 元数据为 foo=bar 且对象内容不变 | 符合预期 |
| rustfs-cpp-s3 | 复制保留元数据 | pass | 3 字节和 1 MiB 两组均保留类型、元数据与长度 | 符合预期 |
| rustfs-cpp-s3 | 复制替换元数据 | pass | 两组大小均得到新 metadata 和 audio/mpeg，不混入旧字段 | 符合预期 |
| rustfs-cpp-s3 | 复制不存在的来源对象 | pass | 真实 not_found，不能发布目标对象 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 独立桶生命周期 head | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-cpp-s3 | 对象存储 独立桶生命周期 delete_nonempty | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-cpp-s3 | 对象存储 独立桶生命周期 create_delete | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-cpp-s3 | 对象存储 独立桶生命周期 missing_key | pass | 按源用例验证实际状态/错误，最终清理独立测试桶 | 符合预期；独立测试桶已清理 |
| rustfs-cpp-s3 | 对象存储 不存在桶 head | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-cpp-s3 | 对象存储 不存在桶 delete | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-cpp-s3 | 对象存储 不存在桶 list_v2 | pass | 真实服务端返回 404；删除和列举还要求 NoSuchBucket | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_percentage | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_whitespace | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('delimiter', '') != ('delimiter', ' ') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_dot | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('delimiter', '') != ('delimiter', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('prefix', '') != ('prefix', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_delimiter_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_delimiter_alt | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_delimiter_prefix_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_delimiter_delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 prefix_delimiter_prefix_delimiter_not_exist | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 maxkeys_zero | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 maxkeys_none | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 encoding_basic | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 many | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 maxkeys_one | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_prefix | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_prefix_underscore | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 delimiter_prefix_ends_with_delimiter | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 startafter_unreadable | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | unexpected result: ('start_after', '') != ('start_after', '\n') |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 startafter_not_in_list | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 startafter_after_list | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 continuationtoken_empty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 continuationtoken_walk | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 continuationtoken_both | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 fetchowner_notempty | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | Unable to parse ExceptionName: InvalidArgument Message: invalid query: fetch-owner: 1 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 fetchowner_defaultempty | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 fetchowner_empty | fail | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | Unable to parse ExceptionName: InvalidArgument Message: invalid query: fetch-owner: 0 |
| rustfs-cpp-s3 | 对象存储 原始 LIST V2 key_count | pass | 独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 批量删除 V2 重复删除 | pass | 保留原始一次性请求、删除确认、Errors/Contents 字段有无和实际 HTTP 400 断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 批量删除 V2 1001 键上限 | pass | 保留原始一次性请求、删除确认、Errors/Contents 字段有无和实际 HTTP 400 断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 full | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 small | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 resend | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 sizes | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 contents | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 overwrite | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 abort | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 abort_missing | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 empty | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 missing_upload | fail | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | unexpected result: 400 != 404 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 too_small | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 missing_part | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 对象存储 精确分块上传 etag | pass | 保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言 | 符合预期 |
| rustfs-cpp-s3 | 验证标准生命周期配置：abort_multipart | pass | 通过真实 PUT/GET 校验规则，再验证替换与删除；不使用加速时钟、不宣称后台已执行 | 符合预期 |
| rustfs-cpp-s3 | 静默批量删除现存和不存在对象并重复请求 | fail | 对象确已删除，Deleted/Errors 均为空，特殊键完整往返 | unexpected result: [('never-created', None), ('plain', None), ('special <&+% 中文', None)] != [] |
| rustfs-cpp-s3 | 显式非静默批量删除对照 | pass | 每个现存或不存在键均返回 Deleted，重复请求幂等 | 符合预期 |

</details>

## 性能

SDK 延迟来自驱动计时；吞吐包含调度与数据校验开销。失败请求单独计数。

本次未执行性能测试。

执行模式：functional；功能结果不阻断性能执行或 SDK 性能对比。


## 同厂商 SDK 对比

本次仅执行功能测试，未执行 SDK 性能对比。
