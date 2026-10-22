"""JuiceFS-shaped multi-object benchmark, executed only through our SDK workers.

The phase/data model follows pinned objbench source. Each object has independent
expected bytes; read-back and final deletion are verified rather than trusting SDK success.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import random
import time
import tempfile
import threading
from .benchmark import WriteWindow, write_seconds, MAX_WRITE_BYTES, sync_inputs
from pathlib import Path

from .scenarios import assert_equal, require_ok

DEFAULTS = {"small_object_size": 128*1024, "small_objects": 100,
            "block_size": 4*1024*1024, "big_object_size": 1024*1024*1024,
            "list_requests_per_lane": 1, "warmup_rounds": 0}
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


def iter_objbench(ctx, workers, report, target, round_plan=None):
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
        small_round, big_round = list(small), list(big)
        all_objects = small_round+big_round
        expected_keys = {prefix+name for name, _, _ in all_objects}
        phases = [("smallput",small_round), ("smallget",small_round), ("put",big_round), ("get",big_round), ("range",big_round),
                  ("list",[None]*(lanes*options["list_requests_per_lane"])),
                  ("head",all_objects), ("delete",all_objects)]
        with tempfile.TemporaryDirectory(prefix="objbench-input-", dir=ctx.directory) as staging:
            sources = {}
            for name, size, identity in all_objects:
                path = Path(staging)/str(identity)
                path.write_bytes(_payload(ctx.workload["seed"], identity, size))
                sources[name] = path
            sync_inputs(sources.values())
            yield from _phases(ctx, workers, report, target, phase, round_index, prefix,
                               phases, lanes, sources, expected_keys)


def _phases(ctx, workers, report, target, phase, round_index, prefix, phases, lanes, sources, expected_keys):
    current_failure = False
    writes = {}
    for operation, items in phases:
        # Prepare independent expected lengths/digests before starting the batch clock.
        oracles = {}
        if operation in ("smallget", "get", "range"):
            for index, (name, size, _) in enumerate(items):
                offset = 0
                length = size
                if operation == "range":
                    length = min(ctx.workload["range_size"], size)
                    offset = random.Random(ctx.workload["seed"]+index+round_index).randrange(size-length+1)
                with sources[name].open("rb") as stream:
                    stream.seek(offset)
                    digest = hashlib.sha256(stream.read(length)).digest()
                oracles[name] = (offset, length, digest)
        base_items = list(items)
        upload_hashes = {}
        if operation == "put":
            upload_hashes = {sources[name]: hashlib.sha256(sources[name].read_bytes()).digest() for name, _, _ in base_items}
        minimum = write_seconds(ctx, phase) if operation == "put" else 0
        limit = max(len(items), min(4096, MAX_WRITE_BYTES//items[0][1])) if minimum else len(items)
        stopped = threading.Event()
        def lane(slot):
            rows, added = [], []
            worker = workers[slot]
            output = ctx.directory/f"objbench-{slot}.output"
            index = slot
            while index < len(base_items) or (minimum and not stopped.is_set() and index < limit and time.monotonic_ns() < window.deadline_ns):
                row = {"target":target, "op":operation, "phase":phase, "round":round_index,
                       "index":index, "ok":False, "elapsed_ns":0, "bytes":0}
                try:
                    item = base_items[index % len(base_items)]
                    request = {"op":operation, "prefix":prefix}
                    if item:
                        name, size, identity = item
                        source_name = name
                        if index >= len(base_items):
                            name += f"-write-{index:06d}"
                            added.append(((name, size, identity), sources[source_name]))
                        request["key"] = prefix+name
                        row["key"] = request["key"]
                    if operation in ("smallput", "put"):
                        request.update(op="put", body_path=str(sources[source_name]))
                    elif operation in ("smallget", "get", "range"):
                        request.update(op="get", output_path=str(output))
                        output.unlink(missing_ok=True)
                        offset, length, expected_hash = oracles[name]
                        if operation == "range":
                            request.update(offset=offset,length=length)
                    row.update({name:request[name] for name in ("offset","length") if name in request})
                    row["request_start_ns"] = time.monotonic_ns()
                    try:
                        result = worker.call(request)
                    finally:
                        row["request_end_ns"] = time.monotonic_ns()
                        row["rpc_wall_ns"] = row["request_end_ns"]-row["request_start_ns"]
                    row["elapsed_ns"] = result["elapsed_ns"]
                    require_ok(result)
                    if operation in ("smallget", "get", "range"):
                        actual = output.read_bytes()
                        assert_equal(len(actual), length)
                        assert_equal(hashlib.sha256(actual).digest(), expected_hash)
                        row["bytes"] = length
                    elif operation in ("smallput", "put"):
                        row["bytes"] = size
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
                if not row["ok"]:
                    stopped.set()
                index += lanes
            return rows, added
        with WriteWindow(workers, minimum) as window:
            with ThreadPoolExecutor(max_workers=lanes) as executor:
                batches = list(executor.map(lane,range(lanes)))
        rows = [row for batch, _ in batches for row in batch]
        additions = [entry for _, extra in batches for entry in extra]
        by_key = {r.get("key"): r for r in rows}
        def verify_extra(slot):
            worker = workers[slot]
            output = ctx.directory/f"objbench-extra-{slot}.verify"
            for (name, size, _), path in additions[slot::lanes]:
                key = prefix+name
                row = by_key[key]
                try:
                    if row["ok"]:
                        require_ok(worker.call({"op": "get", "key": key, "output_path": str(output)}))
                        actual = output.read_bytes()
                        assert_equal(len(actual), size)
                        assert_equal(hashlib.sha256(actual).digest(), upload_hashes[path])
                except Exception as error:
                    row.update(ok=False, bytes=0, error="uploaded object readback failed: "+str(error))
                finally:
                    output.unlink(missing_ok=True)
                    try:
                        require_ok(worker.call({"op": "delete", "key": key}))
                    except Exception as error:
                        report["execution_errors"].append({"target": target, "phase": "objbench_extra_cleanup", "error": str(error)})
        verified_at = time.monotonic()
        if additions:
            with ThreadPoolExecutor(max_workers=lanes) as executor:
                list(executor.map(verify_extra, range(lanes)))
        verification = {"extra_objects": len(additions), "wall_seconds": time.monotonic()-verified_at,
                        "failures": sum(not by_key[prefix+item[0]]["ok"] for item, _ in additions),
                        "outside_upload_timing": True}
        if operation in ("smallput", "put"):
            writes.update({row["key"]: row for row in rows if "key" in row})
        elif operation in ("smallget", "get"):
            for row in rows:
                if not row["ok"] and row.get("key") in writes:
                    writes[row["key"]].update(ok=False, bytes=0, error="uploaded object readback failed: "+row["error"])
        report["samples"].extend(rows)
        batch = window.batch(target=target, op=operation, phase=phase, round=round_index)
        if operation == "put":
            batch["verification"] = verification
        report["batches"].append(batch)
        if any(not row["ok"] for row in rows):
            current_failure = True
        yield operation
    try:
        assert_equal(require_ok(workers[0].call({"op":"list","prefix":prefix})).get("keys",[]),[])
    except Exception as error:
        current_failure = True
        report["execution_errors"].append({"target":target,"phase":f"objbench_delete_{phase}_{round_index}",
                                           "error":str(error)})
    if current_failure:
        report["execution_errors"].append({"target":target,"phase":f"objbench_dataflow_{phase}_{round_index}",
                                           "error":"multi-object workload or its integrity check failed"})


def run_objbench(*args, **kwargs):
    """Execute every case; the CLI uses the same iterator to interleave targets."""
    for _ in iter_objbench(*args, **kwargs):
        pass
