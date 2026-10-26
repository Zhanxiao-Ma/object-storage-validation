"""One manifest, real SDK subprocesses, reproducible payloads and explicit evidence."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import platform
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from .worker import Worker, invoke_external
from .scenarios import Context, build_registry, require_ok
from .report import summarize, comparisons, markdown


def load_config(path, profile_override=None, mode_override=None):
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
    if mode_override is not None:
        workload = {**workload, "mode": mode_override}
    defaults = {"mode": "functional", "object_size": 8 * 1024 * 1024, "range_size": 64 * 1024,
                "requests": 32, "concurrency": 4, "warmup_requests": 4,
                "rounds": 8, "part_size": 5 * 1024 * 1024, "timeout_seconds": 60,
                "seed": 1729, "list_objects": 1001, "large_object_size": 256*1024*1024, "operations": ["get", "range", "put", "head", "list"]}
    workload = {**defaults, **workload}
    if workload["mode"] not in ("functional", "performance", "all"):
        raise ValueError("mode must be functional, performance or all")
    for key in ("object_size", "range_size", "requests", "concurrency", "rounds", "part_size", "timeout_seconds", "large_object_size"):
        if type(workload[key]) is not int or workload[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if type(workload["warmup_requests"]) is not int or workload["warmup_requests"] < 0:
        raise ValueError("warmup_requests must be nonnegative")
    if workload["range_size"] > workload["object_size"]:
        raise ValueError("range_size must not exceed object_size")
    if "performance_model" in workload:
        raise ValueError("performance_model has been removed; delete it to run all performance cases")
    from .objbench import normalize
    workload["objbench"] = normalize(workload.get("objbench", {}))
    from .io_bench import normalize as normalize_io
    for model in ("read", "multipart"):
        workload[model] = normalize_io(model, workload.get(model, {}))
    from .file_bench import normalize as normalize_files
    workload["files"] = normalize_files(workload.get("files", {}))
    if workload["mode"] != "functional" and workload["concurrency"] * max(*workload["files"]["sizes"], *workload["files"]["multipart_sizes"],workload["files"]["range_object_size"]) > 1024**3:
        raise ValueError("files concurrent data exceeds 1 GiB; reduce concurrency or file sizes")
    if "identities" in workload:
        if not isinstance(workload["identities"],dict):raise ValueError("identities must be a metadata object")
        for name,metadata in workload["identities"].items():
            if not isinstance(name,str) or not name or not isinstance(metadata,dict):raise ValueError("identity metadata must be named objects")
            for field in ("canonical_id","display_name","email","user_id"):
                if metadata.get(field) is not None and not isinstance(metadata[field],str):raise ValueError("identity metadata fields must be strings or null")
    if workload.get("kms_key_id") is not None and not isinstance(workload["kms_key_id"],str):raise ValueError("kms_key_id must be a string or null")
    if workload.get("secondary_kms_key_id") is not None and not isinstance(workload["secondary_kms_key_id"],str):raise ValueError("secondary_kms_key_id must be a string or null")
    if "storage_classes" in workload and (not isinstance(workload["storage_classes"],list) or not all(isinstance(value,str) and value for value in workload["storage_classes"])):raise ValueError("storage_classes must be a list of nonempty strings")
    for interval_field in ("lifecycle_debug_interval_seconds","restore_debug_interval_seconds","restore_processor_period_seconds"):
        if interval_field not in workload:continue
        value=workload[interval_field]
        try:valid_interval=type(value) in (int,float) and math.isfinite(value) and value>0
        except OverflowError:valid_interval=False
        if not valid_interval:raise ValueError(interval_field+" must be finite and positive")
    for field in ("ceph_logging_wire_extensions","sns_mutation_authorized","isolated_account_mutation_authorized"):
        workload.setdefault(field,False)
        if type(workload[field]) is not bool:
            raise ValueError(field+" must be boolean")
    if "account_id" in workload and (not isinstance(workload["account_id"],str) or not workload["account_id"]):
        raise ValueError("account_id must be a nonempty string")
    workload.setdefault("iam_mutation_authorized",False)
    if type(workload["iam_mutation_authorized"]) is not bool:
        raise ValueError("iam_mutation_authorized must be boolean")
    for field in ("iam_alt_user_name","iam_root_account_id","iam_root_user_id","iam_alt_root_account_id","iam_alt_root_user_id","iam_alt_root_email"):
        if field in workload and (not isinstance(workload[field],str) or not workload[field]):
            raise ValueError(field+" must be a nonempty string")
    if "object_lock_cleanup_authorized" in workload and type(workload["object_lock_cleanup_authorized"]) is not bool:
        raise ValueError("object_lock_cleanup_authorized must be boolean")
    if "ceph_literal_bucket_names_authorized" in workload and type(workload["ceph_literal_bucket_names_authorized"]) is not bool:
        raise ValueError("ceph_literal_bucket_names_authorized must be boolean")
    if "tenant_name" in workload and (not isinstance(workload["tenant_name"],str) or not workload["tenant_name"]):
        raise ValueError("tenant_name must be a nonempty string")
    if "ceph_api_name" in workload and (not isinstance(workload["ceph_api_name"],str) or not workload["ceph_api_name"]):
        raise ValueError("ceph_api_name must be a nonempty string")
    if "sts_web_identity" in workload:
        settings=workload["sts_web_identity"]
        allowed={"realm","aud","sub","azp","thumbprint","token_env","user_token_env"}
        if not isinstance(settings,dict) or set(settings)-allowed:
            raise ValueError("sts_web_identity only accepts realm/aud/sub/azp/thumbprint and token environment names")
        for name,value in settings.items():
            if not isinstance(value,str) or not value:
                raise ValueError("sts_web_identity settings must be nonempty strings")
            if name.endswith("_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",value):
                raise ValueError("sts_web_identity token settings must name environment variables")
    if "cloud_lifecycle" in workload:
        settings=workload["cloud_lifecycle"]
        strings={"storage_class","regular_storage_class","retain_head_object","target_path","target_storage_class","target_by_bucket_prefix","allow_read_through"}
        if not isinstance(settings,dict) or set(settings)-(strings|{"target_by_bucket","read_through_days"}):raise ValueError("cloud_lifecycle contains invalid settings")
        for field in strings:
            if field in settings and (not isinstance(settings[field],str) or not settings[field]):raise ValueError("cloud_lifecycle text settings must be nonempty strings")
        if "target_by_bucket_prefix" in settings:
            template=settings["target_by_bucket_prefix"]
            rest=template
            for token in ("${bucket}","${storage_class}","${zonegroup}"):rest=rest.replace(token,"")
            if template.count("${bucket}")!=1 or "${" in rest:raise ValueError("cloud_lifecycle.target_by_bucket_prefix requires exactly one ${bucket} and only known placeholders")
        if "target_by_bucket" in settings and type(settings["target_by_bucket"]) is not bool:raise ValueError("cloud_lifecycle.target_by_bucket must be boolean")
        if "read_through_days" in settings and (type(settings["read_through_days"]) is not int or settings["read_through_days"]<0):raise ValueError("cloud_lifecycle.read_through_days must be a nonnegative integer")
    if "ceph_http" in workload:
        settings=workload["ceph_http"]
        if not isinstance(settings,dict) or set(settings)-{"endpoint","addressing_style","access_key_env","secret_key_env"}:
            raise ValueError("ceph_http must be an object with endpoint, addressing_style and optional credential environment names")
        endpoint=settings.get("endpoint")
        if not isinstance(endpoint,str):raise ValueError("ceph_http.endpoint must be an explicit HTTP endpoint")
        parsed=urlsplit(endpoint)
        if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("ceph_http.endpoint must be HTTP(S), without credentials, query or fragment")
        if parsed.port is not None and not 1<=parsed.port<=65535:raise ValueError("ceph_http.endpoint port must be between 1 and 65535")
        if settings.get("addressing_style") not in ("path","virtual"):raise ValueError("ceph_http.addressing_style must be path or virtual")
        for field in ("access_key_env","secret_key_env"):
            if field in settings and (not isinstance(settings[field],str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",settings[field])):
                raise ValueError("ceph_http credential references must name environment variables")
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
    workload["operations"] = list(dict.fromkeys(["get", "range", "put", "head", "list", *workload["operations"]]))
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
    selected = [] if workload["mode"] == "performance" else workload["compatibility_suites"]
    profile = registry.profiles.get(workload.get("profile"))
    return {"catalog_version": CATALOG_VERSION, "profile": profile.name if profile else "custom",
            "description": "本次仅执行性能测试，未选择功能用例" if workload["mode"] == "performance" else profile.description if profile else "自选 Suite；不对应内置档位",
            "suites": selected,
            "catalog": [{**case, "suite": name} for name in selected for case in registry.suites[name].catalog],
            "uncatalogued_suites": [name for name in selected if not registry.suites[name].catalog],
            "exclusions": list(profile.exclusions) if profile else ["自选 Suite 未声明档位覆盖范围"]}


def redactor(config):
    from .secrets import SecretRegistry, current_registry
    registry=current_registry() or SecretRegistry()
    registry.environment(os.environ)
    for target in config.get("targets",[]):
        registry.environment(target.get("env",{}))
    return registry.redact



def cleanup_objects(worker, prefix):
    """Delete only objects returned inside this run prefix, checking the entire listing before mutations."""
    keys = require_ok(worker.call({"op": "list", "prefix": prefix})).get("keys", [])
    if any(not isinstance(key, str) or not key.startswith(prefix) for key in keys):
        raise ValueError("refusing cleanup outside this run prefix")
    for key in keys:
        require_ok(worker.call({"op": "delete", "key": key}))


def run(config, output_dir):
    from .secrets import secret_scope
    with secret_scope():
        return _run(config, output_dir)


def _run(config, output_dir):
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
    from .objbench import METRICS
    report["operation_metrics"] = {"repeat_"+name: metric for name, metric in report["operation_metrics"].items()}
    report["operation_metrics"].update(METRICS)
    report["operation_metrics"]["parallel_put"] = "verified_mib_s"
    payload = random.Random(workload["seed"]).randbytes(workload["object_size"])
    prefix = "osv/" + report["run_id"] + "/"
    functional = workload["mode"] in ("functional", "all")
    performance = workload["mode"] in ("performance", "all")
    report["execution_errors"] = []
    pools = {}
    ready = set()
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="osv-payload-")))
        from .benchmark import performance_workspace, sync_inputs
        perfroot = stack.enter_context(performance_workspace(workload, config.get("targets", []), report)) if performance else root
        perfbody = perfroot / "payload.bin"
        if performance:
            perfbody.write_bytes(payload)
            sync_inputs([perfbody])
        body = root / "payload.bin" if functional else perfbody
        if functional:
            body.write_bytes(payload)
        try:
            for target in config.get("targets", []):
                ident = target["id"]
                report["targets"].append({k: target[k] for k in ("id", "provider", "language", "api", "sdk", "sdk_version")})
                try:
                    workers = []
                    pools[ident] = workers
                    for _ in range(workload["concurrency"] if performance else 1):
                        workers.append(Worker(target, workload["timeout_seconds"]))
                    report["targets"][-1]["worker_info"] = workers[0].info
                    directory = root / ident
                    directory.mkdir()
                    if performance:
                        (perfroot/ident).mkdir()
                except Exception as error:
                    report["execution_errors"].append({"target": ident, "phase": "worker_setup", "error": str(error)})
                    if functional:
                        report["cases"].append({"target": ident, "case": "setup", "status": "fail", "error": str(error)})
                    continue
                if functional:
                    ctx = Context(workers[0], prefix+ident+"/functional/", directory, payload, body,
                                  directory/"read.bin", workload)
                    try:
                        for name in workload["compatibility_suites"]:
                            suite = registry.suites[name]
                            if not suite.case_capabilities:
                                missing = suite.required_operations - workers[0].capabilities
                                if missing:
                                    raise RuntimeError("unsupported worker operations: " + ", ".join(sorted(missing)))
                            cases = registry.suites[name].execute(ctx)
                            if not isinstance(cases, list) or not cases or any(
                                not isinstance(case, dict) or not isinstance(case.get("case"), str)
                                or case.get("status") not in ("pass", "fail", "warning", "not_run") for case in cases):
                                raise ValueError("suite must return nonempty valid case results")
                            catalog = registry.suites[name].catalog
                            actual_names = [case["case"] for case in cases]
                            if len(set(actual_names)) != len(actual_names):
                                raise ValueError("suite returned duplicate case names")
                            if catalog and set(actual_names) != {case["case"] for case in catalog}:
                                raise ValueError("suite results differ from declared case catalog")
                            metadata = {case["case"]: case for case in catalog}
                            for result in cases:
                                if "gate" in result:
                                    raise ValueError("gate classification is no longer supported")
                                severity = metadata.get(result["case"], {}).get("severity", "error")
                                if catalog and result["status"] == "warning" and severity != "warning":
                                    raise ValueError("source assertion cannot be downgraded to warning")
                                if catalog and result.get("severity", severity) != severity:
                                    raise ValueError("result severity differs from source catalog")
                            if any(case["status"]=="not_run" and not case.get("reason") for case in cases):
                                raise ValueError("not_run requires an explicit reason")
                            report["cases"].extend({**case, "target": ident, "suite": name} for case in cases)
                    except Exception as error:
                        report["cases"].append({"target": ident, "case": "setup", "status": "fail", "error": str(error)})
                    try:
                        cleanup_objects(workers[0], ctx.prefix)
                    except Exception as error:
                        report["execution_errors"].append({"target": ident, "phase": "functional_cleanup", "error": str(error)})
                if performance:
                    ready.add(ident)
            from .file_bench import iter_files, inventory
            report["performance_inventory"] = list(inventory(workload)) if performance else []
            for phase in ("warmup", "measure"):
                rounds = int(workload["warmup_requests"] > 0) if phase == "warmup" else workload["rounds"]
                for round_index in range(rounds if performance else 0):
                    from .benchmark import target_order
                    active = []
                    for target in target_order(config.get("targets", []), workload["seed"], round_index):
                        ident = target["id"]
                        if ident not in ready: continue
                        ctx = Context(pools[ident][0], prefix+ident+"/performance/", perfroot/ident, payload, perfbody,
                                      perfroot/ident/"read.bin", workload)
                        active.append((ident, iter_files(ctx,pools[ident],report,ident,phase,round_index)))
                    try:
                        while active:
                            pending=[]
                            for ident, iterator in active:
                                try:
                                    next(iterator);pending.append((ident,iterator))
                                except StopIteration: pass
                                except Exception as error:
                                    report["execution_errors"].append({"target":ident,"phase":"file_workload","error":str(error)})
                            active=pending
                    finally:
                        for _, iterator in active: iterator.close()
                    # Explicit extension operations remain opt-in; built-ins run only in the file matrix.
                    custom=[name for name in workload["operations"] if name not in ("get","range","put","head","list")]
                    for target in target_order(config.get("targets", []),workload["seed"],round_index):
                        ident=target["id"]
                        if ident not in ready or not custom or ident in report.get("performance_stopped",{}):continue
                        try:
                            workers=pools[ident]
                            required=set().union(*(registry.operations[name].required_operations for name in custom))
                            missing=required-workers[0].capabilities
                            if missing:raise RuntimeError("unsupported worker operations: "+", ".join(sorted(missing)))
                            ctx=Context(workers[0],prefix+ident+"/performance/extensions/",perfroot/ident,payload,perfbody,perfroot/ident/"extension.read",workload)
                            require_ok(workers[0].call({"op":"put","key":ctx.prefix+"perf","body_path":str(perfbody)}))
                            list(iter_operations(ctx,workers,report,ident,registry,{**workload,"operations":custom},phase,round_index,
                                                workload["warmup_requests"] if phase=="warmup" else workload["requests"]))
                        except Exception as error:
                            report["execution_errors"].append({"target":ident,"phase":"performance_setup","error":str(error)})
        finally:
            for ident, workers in pools.items():
                if workers:
                    try:
                        if ident in report.get("performance_stopped",{}):
                            # A fresh client is used only for cleanup, never to hide failed measurements.
                            with_cleanup=Worker(workers[0].target,workload['timeout_seconds'])
                            try:cleanup_objects(with_cleanup,prefix+ident+"/")
                            finally:with_cleanup.close()
                        else:cleanup_objects(workers[0], prefix+ident+"/")
                    except Exception as error:
                        report["execution_errors"].append({"target": ident, "phase": "cleanup", "error": str(error)})
                for worker in workers:
                    worker.close()
    for spec in config.get("external", []):
        try:
            report["external"].append(invoke_external(spec, output_dir, redact))
        except Exception as error:
            report["external"].append({"id": spec["id"], "tool": spec["tool"], "exit_code": -1,
                                      "error": redact(str(error)), "stdout": "unavailable"})
    if functional:
        from .alignment import audit
        alignment=audit(config,registry)
        report["upstream_alignment"]={"summary":alignment["summary"],"details":"alignment.json"}
        (output_dir/"alignment.json").write_text(json.dumps(alignment,indent=2,ensure_ascii=False)+"\n")
    report["summary"] = summarize(report["samples"], report["batches"])
    report["comparisons"] = comparisons(config, report["cases"], report["summary"], report["operation_metrics"]) if performance else []
    for pair in report['comparisons']:
        if any(t in report.get('performance_stopped',{}) for t in (pair.get('s3'),pair.get('native'))):
            pair.update(status='inconclusive',reason='SDK worker terminated; performance run incomplete')
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
            command.add_argument("--mode", choices=("functional", "performance", "all"),
                                 help="run functional checks, performance measurements, or both independently; default: functional")
    args = parser.parse_args()
    try:
        config = load_config(args.manifest, args.profile, getattr(args, "mode", None))
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
        failed = any(c["status"] == "fail" for c in report["cases"])
        failed |= bool(report["execution_errors"])
        failed |= any(not s["ok"] for s in report["samples"])
        failed |= any(e["exit_code"] != 0 or e.get("timed_out") for e in report["external"])
        failed |= any(c["status"] != "within_budget" for c in report["comparisons"])
        if failed:
            sys.exit(1)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        parser.exit(2, f"error: {error}\n")

def iter_operations(ctx, workers, report, ident, registry, workload, phase, round_index, count):
    """Yield after each measured case to interleave targets."""
    plan = [(op, "repeat_"+op, False) for op in workload["operations"]]
    if "put" in workload["operations"]:
        plan.insert(next(i for i, entry in enumerate(plan) if entry[0] == "put")+1,
                    ("put", "parallel_put", True))
    for op, case_name, independent in plan:
        directory, perf_prefix, payload, body = ctx.directory, ctx.prefix, ctx.payload, ctx.body
        from .benchmark import WriteWindow, write_seconds
        minimum = write_seconds(ctx, phase) if op == "put" else 0
        expected_hash = hashlib.sha256(payload).digest() if op == "put" else None
        def lane(slot):
            samples = []
            worker = workers[slot]
            output = directory / f"slot-{slot}.bin"
            index = slot
            while index < count or (minimum and time.monotonic_ns() < window.deadline_ns and index < 100000):
                operation = registry.operations[op]
                ctx = Context(worker, perf_prefix, directory, payload, body,
                              output, workload, round_index, index)
                row = {"target": ident, "op": case_name, "phase": phase,
                       "round": round_index, "index": index, "ok": False,
                       "elapsed_ns": 0, "bytes": 0}
                try:
                    request = operation.prepare(ctx)
                    if request.get("op") not in operation.required_operations:
                        raise ValueError("operation sent an undeclared worker request")
                    if independent:
                        request["key"] = perf_prefix+f"perf-lane-{slot}"
                    if op == "put":
                        row.update(key=request["key"], lane=slot,
                                   key_mode="per_lane" if independent else "shared")
                    row["request_start_ns"] = time.monotonic_ns()
                    try:
                        result = worker.call(request)
                    finally:
                        row["request_end_ns"] = time.monotonic_ns()
                        row["rpc_wall_ns"] = row["request_end_ns"]-row["request_start_ns"]
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
                index += len(workers)
                if not row["ok"] and index >= count:
                    break
            return samples
        rows = []
        with WriteWindow(workers, minimum) as window:
            with ThreadPoolExecutor(max_workers=len(workers)) as executor:
                for samples in executor.map(lane, range(len(workers))):
                    rows.extend(samples)
        batch = window.batch(target=ident, op=case_name, phase=phase, round=round_index)
        if op == "put":
            # Overwrites retain only final state: verify each used key outside upload timing.
            keys = {r["key"]: r["lane"] for r in rows if "key" in r}
            failures = []
            verified_at = time.monotonic_ns()
            for key, slot in keys.items():
                output = directory/f"overwrite-{slot}.verify"
                try:
                    require_ok(workers[slot].call({"op": "get", "key": key, "output_path": str(output)}))
                    actual = output.read_bytes()
                    if len(actual) != len(payload) or hashlib.sha256(actual).digest() != expected_hash:
                        raise AssertionError("final overwrite length or SHA-256 differs from input")
                except Exception as error:
                    failures.append({"key": key, "error": str(error)})
                    for row in rows:
                        if row.get("key") == key:
                            row.update(ok=False, bytes=0, error="final overwrite readback failed: "+str(error))
                finally:
                    output.unlink(missing_ok=True)
            # Keep the existing shared-key GET/HEAD/LIST dataset unchanged.
            if independent:
                for key, slot in keys.items():
                    try:
                        require_ok(workers[slot].call({"op": "delete", "key": key}))
                    except Exception as error:
                        report.setdefault("execution_errors", []).append(
                            {"target": ident, "phase": "parallel_put_cleanup", "error": str(error)})
            batch.update(key_mode="per_lane" if independent else "shared", active_keys=len(keys),
                         verification={"scope": "final_state_only", "keys": len(keys), "failures": failures,
                                       "outside_upload_timing": True,
                                       "wall_seconds": (time.monotonic_ns()-verified_at)/1e9})
        report["samples"].extend(rows)
        report["batches"].append(batch)
        yield case_name
