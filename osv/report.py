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
             "## 兼容性", "", "| Target | Case | Status |", "|---|---|---|"]
    for row in report["cases"]:
        lines.append(f'| {row["target"]} | {row["case"]} | {row["status"]} |')
    lines += ["", "## 性能", "", "SDK 延迟来自驱动计时；吞吐包含调度与数据校验开销。失败请求单独计数。", "",
              "| Target | Operation | OK / attempts | p50 ms | p95 ms | p99 ms | Verified MiB/s | Verified ops/s |",
              "|---|---|---|---|---|---|---|---|"]
    def fmt(value):
        return "N/A" if value is None else f"{value:.3f}"
    for row in report["summary"]:
        lines.append(f'| {row["target"]} | {row["op"]} | {row["successes"]}/{row["attempts"]} | '
                     + " | ".join(fmt(row[k]) for k in ("p50_ms", "p95_ms", "p99_ms", "verified_mib_s", "verified_ops_s")) + " |")
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
