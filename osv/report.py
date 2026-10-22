"""Report functional verdicts and performance measurements independently."""
from collections import Counter
import math
import re
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
        round_results = []
        for index in sorted({b["round"] for b in batches if b["target"] == target and b["op"] == op and b["phase"] == "measure"}):
            seconds = sum(b["wall_seconds"] for b in batches if b["target"] == target and b["op"] == op and b["phase"] == "measure" and b["round"] == index)
            selected = [s for s in good if s["round"] == index]
            if seconds > 0:
                round_results.append({"round": index, "wall_seconds": seconds,
                    "verified_ops_s": len(selected)/seconds,
                    "verified_objects_s": sum(s.get("objects", 0) for s in selected)/seconds,
                    "verified_mib_s": sum(s["bytes"] for s in selected)/2**20/seconds})
        cvs = {}
        for metric in ("verified_ops_s", "verified_mib_s", "verified_objects_s"):
            rates = [r[metric] for r in round_results]
            cvs[metric] = statistics.pstdev(rates)/statistics.mean(rates) if len(rates) > 1 and statistics.mean(rates) else None
        short = [b["round"] for b in batches if b["target"] == target and b["op"] == op and b["phase"] == "measure" and not b.get("duration_satisfied", True)]
        queue_ms = [(r["scheduler_wait_ns"]+r.get("dispatch_wait_ns", 0)+r.get("queue_wait_ns", 0))/1e6 for r in good if "scheduler_wait_ns" in r]
        rpc_ms = [r["rpc_wall_ns"]/1e6 for r in good if "rpc_wall_ns" in r]
        result.append({"target": target, "op": op, "attempts": len(rows),
                       "successes": len(good), "failures": len(rows) - len(good),
                       "round_results": round_results, "throughput_cv": cvs,
                       "short_write_rounds": short, "queue_p95_ms": percentile(queue_ms, .95),
                       "rpc_p95_ms": percentile(rpc_ms, .95),
                       "measured_seconds": sum(walls),
                       "percentile_sample_count": len(durations),
                       "p50_ms": percentile(durations, .50),
                       "p95_ms": percentile(durations, .95),
                       "p99_ms": percentile(durations, .99),
                       "sdk_mean_ms": statistics.mean(durations) if durations and all(s.get("timing_scope", "sdk") == "sdk" for s in good) else None,
                       "latency_scope": next(iter({s.get("timing_scope", "sdk") for s in rows})) if len({s.get("timing_scope", "sdk") for s in rows}) == 1 else "mixed",
                       "verified_ops_s": len(good) / sum(walls) if sum(walls) > 0 else None,
                       "verified_objects_s": sum(s.get("objects",0) for s in good)/sum(walls) if sum(walls)>0 else None,
                       "verified_mib_s": sum(s["bytes"] for s in good) / 2**20 / sum(walls)
                       if sum(walls) > 0 else None})
    return result


def comparisons(config, cases, summary, operation_metrics=None):
    """Compare performance alone; the legacy cases argument is ignored for API compatibility."""
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
            if metric == "latency_only":
                continue
            throughput = (x[metric] / y[metric] if x and y and y[metric] else None)
            latency = x["p95_ms"] / y["p95_ms"] if x and y and y["p95_ms"] else None
            if x and y:
                if x.get("short_write_rounds") or y.get("short_write_rounds"):
                    why = why or "write measurement window too short"
                if any((r.get("throughput_cv", {}).get(metric) or 0) > .20 for r in (x, y)):
                    why = why or "high between-round throughput variation (CV > 20%)"
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


def display_text(value):
    """Use neutral report wording while leaving machine evidence and source identities intact."""
    text = str(value)
    for old, new in (("JuiceFS 起点越界的 warning 预期", "Range 起点越界处理"),
                     ("JuiceFS warning", "接口行为警告"), ("JuiceFS", "对象接口"),
                     ("Ceph", "对象存储"), ("objbench", "多对象负载")):
        text = re.sub(re.escape(old), lambda match, replacement=new: replacement, text, flags=re.IGNORECASE)
    return text.replace("原版 ", "").replace("原版", "")


