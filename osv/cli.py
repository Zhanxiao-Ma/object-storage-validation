"""One manifest, real SDK subprocesses, reproducible payloads and explicit evidence."""
import argparse
from datetime import datetime, timezone
import platform
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
import uuid

from .worker import Worker, invoke_external
from .scenarios import Context, build_registry, require_ok, blocks_measurement
from .report import summarize, comparisons, markdown


def load_config(path, profile_override=None):
    config = json.loads(Path(path).read_text())
    if config.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    targets = config.get("targets", [])
    external = config.get("external", [])
    if not targets and not external:
        raise ValueError("configure at least one SDK target or external tool")
    ids = set()
    for target in targets + external:
        ident = target.get("id", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", ident) or ident in ids:
            raise ValueError("unique safe target IDs required")
        ids.add(ident)
        command = target.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
            raise ValueError("command must be a nonempty argument array; shell commands are not supported")
        env = target.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("env must map strings to strings")
        if any(any(word in k.upper() for word in ("SECRET", "TOKEN", "PASSWORD", "ACCESS_KEY")) for k in env):
            raise ValueError("credentials must be inherited from the environment, not stored in the manifest")
    for target in targets:
        for key in ("provider", "language", "api", "sdk", "sdk_version"):
            if not isinstance(target.get(key), str) or not target[key]:
                raise ValueError(f"target requires {key}")
        if target["api"] not in ("s3", "native", "local"):
            raise ValueError("api must be s3, native or local")
    for spec in external:
        if not isinstance(spec.get("tool"), str) or not spec["tool"]:
            raise ValueError("external tool requires a nonempty tool label")
    workload = config.get("workload", {})
    if profile_override is not None:
        workload = {**workload, "profile": profile_override}
        workload.pop("compatibility_suites", None)
    if "profile" in workload and "compatibility_suites" in workload:
        raise ValueError("choose profile or compatibility_suites, not both")
    defaults = {"object_size": 8 * 1024 * 1024, "range_size": 64 * 1024,
                "requests": 100, "concurrency": 4, "warmup_requests": 4,
                "rounds": 3, "part_size": 5 * 1024 * 1024, "timeout_seconds": 60,
                "seed": 1729, "list_objects": 1001, "large_object_size": 256*1024*1024, "performance_model": "objbench", "operations": ["get", "range", "put", "head"]}
    workload = {**defaults, **workload}
    for key in ("object_size", "range_size", "requests", "concurrency", "rounds", "part_size", "timeout_seconds", "large_object_size"):
        if type(workload[key]) is not int or workload[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if type(workload["warmup_requests"]) is not int or workload["warmup_requests"] < 0:
        raise ValueError("warmup_requests must be nonnegative")
    if workload["range_size"] > workload["object_size"]:
        raise ValueError("range_size must not exceed object_size")
    if workload["performance_model"] not in ("objbench", "operations"):
        raise ValueError("performance_model must be objbench or operations")
    from .objbench import normalize
    workload["objbench"] = normalize(workload.get("objbench", {}))
    registry = build_registry(config)
    if "compatibility_suites" not in workload:
        profile = workload.setdefault("profile", "normal")
        if not isinstance(profile, str) or profile not in registry.profiles:
            raise ValueError("unknown profile; registered: " + ", ".join(registry.profiles))
        workload["compatibility_suites"] = list(registry.profiles[profile].suites)
    if type(workload["list_objects"]) is not int or not 1001 <= workload["list_objects"] <= 10000:
        raise ValueError("list_objects must be an integer between 1001 and 10000")
    for field, choices, default in (("operations", registry.operations, None),
                                    ("compatibility_suites", registry.suites, ["baseline"])):
        selected = workload.setdefault(field, default)
        if not isinstance(selected, list) or not selected or any(not isinstance(name, str) or name not in choices for name in selected):
            raise ValueError(f"{field} must select registered names: {', '.join(choices)}")
        if len(set(selected)) != len(selected):
            raise ValueError(f"duplicate {field}")
    # Explicit ceilings prevent accidental unbounded subprocess fanout or allocations.
    if workload["concurrency"] > 128 or max(workload["object_size"], workload["part_size"], workload["large_object_size"]) > 256 * 1024 * 1024:
        raise ValueError("limit concurrency to 128 and object/part size to 256 MiB")
    if type(workload["seed"]) is not int:
        raise ValueError("seed must be an integer")
    config["workload"] = workload
    target_ids = {t["id"] for t in targets}
    for pair in config.get("comparisons", []):
        if pair.get("s3") not in target_ids or pair.get("native") not in target_ids:
            raise ValueError("comparison references unknown targets")
        for key in ("min_samples", "min_throughput_ratio", "max_p95_ratio"):
            if type(pair.get(key)) not in (int, float) or not math.isfinite(pair[key]) or pair[key] <= 0:
                raise ValueError(f"comparison requires finite positive {key}")
        if type(pair["min_samples"]) is not int:
            raise ValueError("comparison min_samples must be an integer")
    return config


def coverage(config, registry):
    """Publish the selected case inventory before execution, including explicit scope gaps."""
    from .suites import CATALOG_VERSION
    workload = config["workload"]
    selected = workload["compatibility_suites"]
    profile = registry.profiles.get(workload.get("profile"))
    return {"catalog_version": CATALOG_VERSION, "profile": profile.name if profile else "custom",
            "description": profile.description if profile else "自选 Suite；不对应内置档位",
            "suites": selected,
            "catalog": [{**case, "suite": name} for name in selected for case in registry.suites[name].catalog],
            "uncatalogued_suites": [name for name in selected if not registry.suites[name].catalog],
            "exclusions": list(profile.exclusions) if profile else ["自选 Suite 未声明档位覆盖范围"]}


def redactor(config):
    secrets = set()
    for name, value in os.environ.items():
        if len(value) >= 4 and any(w in name.upper() for w in ("SECRET", "TOKEN", "PASSWORD", "ACCESS_KEY")):
            secrets.add(value)
    def redact(text):
        for value in sorted(secrets, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return text
    return redact



def cleanup_objects(worker, prefix):
    """Delete only objects returned inside this run prefix, checking the entire listing before mutations."""
    keys = require_ok(worker.call({"op": "list", "prefix": prefix})).get("keys", [])
    if any(not isinstance(key, str) or not key.startswith(prefix) for key in keys):
        raise ValueError("refusing cleanup outside this run prefix")
    for key in keys:
        require_ok(worker.call({"op": "delete", "key": key}))


def run(config, output_dir):
    registry = build_registry(config)
    output_dir.mkdir(parents=True, exist_ok=False)
    redact = redactor(config)
    report = {"schema_version": 1, "run_id": uuid.uuid4().hex,
              "started_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"system": platform.system(), "release": platform.release(),
                              "machine": platform.machine(), "logical_cpus": os.cpu_count(),
                              "python": platform.python_version()},
              "workload": config["workload"], "targets": [], "cases": [], "samples": [],
              "batches": [], "external": [], "summary": [], "comparisons": [],
              "plugins": config.get("plugins", []),
              "coverage": coverage(config, registry),
              "operation_metrics": {name: registry.operations[name].throughput_metric for name in config["workload"]["operations"]}}
    workload = config["workload"]
    if workload["performance_model"] == "objbench":
        from .objbench import METRICS
        report["operation_metrics"] = METRICS.copy()
    payload = random.Random(workload["seed"]).randbytes(workload["object_size"])
    prefix = "osv/" + report["run_id"] + "/"
    pools = {}
    with tempfile.TemporaryDirectory(prefix="osv-payload-") as temporary:
        root = Path(temporary)
        body = root / "payload.bin"
        body.write_bytes(payload)
        try:
            for target in config.get("targets", []):
                ident = target["id"]
                report["targets"].append({k: target[k] for k in ("id", "provider", "language", "api", "sdk", "sdk_version")})
                try:
                    workers = []
                    pools[ident] = workers
                    for _ in range(workload["concurrency"]):
                        workers.append(Worker(target, workload["timeout_seconds"]))
                    report["targets"][-1]["worker_info"] = workers[0].info
                    directory = root / ident
                    directory.mkdir()
                    required = {"put", "list", "delete"}  # seed and cleanup
                    if workload["performance_model"]=="objbench":
                        required.update({"put","get","head","list","delete"})
                    else:
                        for name in workload["operations"]:
                            required.update(registry.operations[name].required_operations)
                    for name in workload["compatibility_suites"]:
                        if not registry.suites[name].case_capabilities:
                            required.update(registry.suites[name].required_operations)
                    for worker in workers:
                        missing = required - worker.capabilities
                        if missing:
                            raise RuntimeError("unsupported worker operations: " + ", ".join(sorted(missing)))
                    ctx = Context(workers[0], prefix+ident+"/", directory, payload, body,
                                  directory/"read.bin", workload)
                    for name in workload["compatibility_suites"]:
                        cases = registry.suites[name].execute(ctx)
                        if not isinstance(cases, list) or not cases or any(
                            not isinstance(case, dict) or not isinstance(case.get("case"), str)
                            or case.get("status") not in ("pass", "fail", "observation", "not_run") for case in cases):
                            raise ValueError("suite must return nonempty valid case results")
                        catalog = registry.suites[name].catalog
                        actual_names = [case["case"] for case in cases]
                        if len(set(actual_names)) != len(actual_names):
                            raise ValueError("suite returned duplicate case names")
                        if catalog and set(actual_names) != {case["case"] for case in catalog}:
                            raise ValueError("suite results differ from declared case catalog")
                        if catalog:
                            metadata = {case["case"]: case for case in catalog}
                            for result in cases:
                                expected_gate=metadata[result["case"]]["gate"]
                                if result["status"] == "observation" and expected_gate:
                                    raise ValueError("required gate cannot be downgraded to observation")
                                if "gate" in result and result["gate"]!=expected_gate:
                                    raise ValueError("result gate differs from declared catalog")
                                result["gate"]=expected_gate
                        else:
                            for result in cases:result["gate"]=True
                        if any(case["status"]=="not_run" and not case.get("reason") for case in cases):
                            raise ValueError("not_run requires an explicit reason")
                        if not any(case["status"] in ("pass", "fail", "not_run") for case in cases):
                            raise ValueError("suite must return at least one semantic gate")
                        report["cases"].extend({**case, "target": ident, "suite": name} for case in cases)
                    # Compatibility data must not change the performance dataset between profiles.
                    cleanup_objects(workers[0], prefix+ident+"/")
                    if workload["performance_model"] == "operations":
                        require_ok(workers[0].call({"op": "put", "key": prefix+ident+"/perf", "body_path": str(body)}))
                except Exception as error:
                    report["cases"].append({"target": ident, "case": "setup", "status": "fail", "error": str(error)})
            active = [t for t in config.get("targets", []) if pools.get(t["id"])
                      and not any(blocks_measurement(c) for c in report["cases"] if c["target"] == t["id"])]
            for phase in ("warmup", "measure"):
                rounds = (workload["objbench"]["warmup_rounds"] if workload["performance_model"] == "objbench" else 1) if phase == "warmup" else workload["rounds"]
                count = workload["warmup_requests"] if phase == "warmup" else workload["requests"]
                for round_index in range(rounds):
                    order = list(active)
                    random.Random(workload["seed"] + round_index).shuffle(order)
                    for target in order:
                        ident = target["id"]
                        if workload["performance_model"] == "objbench":
                            from .objbench import run_objbench
                            ctx = Context(pools[ident][0], prefix+ident+"/", root/ident, payload, body,
                                          root/ident/"read.bin", workload)
                            run_objbench(ctx,pools[ident],report,ident,[(phase,round_index)])
                            continue
                        for op in workload["operations"]:
                            workers = pools[ident]
                            def lane(slot):
                                samples = []
                                worker = workers[slot]
                                output = root / ident / f"slot-{slot}.bin"
                                for index in range(slot, count, len(workers)):
                                    operation = registry.operations[op]
                                    ctx = Context(worker, prefix+ident+"/", root/ident, payload, body,
                                                  output, workload, round_index, index)
                                    row = {"target": ident, "op": op, "phase": phase,
                                           "round": round_index, "index": index, "ok": False,
                                           "elapsed_ns": 0, "bytes": 0}
                                    try:
                                        request = operation.prepare(ctx)
                                        if request.get("op") not in operation.required_operations:
                                            raise ValueError("operation sent an undeclared worker request")
                                        result = worker.call(request)
                                        row["elapsed_ns"] = result["elapsed_ns"]
                                        require_ok(result)
                                        verified_bytes = operation.verify(ctx, request, result)
                                        if type(verified_bytes) is not int or verified_bytes < 0:
                                            raise ValueError("verifier must return a nonnegative byte count")
                                        row["bytes"] = verified_bytes
                                        row["ok"] = True
                                    except Exception as error:
                                        row["error"] = str(error)
                                    samples.append(row)
                                return samples
                            started = time.monotonic()
                            with ThreadPoolExecutor(max_workers=len(workers)) as executor:
                                for samples in executor.map(lane, range(len(workers))):
                                    report["samples"].extend(samples)
                            report["batches"].append({"target": ident, "op": op, "phase": phase,
                                                    "round": round_index, "wall_seconds": time.monotonic()-started})
        finally:
            for ident, workers in pools.items():
                if workers:
                    try:
                        cleanup_objects(workers[0], prefix+ident+"/")
                    except Exception as error:
                        report["cases"].append({"target": ident, "case": "cleanup", "status": "fail", "error": str(error)})
                for worker in workers:
                    worker.close()
    for spec in config.get("external", []):
        try:
            report["external"].append(invoke_external(spec, output_dir, redact))
        except Exception as error:
            report["external"].append({"id": spec["id"], "tool": spec["tool"], "exit_code": -1,
                                      "error": redact(str(error)), "stdout": "unavailable"})
    from .alignment import audit
    alignment=audit(config,registry)
    report["upstream_alignment"]={"summary":alignment["summary"],"details":"alignment.json"}
    (output_dir/"alignment.json").write_text(json.dumps(alignment,indent=2,ensure_ascii=False)+"\n")
    report["summary"] = summarize(report["samples"], report["batches"])
    # Observations are not required semantic gates.
    gates = [c for c in report["cases"] if c["status"] != "observation" and not (c["status"]=="not_run" and not c.get("gate",True))]
    report["comparisons"] = comparisons(config, gates, report["summary"], report["operation_metrics"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report = json.loads(redact(json.dumps(report)))
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n")
    (output_dir / "report.md").write_text(markdown(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "catalog", "alignment", "run"):
        command = commands.add_parser(name)
        command.add_argument("manifest")
        command.add_argument("--profile", help="select registered test profile; overrides manifest suite selection")
        if name == "run":
            command.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.manifest, args.profile)
        if args.command == "alignment":
            from .alignment import audit
            print(json.dumps(audit(config,build_registry(config)),indent=2,ensure_ascii=False))
            return
        if args.command == "catalog":
            print(json.dumps(coverage(config, build_registry(config)), indent=2, ensure_ascii=False))
            return
        if args.command == "check":
            registry = build_registry(config)
            selected = coverage(config, registry)
            print(f"Manifest valid. Profile={selected['profile']}; suites={','.join(selected['suites'])}; "
                  f"catalogued cases={len(selected['catalog'])}. No SDK or cloud operation executed.")
            return
        report = run(config, Path(args.output))
        print(Path(args.output) / "report.md")
        failed = any(blocks_measurement(c) for c in report["cases"])
        failed |= any(not s["ok"] for s in report["samples"])
        failed |= any(e["exit_code"] != 0 or e.get("timed_out") for e in report["external"])
        failed |= any(c["status"] != "within_budget" for c in report["comparisons"])
        if failed:
            sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")
