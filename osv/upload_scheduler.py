"""Bounded round-robin multipart dispatch using only real SDK requests."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time

from .benchmark import WriteWindow, write_seconds, MAX_WRITE_BYTES
from .scenarios import require_ok


def upload_group(ctx, workers, report, target, phase, round_index, size, label, inputs):
    """Keep at most one SDK task per client in flight, rotating ready objects instead of flooding a shared executor."""
    from .io_bench import sample_call, pool_for, fail
    pool = pool_for(workers)
    active, states, futures = deque(), [], {}
    pending_verification = []
    minimum = write_seconds(ctx, phase)
    maximum = max(len(inputs), min(4096, MAX_WRITE_BYTES//size)) if minimum else len(inputs)
    failed = False

    def admit():
        index = len(states)
        original_key, paths, expected_hash = inputs[index % len(inputs)]
        key = original_key.rsplit("/", 1)[0]+"/"+str(index)
        now = time.monotonic_ns()
        state = {"key": key, "paths": paths, "hash": expected_hash, "stage": "create",
            "upload_id": None, "next_part": 0, "inflight": 0, "etags": {}, "part_rows": [],
            "ready_ns": now, "started_ns": now,
            "total": {"target": target, "op": "multipart_total_"+label, "phase": phase, "round": round_index,
                "key": key, "ok": False, "bytes": 0, "elapsed_ns": 0, "timing_scope": "upload_wall"}}
        states.append(state)
        active.append(state)

    def request(state):
        stage = state["stage"]
        base = {"key": state["key"]}
        if stage == "create" and not state["inflight"]:
            return "create", {"op": "mp_create", **base}, {}
        if stage == "part" and state["next_part"] < len(state["paths"]):
            index = state["next_part"]
            state["next_part"] += 1
            path = state["paths"][index]
            return "part", {"op": "mp_part", **base, "upload_id": state["upload_id"],
                "part_number": index+1, "body_path": str(path)}, {"part_number": index+1}
        if stage == "part" and not state["inflight"] and len(state["etags"]) == len(state["paths"]):
            state["stage"] = "complete"
            state["ready_ns"] = time.monotonic_ns()
            return "complete", {"op": "mp_complete", **base, "upload_id": state["upload_id"],
                "parts": [{"part_number": i, "etag": state["etags"][i]} for i in sorted(state["etags"])]}, {}
        return None

    try:
        with WriteWindow(workers, minimum) as window:
            with ThreadPoolExecutor(max_workers=len(workers)) as executor:
                while True:
                    while len(active) < min(len(inputs), len(workers)) and (len(states) < len(inputs) or
                            (not failed and len(states) < maximum and time.monotonic_ns() < window.deadline_ns)):
                        admit()
                    while len(futures) < len(workers):
                        choice = None
                        for _ in range(len(active)):
                            state = active.popleft()
                            active.append(state)
                            planned = request(state)
                            if planned:
                                choice = state, planned
                                break
                        if choice is None:
                            break
                        state, (name, wire, extra) = choice
                        submitted = time.monotonic_ns()
                        future = executor.submit(sample_call, pool, wire, target, "multipart_"+name+"_"+label,
                            phase, round_index, key=state["key"], submitted_ns=submitted,
                            ready_ns=state["ready_ns"], **extra)
                        state["inflight"] += 1
                        futures[future] = (state, name, wire)
                    if not futures:
                        if active:
                            raise RuntimeError("multipart scheduler stalled with unfinished objects")
                        break
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        state, name, wire = futures.pop(future)
                        row, response, _, _ = future.result()
                        state["inflight"] -= 1
                        if name == "part":
                            row["bytes"] = state["paths"][wire["part_number"]-1].stat().st_size if row["ok"] else 0
                            state["part_rows"].append(row)
                        report["samples"].append(row)
                        if not row["ok"]:
                            failed = True
                            state["stage"] = "failed"
                            state["error"] = row["error"]
                        elif name == "create":
                            state["upload_id"] = response["upload_id"]
                            state["stage"] = "part"
                            state["ready_ns"] = time.monotonic_ns()
                        elif name == "part":
                            state["etags"][wire["part_number"]] = response["etag"]
                        elif name == "complete":
                            state["upload_id"] = None
                            state["stage"] = "done"
                            pending_verification.append((state["key"], state["hash"], state["total"], state["part_rows"]))
                        if state["stage"] in ("failed", "done") and not state["inflight"]:
                            state["total"]["elapsed_ns"] = time.monotonic_ns()-state["started_ns"]
                            if state["stage"] == "failed":
                                fail(state["total"], state["error"])
                                for part in state["part_rows"]:
                                    fail(part, "multipart object was not verified: "+state["error"])
                            report["samples"].append(state["total"])
                            active.remove(state)
    finally:
        # Aborts never compete with live uploads or enter successful upload throughput.
        for state in states:
            if state["upload_id"] is not None:
                try:
                    require_ok(workers[0].call({"op": "mp_abort", "key": state["key"], "upload_id": state["upload_id"]}))
                except Exception as error:
                    report["execution_errors"].append({"target": target, "phase": "multipart_abort", "error": str(error)})
    batch = window.batch(target=target, op="multipart_total_"+label, phase=phase, round=round_index)
    batch.update(objects=len(states), scheduler="bounded_round_robin", maximum_inflight=len(workers))
    return pending_verification, [s["key"] for s in states], batch
