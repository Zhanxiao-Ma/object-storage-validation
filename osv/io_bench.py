"""Generic read-pattern and concurrent multipart workloads over real SDK workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import math
import queue
import random
import time

from .scenarios import assert_equal, require_ok

READ_DEFAULTS = {"small_object_sizes": [4096, 65536, 131072], "objects": 8,
                 "object_size": 64*1024**2, "range_sizes": [65536, 1024**2, 4*1024**2],
                 "patterns": ["sequential", "random", "hot"], "warmup_rounds": 0}
MULTIPART_DEFAULTS = {"object_sizes": [32*1024**2, 128*1024**2],
                      "part_sizes": [5*1024**2, 16*1024**2], "objects": 2, "warmup_rounds": 0}


def normalize(model, parameters):
    """Reject unbounded matrices and invalid S3 part sizes before SDK startup."""
    defaults = READ_DEFAULTS if model == "read" else MULTIPART_DEFAULTS
    if not isinstance(parameters, dict) or set(parameters)-set(defaults):
        raise ValueError("unknown " + model + " parameters")
    result = {**defaults, **parameters}
    for name in ("objects", "warmup_rounds"):
        value = result[name]
        if type(value) is not int or not (0 if name == "warmup_rounds" else 1) <= value <= (10 if name == "warmup_rounds" else 64):
            raise ValueError("invalid " + model + " " + name)
    fields = ("small_object_sizes", "range_sizes") if model == "read" else ("object_sizes", "part_sizes")
    for name in fields:
        values = result[name]
        if not isinstance(values, list) or not 1 <= len(values) <= 8 or any(type(v) is not int or not 1 <= v <= 256*1024**2 for v in values) or len(set(values)) != len(values):
            raise ValueError("invalid " + model + " " + name)
    if model == "read":
        size = result["object_size"]
        if type(size) is not int or not 1 <= size <= 256*1024**2 or max(result["range_sizes"]) > size:
            raise ValueError("read range_sizes must fit object_size (at most 256 MiB)")
        patterns = result["patterns"]
        if not isinstance(patterns, list) or not patterns or any(p not in ("sequential", "random", "hot") for p in patterns) or len(set(patterns)) != len(patterns):
            raise ValueError("invalid read patterns")
        total = max(size, max(result["small_object_sizes"])) * result["objects"]
    else:
        if min(result["part_sizes"]) < 5*1024**2:
            raise ValueError("multipart part_sizes must be at least 5 MiB")
        if any(math.ceil(size/part) > 10000 for size in result["object_sizes"] for part in result["part_sizes"]):
            raise ValueError("multipart exceeds 10000 parts")
        total = max(result["object_sizes"]) * result["objects"]
    if total > 2*1024**3:
        raise ValueError(model + " dataset must not exceed 2 GiB")
    return result


def required_operations(model):
    return {"put", "get", "list", "delete"} if model == "read" else {"mp_create", "mp_part", "mp_complete", "mp_abort", "get", "list", "delete"}


@contextmanager
def loan(pool):
    worker = pool.get()
    try:
        yield worker
    finally:
        pool.put(worker)


def pool_for(workers):
    pool = queue.Queue()
    for worker in workers:
        pool.put(worker)
    return pool


def sample_call(pool, request, target, op, phase, round_index, **metadata):
    """Keep worker SDK timing distinct from client-side queueing and verification."""
    row = {"target": target, "op": op, "phase": phase, "round": round_index,
           "ok": False, "bytes": 0, "elapsed_ns": 0, "timing_scope": "sdk", **metadata}
    start = time.monotonic()
    try:
        with loan(pool) as worker:
            result = worker.call(request)
        row["elapsed_ns"] = result["elapsed_ns"]
        require_ok(result)
        row["ok"] = True
        return row, result, start, time.monotonic()
    except Exception as error:
        row["error"] = str(error)
        return row, {}, start, time.monotonic()


def fail(row, error):
    row.update(ok=False, bytes=0, error=str(error))


def run_reads(ctx, workers, report, target, phase, round_index):
    """Measure full small reads and sequential/random/hot-key ranges with exact byte oracles."""
    options = ctx.workload["read"]
    pool = pool_for(workers)
    count = ctx.workload["requests"]
    prefix = ctx.prefix + f"read/{phase}-{round_index}/"
    report.setdefault("io_datasets", []).append({"target": target, "model": "read", "phase": phase,
        "round": round_index, "parameters": options, "concurrency": len(workers),
        "client_layout": "one SDK process/client per lane; hot reads share an object, not a client"})
    specs = [(f"read_small_{size}", size, None, "full") for size in options["small_object_sizes"]]
    specs += [(f"read_{pattern}_{length}", options["object_size"], length, pattern)
              for length in options["range_sizes"] for pattern in options["patterns"]]
    for op, size, length, pattern in specs:
        sources = []
        try:
            for index in range(1 if pattern == "hot" else options["objects"]):
                source = ctx.directory/f"{op}-{index}.source"
                source.write_bytes(random.Random(ctx.workload["seed"]+index).randbytes(size))
                key = prefix+op+f"/{index}"
                require_ok(workers[0].call({"op": "put", "key": key, "body_path": str(source)}))
                sources.append((key, source))
            def read(index):
                key, source = sources[index % len(sources)]
                output = ctx.directory/f"{op}-{index}.read"
                request = {"op": "get", "key": key, "output_path": str(output)}
                offset = 0
                if length is not None:
                    if pattern == "sequential":
                        # Every object advances through adjacent windows; its final window may be shorter.
                        offset = ((index//len(sources)) % math.ceil(size/length))*length
                    else:
                        offset = random.Random(ctx.workload["seed"]+round_index*count+index).randrange(size-length+1)
                    request.update(offset=offset, length=min(length, size-offset))
                row, _, _, _ = sample_call(pool, request, target, op, phase, round_index,
                    key=key, index=index, offset=offset, length=request.get("length", size), pattern=pattern)
                try:
                    if row["ok"]:
                        with source.open("rb") as stream:
                            stream.seek(offset)
                            expected = stream.read(row["length"])
                        actual = output.read_bytes()
                        assert_equal(len(actual), len(expected))
                        assert_equal(hashlib.sha256(actual).digest(), hashlib.sha256(expected).digest())
                        row["bytes"] = len(expected)
                except Exception as error:
                    fail(row, error)
                finally:
                    output.unlink(missing_ok=True)
                return row
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=len(workers)) as executor:
                rows = list(executor.map(read, range(count)))
            report["samples"].extend(rows)
            report["operation_metrics"][op] = "verified_mib_s"
            report["batches"].append({"target": target, "op": op, "phase": phase, "round": round_index,
                                      "wall_seconds": time.monotonic()-started})
        finally:
            for key, source in sources:
                try:
                    require_ok(workers[0].call({"op": "delete", "key": key}))
                finally:
                    source.unlink(missing_ok=True)


def run_multipart(ctx, workers, report, target, phase, round_index):
    """Measure parallel object/part uploads, verify final bytes, and abort every unfinished upload."""
    options = ctx.workload["multipart"]
    pool = pool_for(workers)
    report.setdefault("io_datasets", []).append({"target": target, "model": "multipart", "phase": phase,
        "round": round_index, "parameters": options, "concurrency": len(workers),
        "client_layout": "one SDK process/client per lane; bounded parallel parts across objects",
        "total_timing": "create through complete wall time, excluding input generation and readback; successful bytes require full readback"})
    for size in options["object_sizes"]:
        for part_size in options["part_sizes"]:
            label = f"{size}_{part_size}"
            prefix = ctx.prefix + f"multipart/{phase}-{round_index}/{label}/"
            # Prepare all raw inputs outside measured upload time; retain independent per-object expected hashes.
            inputs = []
            for index in range(options["objects"]):
                paths = []
                digest = hashlib.sha256()
                for number, offset in enumerate(range(0, size, part_size), 1):
                    path = ctx.directory/f"multipart-{index}-{number}.part"
                    data = random.Random(ctx.workload["seed"]+index*10001+number).randbytes(min(part_size, size-offset))
                    digest.update(data)
                    path.write_bytes(data)
                    paths.append(path)
                inputs.append((prefix+str(index), paths, digest.digest()))
            spans = {name: [] for name in ("create", "part", "complete")}
            pending_verification = []
            with ThreadPoolExecutor(max_workers=len(workers)) as parts_executor:
                def upload(entry):
                    key, paths, expected_hash = entry
                    upload_id = None
                    part_rows = []
                    total = {"target": target, "op": "multipart_total_"+label, "phase": phase, "round": round_index,
                             "key": key, "ok": False, "bytes": 0, "elapsed_ns": 0, "timing_scope": "upload_wall"}
                    started = time.monotonic_ns()
                    try:
                        row, result, begin, end = sample_call(pool, {"op": "mp_create", "key": key}, target,
                            "multipart_create_"+label, phase, round_index, key=key)
                        report["samples"].append(row);spans["create"].append((begin, end))
                        if not row["ok"]:
                            raise RuntimeError(row["error"])
                        upload_id = result["upload_id"]
                        def part(number, path):
                            r, response, a, b = sample_call(pool, {"op": "mp_part", "key": key, "upload_id": upload_id,
                                "part_number": number, "body_path": str(path)}, target, "multipart_part_"+label,
                                phase, round_index, key=key, part_number=number)
                            spans["part"].append((a, b))
                            r["bytes"] = path.stat().st_size if r["ok"] else 0
                            return r, response
                        futures = [parts_executor.submit(part, number, path) for number, path in enumerate(paths, 1)]
                        outcomes = [future.result() for future in futures]
                        part_rows = [item[0] for item in outcomes]
                        report["samples"].extend(part_rows)
                        if any(not r["ok"] for r in part_rows):
                            raise RuntimeError("one or more multipart parts failed")
                        completed = [{"part_number": number, "etag": response["etag"]}
                                     for number, (_, response) in enumerate(outcomes, 1)]
                        row, _, begin, end = sample_call(pool, {"op": "mp_complete", "key": key, "upload_id": upload_id,
                            "parts": completed}, target, "multipart_complete_"+label, phase, round_index, key=key)
                        report["samples"].append(row);spans["complete"].append((begin, end))
                        total["elapsed_ns"] = time.monotonic_ns()-started
                        if not row["ok"]:
                            raise RuntimeError(row["error"])
                        upload_id = None
                        pending_verification.append((key, expected_hash, total, part_rows))
                    except Exception as error:
                        fail(total, error)
                        if not total["elapsed_ns"]:
                            total["elapsed_ns"] = time.monotonic_ns()-started
                        for row in part_rows:
                            fail(row, "multipart object was not verified: "+str(error))
                    finally:
                        if upload_id is not None:
                            try:
                                with loan(pool) as worker:
                                    require_ok(worker.call({"op": "mp_abort", "key": key, "upload_id": upload_id}))
                            except Exception as error:
                                report["execution_errors"].append({"target": target, "phase": "multipart_abort", "error": str(error)})
                    report["samples"].append(total)
                started_ns = time.monotonic_ns()
                with ThreadPoolExecutor(max_workers=min(options["objects"], len(workers))) as executor:
                    list(executor.map(upload, inputs))
            finished_ns = time.monotonic_ns()
            upload_wall = (finished_ns-started_ns)/1e9
            # Readback must not consume upload clients or enter upload throughput timing.
            for key, expected_hash, total, part_rows in pending_verification:
                output = ctx.directory/("multipart-"+key.rsplit("/", 1)[1]+".verify")
                try:
                    with loan(pool) as worker:
                        require_ok(worker.call({"op": "get", "key": key, "output_path": str(output)}))
                    with output.open("rb") as stream:
                        observed = hashlib.file_digest(stream, "sha256").digest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).digest()
                    assert_equal(output.stat().st_size, size)
                    assert_equal(observed, expected_hash)
                    total.update(ok=True, bytes=size)
                except Exception as error:
                    fail(total, error)
                    for row in part_rows:
                        fail(row, "multipart object was not verified: "+str(error))
                finally:
                    output.unlink(missing_ok=True)
            for key, paths, _ in inputs:
                try:
                    with loan(pool) as worker:
                        require_ok(worker.call({"op": "delete", "key": key}))
                except Exception as error:
                    report["execution_errors"].append({"target": target, "phase": "multipart_cleanup", "error": str(error)})
                for path in paths:
                    path.unlink(missing_ok=True)
            report["operation_metrics"]["multipart_total_"+label] = "verified_mib_s"
            report["batches"].append({"target": target, "op": "multipart_total_"+label, "phase": phase,
                "round": round_index, "wall_seconds": upload_wall,
                "wall_start_ns": started_ns, "wall_end_ns": finished_ns})
            for name, values in spans.items():
                op = "multipart_"+name+"_"+label
                report["operation_metrics"][op] = "verified_mib_s" if name == "part" else "verified_ops_s"
                if values:
                    report["batches"].append({"target": target, "op": op, "phase": phase, "round": round_index,
                        "wall_seconds": max(end for _, end in values)-min(begin for begin, _ in values)})
