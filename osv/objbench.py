"""JuiceFS-shaped multi-object benchmark, executed only through our SDK workers.

The phase/data model follows pinned objbench source. Each object has independent
expected bytes; read-back and final deletion are verified rather than trusting SDK success.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import random
import time

from .scenarios import assert_equal, require_ok

DEFAULTS = {"small_object_size": 128*1024, "small_objects": 100,
            "block_size": 4*1024*1024, "big_object_size": 1024*1024*1024,
            "list_requests_per_lane": 100, "warmup_rounds": 0}
METRICS = {name: "verified_mib_s" if name in ("put", "get", "range") else "verified_ops_s"
           for name in ("smallput", "smallget", "put", "get", "range", "list", "head", "delete")}
METRICS["list"]="verified_objects_s"


def normalize(parameters):
    """Bound dataset sizes independently of concurrency; retain objbench defaults unless explicitly scaled."""
    if not isinstance(parameters, dict) or set(parameters)-set(DEFAULTS):
        raise ValueError("unknown objbench parameters")
    result = {**DEFAULTS, **parameters}
    for name, value in result.items():
        if type(value) is not int or value < (0 if name == "warmup_rounds" else 1):
            raise ValueError(f"invalid objbench {name}")
    if result["warmup_rounds"] > 10 or result["small_objects"] > 10000 or result["list_requests_per_lane"] > 1000:
        raise ValueError("objbench counts exceed safety bounds")
    if max(result["small_object_size"], result["block_size"]) > 256*1024*1024:
        raise ValueError("objbench individual objects must not exceed 256 MiB")
    blocks = math.ceil(result["big_object_size"]/result["block_size"])
    total = blocks*result["block_size"]+result["small_objects"]*result["small_object_size"]
    if blocks > 10000 or total > 16*1024**3:
        raise ValueError("objbench dataset must not exceed 10000 blocks or 16 GiB")
    return result


def _payload(seed, key_index, size):
    # Inputs are reproducible and distinct between keys, exposing cross-key/cached-response mistakes.
    return random.Random(seed+key_index).randbytes(size)


def run_objbench(ctx, workers, report, target, round_plan=None):
    """Run real multi-key producer/consumer phases, preserve each request and verify exact returned data."""
    options = ctx.workload["objbench"]
    block_count = math.ceil(options["big_object_size"]/options["block_size"])
    lanes = min(len(workers), block_count, options["small_objects"])
    small = [(f"small/{n:06d}", options["small_object_size"], n) for n in range(options["small_objects"])]
    big = [(f"block/{n:06d}", options["block_size"], n+options["small_objects"]) for n in range(block_count)]
    # Like objbench, the last block is a full block, so actual bytes round up.
    all_objects = small+big
    expected_keys = None
    report.setdefault("benchmark_datasets", []).append({"target": target, "model":"objbench",
        "small_objects":len(small), "block_objects":len(big), "effective_lanes":lanes,
        "actual_bytes":sum(size for _, size, _ in all_objects),
        "reference_parameters":options, "round_plan":round_plan, "additional_phases":["range"],
        "not_applicable":["chmod", "chown", "chtimes"],
        "not_applicable_reason":"object SDK targets do not expose POSIX filesystem operations"})
    rounds = round_plan if round_plan is not None else [("warmup", n) for n in range(options["warmup_rounds"])] + [("measure", n) for n in range(ctx.workload["rounds"])]
    for phase, round_index in rounds:
        prefix = ctx.prefix+f"objbench/{phase}-{round_index}/"
        expected_keys = {prefix+name for name, _, _ in all_objects}
        current_failure = False
        phases = [("smallput",small), ("smallget",small), ("put",big), ("get",big), ("range",big),
                  ("list",[None]*(lanes*options["list_requests_per_lane"])),
                  ("head",all_objects), ("delete",all_objects)]
        for operation, items in phases:
            def lane(slot):
                rows = []
                worker = workers[slot]
                body = ctx.directory/f"objbench-{slot}.body"
                output = ctx.directory/f"objbench-{slot}.output"
                for index in range(slot, len(items), lanes):
                    row = {"target":target, "op":operation, "phase":phase, "round":round_index,
                           "index":index, "ok":False, "elapsed_ns":0, "bytes":0}
                    try:
                        item = items[index]
                        request = {"op":operation, "prefix":prefix}
                        if item:
                            name, size, identity = item
                            request["key"] = prefix+name
                            row["key"] = request["key"]
                            if operation in ("smallput","put","smallget","get","range"):
                                expected = _payload(ctx.workload["seed"],identity,size)
                        if operation in ("smallput", "put"):
                            body.write_bytes(expected)
                            request.update(op="put", body_path=str(body))
                        elif operation in ("smallget", "get", "range"):
                            request.update(op="get", output_path=str(output))
                            output.unlink(missing_ok=True)
                            if operation == "range":
                                length = min(ctx.workload["range_size"],size)
                                offset = random.Random(ctx.workload["seed"]+index+round_index).randrange(size-length+1)
                                request.update(offset=offset,length=length)
                                expected = expected[offset:offset+length]
                        row.update({name:request[name] for name in ("offset","length") if name in request})
                        result = worker.call(request)
                        row["elapsed_ns"] = result["elapsed_ns"]
                        require_ok(result)
                        if operation in ("smallget", "get", "range"):
                            assert_equal(hashlib.sha256(output.read_bytes()).digest(),hashlib.sha256(expected).digest())
                            row["bytes"] = len(expected)
                        elif operation in ("smallput", "put"):
                            row["bytes"] = len(expected)
                        elif operation == "head":
                            assert_equal(result["size"],size)
                        elif operation == "list":
                            actual = result.get("keys",[])
                            assert_equal(set(actual),expected_keys)
                            assert_equal(len(actual),len(expected_keys))
                            row["objects"] = len(actual)
                        row["ok"] = True
                    except Exception as error:
                        row["error"] = str(error)
                    rows.append(row)
                return rows
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=lanes) as executor:
                batches = list(executor.map(lane,range(lanes)))
            rows = [row for batch in batches for row in batch]
            report["samples"].extend(rows)
            report["batches"].append({"target":target,"op":operation,"phase":phase,"round":round_index,
                                      "wall_seconds":time.monotonic()-start})
            if any(not row["ok"] for row in rows):
                current_failure = True
        try:
            assert_equal(require_ok(workers[0].call({"op":"list","prefix":prefix})).get("keys",[]),[])
        except Exception as error:
            current_failure = True
            report["cases"].append({"target":target,"case":f"objbench_delete_{phase}_{round_index}",
                                     "status":"fail","error":str(error)})
        if current_failure:
            report["cases"].append({"target":target,"case":f"objbench_dataflow_{phase}_{round_index}",
                                     "status":"fail","error":"multi-object workload or its integrity check failed"})