def functional_detail(row):
    """Explain observed results directly, rather than printing upstream-specific evidence dictionaries."""
    if row.get("status") == "not_run":
        parts = []
        if row.get("missing_operations"):
            parts.append("驱动未提供操作：" + ", ".join(row["missing_operations"]))
        if row.get("missing_requirements"):
            parts.append("未满足前置条件：" + ", ".join(row["missing_requirements"]))
        if row.get("error"):
            parts.append(row["error"])
        return "；".join(parts) or row.get("reason", "未记录具体原因，见 report.json")
    if row.get("error"):
        return row["error"]
    evidence = row.get("evidence", {})
    observed = evidence.get("observed") if isinstance(evidence, dict) else None
    if isinstance(observed, dict):
        parts = []
        expectations = {"successful read with empty bytes": "成功读取且内容为空",
                        "successful read with clipped bytes": "成功读取对象末端的截断内容"}
        expected = evidence.get("source_expected")
        if expected:
            parts.append("接口预期：" + expectations.get(expected, expected))
        if observed.get("status_code") is not None:
            parts.append("实际 HTTP " + str(observed["status_code"]))
        if observed.get("error_code"):
            parts.append("错误码 " + str(observed["error_code"]))
        if "bytes" in observed:
            parts.append("返回 " + str(observed["bytes"]) + " 字节")
        if evidence.get("source_matches") is False:
            parts.append("实际行为与接口预期不同")
        return "；".join(parts) or row.get("behavior", "—")
    if isinstance(evidence, dict) and evidence and row["status"] == "pass":
        if evidence.get("cleanup_confirmed") is True:
            return "符合预期；独立测试桶已清理"
        return "符合预期；详细证据见 report.json"
    return row.get("behavior", "符合预期" if row["status"] == "pass" else row.get("reason", "—"))


