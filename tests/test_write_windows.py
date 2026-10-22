"""Exercise sustained write production, bounded dispatch, and real input synchronization."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from osv.cli import load_config, run
from osv.benchmark import target_order, performance_workspace
from osv.worker import Worker
import osv.upload_scheduler  # Import consumers before temporarily overriding window policy.
from test_harness import config


class WriteWindowsTest(unittest.TestCase):
    """Use real reference storage as both producer and consumer; change clocks/limits, never storage semantics."""

    def execute(self, directory, **overrides):
        data = config(directory)
        data["workload"].update(mode="performance", rounds=1, warmup_requests=0, **overrides)
        path = directory/"manifest.json"
        path.write_text(json.dumps(data))
        return run(load_config(path), directory/"report")

    def extend(self, seconds):
        stack = ExitStack()
        for module in ("osv.benchmark", "osv.objbench", "osv.upload_scheduler"):
            stack.enter_context(patch(module+".write_seconds", lambda ctx, phase: seconds if phase == "measure" else 0))
        return stack

    def test_workspace_preserves_normal_temp_location_without_forcing_ram(self):
        """Purpose: open the production workspace; expect the normal temporary parent, honest filesystem metadata, and removal after exit without dataset-sized RAM reservation."""
        report = {}
        with performance_workspace({}, [], report) as path:
            self.assertEqual(path.parent, Path(tempfile.gettempdir()))
            self.assertTrue(path.is_dir())
            self.assertIn("filesystem", report["performance_workspace"])
        self.assertFalse(path.exists())

    def test_input_fsync_precedes_actual_upload_and_no_global_sync_occurs(self):
        """Purpose: observe real fsync and worker PUT calls; expect every prepared multi-object source persisted before the first measured upload, exact reads, and no call to host-wide sync."""
        original_sync, original_call = os.fsync, Worker.call
        persisted, first_upload = [], []
        def sync(fd):
            original_sync(fd)
            persisted.append((os.readlink(f"/proc/self/fd/{fd}"), time.monotonic_ns()))
        def call(worker, request):
            if request["op"] == "put" and "/objbench/" in request.get("key", ""):
                first_upload.append(time.monotonic_ns())
            return original_call(worker, request)
        with tempfile.TemporaryDirectory() as tmp, patch("os.fsync", sync), patch.object(Worker, "call", call), patch("os.sync", side_effect=AssertionError("global sync")):
            report = self.execute(Path(tmp))
            self.assertTrue(all(r["ok"] for r in report["samples"]))
            inputs = [at for path, at in persisted if "objbench-input-" in path]
            self.assertEqual(len(inputs), 5)
            self.assertLess(max(inputs), min(first_upload))

    def test_extended_puts_feed_real_reads_list_head_delete_and_verified_bytes(self):
        """Purpose: extend tiny real PUT batches; expect fresh keys, full downstream byte oracles and cleanup for every extra object, a continuous minimum window, and actual IO counter snapshots."""
        original = Worker.call
        inputs, reads = {}, set()
        def call(worker, request):
            key = request.get("key", "")
            if request["op"] == "put" and "/objbench/" in key:
                inputs[key] = Path(request["body_path"]).read_bytes()
            result = original(worker, request)
            if request["op"] == "get" and "/objbench/" in key and "offset" not in request:
                self.assertEqual(Path(request["output_path"]).read_bytes(), inputs[key])
                reads.add(key)
            return result
        with tempfile.TemporaryDirectory() as tmp, self.extend(.04), patch.object(Worker, "call", call):
            directory = Path(tmp)
            report = self.execute(directory)
            self.assertTrue(all(r["ok"] for r in report["samples"]), report["execution_errors"])
            puts = [r for r in report["samples"] if r["op"] == "put"]
            self.assertGreater(len(puts), 3)
            self.assertEqual(len({r["key"] for r in puts}), len(puts))
            self.assertEqual(reads, set(inputs))
            batch = next(b for b in report["batches"] if b["op"] == "put")
            self.assertGreaterEqual(batch["wall_seconds"], .04)
            self.assertTrue(batch["duration_satisfied"])
            self.assertEqual(batch["verification"]["extra_objects"],len(puts)-3)
            self.assertEqual(batch["verification"]["failures"],0)
            # Extra write verification is outside subsequent fixed-size read/metadata measurements.
            self.assertEqual(len([r for r in report["samples"] if r["op"]=="get"]),3)
            self.assertEqual(len([r for r in report["samples"] if r["op"]=="head"]),5)
            self.assertTrue(all(r["objects"]==5 for r in report["samples"] if r["op"]=="list"))
            self.assertGreaterEqual(len(batch["observations"]), 2)
            self.assertFalse([p for p in (directory/"objects").rglob("*") if p.is_file()])

    def test_multipart_dispatch_is_bounded_and_objects_take_turns(self):
        """Purpose: upload two actual multi-part objects with slow real part calls; expect at most two outstanding SDK tasks, both objects served before either third part, measured queue/RPC evidence, exact completed readback and no unfinished uploads."""
        original_call, original_submit = Worker.call, ThreadPoolExecutor.submit
        lock = threading.Lock()
        outstanding = maximum = 0
        parts = []
        def submit(executor, function, *args, **kwargs):
            nonlocal outstanding, maximum
            tracked = function.__name__ == "sample_call" and args[1]["op"].startswith("mp_")
            if tracked:
                with lock:
                    outstanding += 1
                    maximum = max(maximum, outstanding)
            future = original_submit(executor, function, *args, **kwargs)
            if tracked:
                def done(_):
                    nonlocal outstanding
                    with lock:
                        outstanding -= 1
                future.add_done_callback(done)
            return future
        def call(worker, request):
            if request["op"] == "mp_part":
                with lock:
                    parts.append((request["key"], request["part_number"]))
                time.sleep(.005)
            return original_call(worker, request)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Worker, "call", call), patch.object(ThreadPoolExecutor, "submit", submit):
            directory = Path(tmp)
            report = self.execute(directory, multipart={"object_sizes":[10*1024**2+17], "part_sizes":[5*1024**2], "objects":2})
            self.assertTrue(all(r["ok"] for r in report["samples"]), report["execution_errors"])
            self.assertEqual(maximum, 2)
            self.assertEqual(len({key for key, _ in parts[:2]}), 2)
            rows = [r for r in report["samples"] if r["op"].startswith("multipart_part_")]
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(r["rpc_wall_ns"] >= r["elapsed_ns"] and r["scheduler_wait_ns"] >= 0 and r["dispatch_wait_ns"] >= 0 for r in rows))
            self.assertGreater(max(r["scheduler_wait_ns"] for r in rows), 0)
            self.assertFalse([p for p in (directory/"objects").rglob("*") if p.is_file()])

    def test_extended_multipart_failure_aborts_and_stops_extension(self):
        """Purpose: corrupt a genuine completion ETag only on the first extra object; expect a failed total and provisional parts, true AbortMultipartUpload, bounded further work and complete cleanup."""
        original = Worker.call
        aborted = []
        def call(worker, request):
            if request["op"] == "mp_complete" and request["key"].endswith("/1"):
                request = {**request, "parts":[{**request["parts"][0], "etag":"wrong-independent-etag"}]}
            if request["op"] == "mp_abort":
                aborted.append(request["upload_id"])
            return original(worker, request)
        with tempfile.TemporaryDirectory() as tmp, self.extend(.04), patch.object(Worker, "call", call):
            directory = Path(tmp)
            report = self.execute(directory, multipart={"object_sizes":[17], "part_sizes":[5*1024**2], "objects":1})
            totals = [r for r in report["samples"] if r["op"].startswith("multipart_total_")]
            self.assertEqual(len(totals), 2)
            self.assertTrue(totals[0]["ok"])
            self.assertFalse(totals[1]["ok"])
            self.assertEqual(totals[1]["bytes"], 0)
            self.assertEqual(len(aborted), 1)
            self.assertFalse([p for p in (directory/"objects").rglob("*") if p.is_file()])

    def test_safety_limit_marks_short_write_window_instead_of_claiming_sufficient_measurement(self):
        """Purpose: hit the real byte cap with a tiny valid dataset; expect all minimum requested writes/reads to pass while the under-length window is explicitly retained in summary quality evidence."""
        with tempfile.TemporaryDirectory() as tmp, self.extend(.04), patch("osv.objbench.MAX_WRITE_BYTES", 8):
            report = self.execute(Path(tmp))
            row = next(r for r in report["summary"] if r["op"] == "put")
            self.assertEqual(row["attempts"], 3)
            self.assertEqual(row["short_write_rounds"], [0])
            self.assertEqual(row["failures"], 0)

    def test_even_target_cycles_balance_every_pair_and_position(self):
        """Purpose: inspect production schedules for 1/2/3/4/6 targets; expect each target once per round, balanced positions and equal pair precedence across an even cycle or two odd cycles."""
        for count in (1, 2, 3, 4, 6):
            ids = list(range(count))
            cycle = count if count % 2 == 0 else count*2
            orders = [target_order(ids, 1729, index) for index in range(cycle)]
            for order in orders:
                self.assertEqual(set(order), set(ids))
            for a in ids:
                for b in ids:
                    if a < b:
                        self.assertEqual(sum(o.index(a)<o.index(b) for o in orders), cycle//2)
                positions = [o.index(a) for o in orders]
                self.assertEqual([positions.count(i) for i in ids], [cycle//count]*count)

    def test_high_variation_real_io_cannot_produce_a_successful_budget_verdict(self):
        """Purpose: feed two real local IO summaries with one delayed measured round into the production budget evaluator; expect retained ratios but inconclusive quality, even when the configured budget itself is lenient."""
        from osv.report import comparisons
        original = Worker.call
        delayed = False
        def call(worker, request):
            nonlocal delayed
            if worker.target["id"] == "left" and request["op"] == "get" and "/objbench/measure-1/block/" in request.get("key", "") and not delayed:
                delayed = True
                time.sleep(.08)
            return original(worker, request)
        with tempfile.TemporaryDirectory() as tmp, patch.object(Worker, "call", call):
            directory = Path(tmp)
            data = config(directory)
            data["workload"].update(mode="performance", rounds=2, warmup_requests=0)
            target = data["targets"][0]
            data["targets"] = [{**target, "id": "left"}, {**target, "id": "right"}]
            path = directory/"manifest.json"
            path.write_text(json.dumps(data))
            report = run(load_config(path), directory/"report")
            self.assertTrue(all(r["ok"] for r in report["samples"]))
            # Pair labels select the evaluator contract only; all timings/bytes above came from genuine local SDK IO.
            pair_config = {"targets": [{**target, "id":"left", "api":"s3"}, {**target, "id":"right", "api":"native"}],
                "comparisons": [{"s3":"left", "native":"right", "same_storage_and_network_confirmed":True,
                    "min_samples":1, "min_throughput_ratio":.00001, "max_p95_ratio":100000}]}
            verdict = next(r for r in comparisons(pair_config, [], report["summary"], report["operation_metrics"]) if r["op"] == "get")
            self.assertEqual(verdict["status"], "inconclusive")
            self.assertIn("CV > 20%", verdict["reason"])
            self.assertIsNotNone(verdict["s3_native_throughput_ratio"])
