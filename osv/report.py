"""Reports distinguish measured evidence, failed gates, and missing comparisons."""
import math
import statistics


def percentile(values, quantile):
    """Nearest-rank percentile; empty populations have no value."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def summarize(samples, batches):
    result = []
    groups = sorted({(s["target"], s["op"]) for s in samples if s["phase"] == "measure"})
    for target, op in groups:
        rows = [s for s in samples if s["target"] == target and s["op"] == op
                and s["phase"] == "measure"]
        good = [s for s in rows if s["ok"]]
        durations = [s["elapsed_ns"] / 1e6 for s in good]
        walls = [b["wall_seconds"] for b in batches
                 if b["target"] == target and b["op"] == op and b["phase"] == "measure"]
        result.append({"target": target, "op": op, "attempts": len(rows),
                       "successes": len(good), "failures": len(rows) - len(good),
                       "p50_ms": percentile(durations, .50),
                       "p95_ms": percentile(durations, .95),
                       "p99_ms": percentile(durations, .99),
                       "sdk_mean_ms": statistics.mean(durations) if durations else None,
                       "verified_ops_s": len(good) / sum(walls) if sum(walls) > 0 else None,
                       "verified_objects_s": sum(s.get("objects",0) for s in good)/sum(walls) if sum(walls)>0 else None,
                       "verified_mib_s": sum(s["bytes"] for s in good) / 2**20 / sum(walls)
                       if sum(walls) > 0 else None})
    return result


def comparisons(config, cases, summary, operation_metrics=None):
    result = []
    targets = {t["id"]: t for t in config.get("targets", [])}
    for pair in config.get("comparisons", []):
        left, right = pair["s3"], pair["native"]
        a, b = targets[left], targets[right]
        reason = None
        if a["provider"] != b["provider"] or a["language"] != b["language"]:
            reason = "provider/language mismatch"
        elif a["api"] != "s3" or b["api"] != "native":
            reason = "comparison requires S3 and native SDKs"
        elif not pair.get("same_storage_and_network_confirmed", False):
            reason = "same storage, storage class, region and network not confirmed"
        elif any(c["status"] != "pass" for c in cases if c["target"] in (left, right)):
            reason = "compatibility gate did not pass"
        elif any(not any(c["target"] == ident for c in cases) for ident in (left, right)):
            reason = "compatibility evidence missing"
        ar = {r["op"]: r for r in summary if r["target"] == left}
        br = {r["op"]: r for r in summary if r["target"] == right}
        if not ar or ar.keys() != br.keys():
            reason = reason or "matching performance samples missing"
        for op in sorted(ar.keys() | br.keys()) or ["all"]:
            x, y = ar.get(op), br.get(op)
            why = reason
            if x and y and (x["failures"] or y["failures"]):
                why = why or "performance requests failed"
            metric = (operation_metrics or {}).get(op, "verified_mib_s" if op in ("get", "range", "put") else "verified_ops_s")
            throughput = (x[metric] / y[metric] if x and y and y[metric] else None)
            latency = x["p95_ms"] / y["p95_ms"] if x and y and y["p95_ms"] else None
            if throughput is None or latency is None:
                why = why or "comparison metrics unavailable"
            enough = bool(x and y and min(x["successes"], y["successes"]) >= pair["min_samples"])
            if not enough:
                why = why or "insufficient sample count"
            within = (throughput is not None and latency is not None
                      and throughput >= pair["min_throughput_ratio"]
                      and latency <= pair["max_p95_ratio"])
            result.append({"s3": left, "native": right, "op": op,
                           "status": "inconclusive" if why else "within_budget" if within else "outside_budget",
                           "reason": why, "throughput_metric": metric, "s3_native_throughput_ratio": throughput,
                           "s3_native_p95_ratio": latency})
    return result


def markdown(report):
    lines = ["# Object Storage Validation", "",
             "结论仅适用于本次 SDK、配置和合成负载；不代表引擎实际链路已验证。",
             "未执行真实云测试时，不得将本地自检用于厂商接入结论。", "",
             "## 兼容性", "", "| Target | Case | Status | Detail |", "|---|---|---|---|"]
    # Record runtime identity and actual retry policy before interpreting performance ratios.
    lines[5:5] = ["## 驱动配置", "", "| Target | Caller | SDK | SDK language | Version | Retry policy |",
                  "|---|---|---|---|---|---|"]
    configuration = []
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    for target in report.get("targets", []):
        info = target.get("worker_info", {})
        values = [target["id"], target["language"], target["sdk"],
                  info.get("sdk_language", info.get("language", "未记录")),
                  info.get("sdk_version", target["sdk_version"]), info.get("retries", "未记录")]
        configuration.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines[9:9] = configuration + ["", "完整驱动配置与源码版本见 report.json 的 worker_info。", ""]
    coverage = report.get("coverage", {})
    if coverage:
        position = lines.index("## 兼容性")
        inventory = ["## 测试档位与覆盖", "", f"档位：**{cell(coverage['profile'])}**；目录版本：{coverage['catalog_version']}。",
                     cell(coverage["description"]), "", "| Target | Planned | Pass | Fail | Observation | Not run |",
                     "|---|---|---|---|---|---|"]
        planned = {(c["suite"], c["case"]) for c in coverage["catalog"]}
        for target in report["targets"]:
            rows = [r for r in report["cases"] if r["target"] == target["id"] and (r.get("suite"), r["case"]) in planned]
            counts = [sum(r["status"] == status for r in rows) for status in ("pass", "fail", "observation")]
            inventory.append("| "+" | ".join(map(str, [target["id"], len(planned), *counts, len(planned)-len(rows)+sum(row["status"]=="not_run" for row in rows)]))+" |")
        inventory += ["", "### 用例来源与预期", "", "| Suite / Case | 预期 | 参考场景 |", "|---|---|---|"]
        for case in coverage["catalog"]:
            inventory.append(f"| {cell(case['suite'])} / {cell(case['case'])} | {cell(case['expected'])} | [{cell(case['reference'])}]({case['source']}) |")
        if coverage["uncatalogued_suites"]:
            inventory += ["", "未声明逐项目录的自定义 Suite："+", ".join(coverage["uncatalogued_suites"])]
        inventory += ["", "### 不在本档位覆盖范围", ""]
        inventory += ["- "+cell(item) for item in coverage["exclusions"]]
        inventory += ["", "档位控制兼容性用例范围；性能负载由 workload 单独配置。参考上游场景，不执行上游测试程序。", ""]
        lines[position:position] = inventory
    if report.get("upstream_alignment"):
        position=lines.index("## 兼容性")
        section=["## 上游能力对齐", "", "逐项映射见 [alignment.json](alignment.json)。partial 不计作已对齐，目录提取不代表用例已经迁移。", "",
                 "| Upstream | Total functions/items | Aligned | Partial | Missing | N/A | Complete |", "|---|---|---|---|---|---|---|"]
        for row in report["upstream_alignment"]["summary"]:
            section.append("| "+" | ".join(map(str,[row["project"],row["total"],row["aligned"],row["partial"],row["not_migrated"]+row["not_selected"],row["not_applicable"],row["complete"]]))+" |")
        section += ["", "以上是实现映射；实际执行数量见档位覆盖表。参数缩放、SDK 回调可观测字段差异及未迁移项目不能省略。", ""]
        lines[position:position]=section
    for row in report["cases"]:
        lines.append(f'| {row["target"]} | {row["case"]} | {row["status"]} | {cell(row.get("error", row.get("behavior", "—")))} |')
    lines += ["", "## 性能", "", "SDK 延迟来自驱动计时；吞吐包含调度与数据校验开销。失败请求单独计数。", "",
              "| Target | Operation | OK / attempts | p50 ms | p95 ms | p99 ms | Verified MiB/s | Verified ops/s | Objects/s |",
              "|---|---|---|---|---|---|---|---|---|"]
    def fmt(value):
        return "N/A" if value is None else f"{value:.3f}"
    for row in report["summary"]:
        lines.append(f'| {row["target"]} | {row["op"]} | {row["successes"]}/{row["attempts"]} | '
                     + " | ".join(fmt(row[k]) for k in ("p50_ms", "p95_ms", "p99_ms", "verified_mib_s", "verified_ops_s", "verified_objects_s")) + " |")
    if report.get("benchmark_datasets"):
        lines += ["", "### 性能数据集", "", "objbench 模型使用多对象与拆块数据集；Range 是额外阶段。POSIX chmod/chown/chtimes 对对象 SDK 不适用。", ""]
        for dataset in report["benchmark_datasets"]:
            lines.append(f"- {dataset['target']}: small={dataset['small_objects']}, blocks={dataset['block_objects']}, bytes={dataset['actual_bytes']}, lanes={dataset['effective_lanes']}; parameters={dataset['reference_parameters']}")
    warmup = [s for s in report["samples"] if s["phase"] == "warmup"]
    lines += ["", f"预热请求：{len(warmup)}；失败：{sum(not s['ok'] for s in warmup)}。预热不进入性能统计。"]
    lines += ["", "## 同厂商 SDK 对比", ""]
    if not report["comparisons"]:
        lines.append("未配置成对的 S3/native SDK，本次没有 SDK 性能对比结论。")
    for row in report["comparisons"]:
        lines.append(f'- {row["s3"]} / {row["native"]}, {row["op"]}: **{row["status"]}**; '
                     f'throughput ratio={fmt(row["s3_native_throughput_ratio"])}, '
                     f'p95 ratio={fmt(row["s3_native_p95_ratio"])}; {row["reason"] or "observed budget check only; not a statistical equivalence claim"}')
    lines += ["", "## 外部工具", "", "外部工具输出独立保存，不转换为 Java/C++ SDK 兼容性通过结果。"]
    for row in report["external"]:
        lines.append(f'- {row["id"]}: {row["tool"]}, exit={row["exit_code"]}; [{row["stdout"]}]({row["stdout"]})')
    return "\n".join(lines) + "\n"
