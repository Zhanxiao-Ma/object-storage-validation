# 扩展语言、SDK 与测试功能

## 新增语言或 SDK

实现一个长驻进程，遵循 [JSONL worker 协议](worker-protocol.md)，并在 manifest 的
`targets[].command` 配置启动参数。无需修改语言枚举、调度逻辑和报告代码。
同一种语言也可以配置多个 SDK 驱动；报告只在同厂商、同语言的 S3/native 目标间比较。
`info` 必须返回真实语言与 SDK 版本，推荐显式返回协议版本及支持的操作列表。
每个驱动负责真实 SDK 的调用与计时，调度器负责生成数据和独立结果校验。

## 新增测试功能

测试注册与语言驱动相互独立。插件是 Python 模块，导出 `register(registry)`。
插件不必包含存储 SDK；测试逻辑通过 worker 协议调用任意语言驱动。
仅在确实需要新 SDK 操作时，同时扩展对应驱动的能力列表和实现。

- `Operation`：一个可测量请求，声明名称、所需操作、请求生成器、结果校验器及对比指标。
- `Suite`：一组兼容性检查，可以执行多步真实操作，返回具有名称与状态的检查结果。
- `Context`：提供本次运行隔离前缀、独立预期数据、本地文件路径、负载配置和 worker。
- `Profile`：一组可复用的 Suite 名称、描述及未覆盖范围，独立于语言和 SDK。
- `Registry`：按运行创建，禁止覆盖已注册名称；插件间不共享运行状态。

`Operation.prepare(ctx)` 返回请求；`verify(ctx, request, result)` 必须独立校验实际结果，
成功返回非负的有效字节数，失败抛异常。元数据操作返回零字节并使用 `verified_ops_s`；
数据操作可以声明 `verified_mib_s`。校验失败不能算成功样本。
当前单个性能样本对应一次 SDK 请求；多步功能可先实现为 Suite，不能把某一步延迟冒充完整流程耗时。

`Suite.execute(ctx)` 返回非空检查列表，每项包含 `case` 和 `status`。
状态为 `pass`、`fail` 或 `observation`；至少包含一个 pass/fail 语义检查。
异常由调度器记录为失败，不是通过。Suite 应自行创建所需输入，不依赖其他 Suite 的执行顺序。

可运行示例为 [inventory_plugin.py](../examples/inventory_plugin.py)。在已有 manifest 增加：

```json
{
  "plugins": ["examples.inventory_plugin"],
  "workload": {
    "operations": ["get", "range", "inventory"],
    "compatibility_suites": ["baseline", "inventory_semantics"]
  }
}
```

以上是与现有配置合并的字段片段。从仓库根目录运行，或将自有插件安装到 Python 环境。
插件是受信任的本地代码，`check` 也会导入插件以校验名称；只加载自己审阅过的模块。
命令仍由 manifest 指定，不自动搜索下载或执行网络插件。

插件和驱动创建的云端数据必须位于 `ctx.prefix` 内，框架仅清理本次运行前缀。
当前性能测试会写入名为 `perf` 的基准对象；插件不得破坏它。
新增复制等功能时，应由真实生产者产生内容，再由真实读取接口验证数据，不能模拟成功响应。

## 验证扩展

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖独立插件进入真实调度/报告、Python 第三种语言驱动、能力缺失、重复注册、
未知场景、指标选择以及清理边界。Python 本地驱动仅验证框架，不构成云端 SDK 兼容性证据。

## 新增测试档位

在插件的 `register(registry)` 中先注册 Suite，再组合 Profile：

```python
from osv.scenarios import Profile

def register(registry):
    registry.add_profile(Profile(
        "smoke", ("core_objects",), "复用基础对象语义",
        ("不包含扩展分块边界或高级 S3 功能",)))
```

随后用 `--profile smoke` 选择。不需要修改 CLI 的档位枚举。
Suite 可在第四个参数 `catalog` 声明元数据元组；每项包含 `case`、`purpose`、`expected`、
`source`、`reference` 与布尔 `gate`，建议再声明 `operations`。
有目录时，结果必须覆盖全部声明的 case 且无重复，必测 gate 不能返回 observation。
目录会进入 JSON/Markdown，便于审核来源、预期与未执行项。
旧的无 catalog 插件仍可运行，但报告明确标出未声明逐项目录的 Suite。