def markdown(report):
    lines = ["# 对象存储测试报告", "",
             "## 兼容性", "", "| Target | Case | Status | Detail |", "|---|---|---|---|"]
    # Record runtime identity and actual retry policy before interpreting performance ratios.
    lines[2:2] = ["## 驱动配置", "", "| Target | Caller | SDK | SDK language | Version | Retry policy |",
                  "|---|---|---|---|---|---|"]
    configuration = []
    def cell(value):
        return display_text(value).replace("|", "\\|").replace("\n", " ")
    for target in report.get("targets", []):
        info = target.get("worker_info", {})
        values = [target["id"], target["language"], target["sdk"],
                  info.get("sdk_language", info.get("language", "未记录")),
                  info.get("sdk_version", target["sdk_version"]), info.get("retries", "未记录")]
        configuration.append("| " + " | ".join(cell(value) for value in values) + " |")
    lines[6:6] = configuration + ["", "完整驱动配置与原始结果见 report.json。", ""]
    overview = ["## 运行概览", "", "- 运行 ID：" + cell(report.get("run_id", "未记录")),
                "- 开始时间：" + cell(report.get("started_at", "未记录")),
                "- 结束时间：" + cell(report.get("finished_at", "未记录")),
                "- 执行模式：" + cell(report["workload"].get("mode", "all")),
                "- 结果状态：pass = 通过；fail = 失败；warning = 行为警告；not_run = 未执行。", ""]
    if report.get("execution_errors"):
        overview += ["### 执行错误", ""]
        for error in report["execution_errors"]:
            overview.append(f"- {cell(error['target'])} / {cell(error['phase'])}: {cell(error['error'])}")
        overview.append("")
    lines[2:2] = overview
    coverage = report.get("coverage", {})
    unreported = []
    metadata = {(c["suite"], c["case"]): c for c in coverage.get("catalog", [])}
    if coverage and report["workload"].get("mode", "all") != "performance":
        position = lines.index("## 兼容性")
        profile_description = {"normal": "覆盖对象读写、Range、复制、批量及静默删除、桶、V2 分页、分块上传和未完成分块生命周期配置。",
                               "complete": "覆盖基础对象操作及版本控制、条件请求、权限、加密、生命周期等扩展功能。"}
        inventory = ["## 测试档位与覆盖", "", f"档位：**{cell(coverage['profile'])}**；目录版本：{coverage['catalog_version']}。",
                     profile_description.get(coverage["profile"], "按配置选择功能用例。"), "", "| Target | Planned | Pass | Fail | Warning | Not run |",
                     "|---|---|---|---|---|---|"]
        planned = {(c["suite"], c["case"]) for c in coverage["catalog"]}
        for target in report["targets"]:
            rows = [r for r in report["cases"] if r["target"] == target["id"] and (r.get("suite"), r["case"]) in planned]
            missing_count = len(planned - {(r.get("suite"), r["case"]) for r in rows})
            if missing_count:
                errors = [e["error"] for e in report.get("execution_errors", []) if e["target"] == target["id"]]
                errors += [r["error"] for r in report["cases"] if r["target"] == target["id"] and r["case"] == "setup" and r.get("error")]
                reason = "未产生用例结果：" + ("；".join(dict.fromkeys(errors)) if errors else "执行提前结束，详见完整明细和 report.json")
                unreported.append((target["id"], missing_count, reason))
            counts = [sum(r["status"] == status for r in rows) for status in ("pass", "fail", "warning")]
            inventory.append("| "+" | ".join(map(str, [target["id"], len(planned), *counts, len(planned)-len(rows)+sum(row["status"]=="not_run" for row in rows)]))+" |")

        if coverage["uncatalogued_suites"]:
            inventory += ["", "部分自定义功能组未声明逐项目录，具体执行结果见下表。"]
        inventory += ["", "### 不在本档位覆盖范围", ""]
        exclusions = ["文件系统 chmod/chown/mtime 等接口不在对象存储测试范围内。",
                      "尚未覆盖基于旧版签名（Signature V2）的相关场景。"]
        if coverage["profile"] == "normal":
            exclusions.append("当前档位不包含版本控制、权限策略、加密及其它生命周期和访问日志配置。")
        if coverage["profile"] == "custom":
            exclusions = coverage["exclusions"]
        inventory += ["- "+cell(item) for item in exclusions]
        inventory += ["", "档位控制功能用例范围；性能负载单独配置。", ""]
        lines[position:position] = inventory
    # Surface actionable results first; retain every original verdict in the full table.
    attention = [r for r in report["cases"] if r["status"] in ("fail", "warning")]
    skipped = [r for r in report["cases"] if r["status"] == "not_run"]
    focus = []
    if attention:
        focus += ["## 失败与警告", "", "| 测试目标 | 测试用例 | 状态 | 预期 | 实际结果 |", "|---|---|---|---|---|"]
        for row in attention:
            item = metadata.get((row.get("suite"), row["case"]), {})
            title = item.get("purpose", {"setup": "初始化", "cleanup": "资源清理"}.get(row["case"], row["case"]))
            title = re.sub(r"(?i)(?:juicefs|ceph)_", "", title)
            focus.append("| " + " | ".join(cell(v) for v in (row["target"], title, row["status"], item.get("expected", "—"), functional_detail(row))) + " |")
        focus.append("")
    if skipped or unreported:
        focus += ["## 未执行原因", "", "按测试目标与具体原因合并计数；对应用例见完整功能明细。", "",
                  "| 测试目标 | 项数 | 原因与缺少条件 |", "|---|---|---|"]
        reasons = Counter((row["target"], functional_detail(row)) for row in skipped)
        grouped = [(target, count, reason) for (target, reason), count in reasons.items()]
        for target, count, reason in grouped + unreported:
            focus.append("| " + " | ".join(cell(v) for v in (target, count, reason)) + " |")
        focus.append("")
    position = lines.index("## 兼容性")
    lines[position:position] = focus
    # Functional results use readable names and expectations; provenance belongs in README.
    position = lines.index("| Target | Case | Status | Detail |")
    lines[position:position+2] = (["<details>", f"<summary>完整功能明细（{len(report['cases'])} 条结果）</summary>", ""] if report["cases"] else []) + ["| 测试目标 | 测试用例 | 状态 | 预期 | 实际结果 |", "|---|---|---|---|---|"]
    for row in report["cases"]:
        item = metadata.get((row.get("suite"), row["case"]), {})
        title = item.get("purpose", {"setup": "初始化", "cleanup": "资源清理"}.get(row["case"], row["case"]))
        title = re.sub(r"(?i)(?:juicefs|ceph)_", "", title)
        expected = item.get("expected", "—")
        if row["case"] == "juicefs_range_start_overflow":
            expected = "记录起点越界时是否返回空内容"
        elif row["case"] == "range_end_overflow":
            expected = "返回对象末端的截断内容"
        lines.append("| " + " | ".join(cell(value) for value in
                     (row["target"], title, row["status"], expected, functional_detail(row))) + " |")
    if report["cases"]:
        lines += ["", "</details>"]
    else:
        lines += ["", "本次未执行功能用例。"]
    lines += ["", "## 性能", "", "单请求延迟来自 SDK 驱动计时；multipart_total 为创建至完成的上传流程耗时，不含输入生成和读回校验。输入生成与预期摘要计算位于计时之外；读取吞吐包含调度与返回内容校验；分片子阶段仅报告延迟与次数；multipart_total 吞吐使用完整上传阶段 wall time，读回校验和清理在测量外。失败请求单独计数。", ""]
    if report["workload"].get("mode") == "functional":
        lines.append("本次未执行性能测试。")
    lines += ["", "执行模式：" + report["workload"].get("mode", "all") + "；功能结果不阻断性能执行或 SDK 性能对比。", ""]
    def fmt(value):
        return "N/A" if value is None else f"{value:.3f}"
    workspace = report.get("performance_workspace")
    if workspace:
        lines += ["", "临时文件系统："+workspace["filesystem"]+"；输入文件在上传计时前逐个 fsync，不强制使用内存文件系统。大块 PUT、重复 PUT 和分片上传每轮自动测量至少 5 秒；达到安全上限仍不足时标记短窗口。", ""]
    if report["summary"]:
        lines += ["", "| Target | Operation | OK / attempts | p50 ms | p95 ms | p99 ms | Verified MiB/s | Verified ops/s | Objects/s | 排队 p95 ms | 逐轮吞吐 min–max / CV |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in report["summary"]:
        metric = report["operation_metrics"].get(row["op"], "verified_ops_s")
        rates = [r[metric] for r in row.get("round_results", [])] if metric != "latency_only" else []
        variation = "N/A"
        if rates:
            cv = statistics.pstdev(rates)/statistics.mean(rates) if statistics.mean(rates) else 0
            variation = f"{min(rates):.3f}–{max(rates):.3f} / {cv:.1%}" if len(rates) > 1 else "仅 1 轮"
        lines.append(f"| {row["target"]} | {row["op"]} | {row["successes"]}/{row["attempts"]} | "
                     + " | ".join(fmt(row[k]) for k in ("p50_ms", "p95_ms", "p99_ms", "verified_mib_s", "verified_ops_s", "verified_objects_s")) + f" | {fmt(row.get("queue_p95_ms"))} | {variation} |")
    if report["summary"]:
        lines += ["", "逐轮吞吐单位与该用例主指标一致；CV 为各轮吞吐的标准差/均值，不表示置信区间。CV 超过 20% 的预算对比记为 inconclusive，该阈值是质量提示而非统计显著性检验。排队包含调度器等待、执行器派发与客户端等待；RPC wall、SDK 时间、主机及进程 I/O 证据保留在 JSON。各轮耗时与有效样本数保留在 JSON；不足 100 个成功样本的 p99 仅作样本描述。"]
    if report.get("benchmark_datasets"):
        lines += ["", "### 性能数据集", "", "使用多对象与拆块数据集；Range 是额外阶段。", ""]
        for dataset in report["benchmark_datasets"]:
            lines.append(f"- {cell(dataset['target'])}: small={dataset['small_objects']}, blocks={dataset['block_objects']}, bytes={dataset['actual_bytes']}, lanes={dataset['effective_lanes']}; parameters={dataset['reference_parameters']}")
    if report.get("io_datasets"):
        lines += ["", "### 读取与分片负载配置", "", "每个 lane 使用独立 SDK 进程/客户端；hot 读取并发访问同一对象。完整参数及每个请求的 offset/length、分片号和计时口径见 report.json。", ""]
        for dataset in report["io_datasets"]:
            lines.append(f"- {cell(dataset['target'])}: model={cell(dataset['model'])}, phase={cell(dataset['phase'])}, round={dataset['round']}, concurrency={dataset['concurrency']}; parameters={cell(dataset['parameters'])}")
    warmup = [s for s in report["samples"] if s["phase"] == "warmup"]
    if report["workload"].get("mode") != "functional":
        lines += ["", f"预热请求：{len(warmup)}；失败：{sum(not s['ok'] for s in warmup)}。预热不进入性能统计。"]
    lines += ["", "## 同厂商 SDK 对比", ""]
    if report["workload"].get("mode") == "functional":
        lines.append("本次仅执行功能测试，未执行 SDK 性能对比。")
    elif not report["comparisons"]:
        lines.append("未配置成对的 S3/native SDK，本次没有 SDK 性能对比结论。")
    for row in report["comparisons"]:
        lines.append(f'- {cell(row["s3"])} / {cell(row["native"])}, {cell(row["op"])}: **{row["status"]}**; '
                     f'throughput ratio={fmt(row["s3_native_throughput_ratio"])}, '
                     f'p95 ratio={fmt(row["s3_native_p95_ratio"])}; {cell(row["reason"] or "observed budget check only; not a statistical equivalence claim")}')
    if report["external"]:
        lines += ["", "## 外部工具", "", "外部工具输出独立保存，不转换为 Java/C++ SDK 兼容性通过结果。"]
    for row in report["external"]:
        lines.append(f'- {row["id"]}: {row["tool"]}, exit={row["exit_code"]}; [{row["stdout"]}]({row["stdout"]})')
    return "\n".join(lines) + "\n"
