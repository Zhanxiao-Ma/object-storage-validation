"""Verify measured windows and scheduling using real storage producers and consumers."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from osv.cli import load_config, run
from osv.worker import Worker
import osv.objbench as objbench
from test_harness import config


class MeasurementTest(unittest.TestCase):
    """Keep timing assertions tied to actual workload execution and persisted reports."""

    def execute(self, directory, targets=1, rounds=2):
        data = config(directory)
        data["workload"].update(mode="performance", rounds=rounds, warmup_requests=0)
        data["targets"] = [dict(copy.deepcopy(data["targets"][0]), id=f"local-{i}") for i in range(targets)]
        manifest = directory/"manifest.json"
        manifest.write_text(json.dumps(data))
        return run(load_config(manifest), directory/"report")

    def test_prepared_bytes_and_oracles_still_verify_real_reads_outside_generation_window(self):
        """Purpose: observe actual input generation and storage calls; expect one generation per object/round, all before the first upload clock, successful exact GET/Range validation, and removal of staged files."""
        generated = []
        original = objbench._payload
        def observe(seed, identity, size):
            result = original(seed, identity, size)
            generated.append(time.monotonic_ns())
            return result
        with tempfile.TemporaryDirectory() as temporary, patch.object(objbench, "_payload", observe):
            directory = Path(temporary)
            report = self.execute(directory, rounds=1)
            self.assertTrue(all(s["ok"] for s in report["samples"]))
            # Independent manifest: 2 small objects and ceil(17/8) = 3 blocks.
            self.assertEqual(len(generated), 5)
            batches = [b for b in report["batches"] if b["op"] in {"smallput", "smallget", "put", "get", "range"}]
            self.assertLess(max(generated), min(b["wall_start_ns"] for b in batches))
            self.assertTrue(all(s["bytes"] == 8 for s in report["samples"] if s["op"] == "get"))
            self.assertFalse(list(directory.rglob("objbench-input-*")))
            self.assertFalse([p for p in (directory/"objects").rglob("*") if p.is_file()])

    def test_all_targets_finish_the_same_case_before_advancing_and_rotate_each_round(self):
        """Purpose: run three genuine storage targets; expect adjacent same-case batches, a rotated first target each round, exact verified bytes, and successful final cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            report = self.execute(directory, targets=3, rounds=3)
            self.assertEqual(report["execution_errors"], [])
            self.assertTrue(all(s["ok"] for s in report["samples"]))
            first = []
            for index in range(3):
                batches = [b for b in report["batches"] if b["round"] == index]
                order = [b["target"] for b in batches if b["op"] == "smallput"]
                first.append(order[0])
                for start in range(0, len(batches), 3):
                    chunk = batches[start:start+3]
                    self.assertEqual(len(chunk), 3)
                    self.assertEqual(len({b["op"] for b in chunk}), 1)
                    self.assertEqual([b["target"] for b in chunk], order)
            self.assertEqual(len(set(first)), 3)
            self.assertFalse([p for p in (directory/"objects").rglob("*") if p.is_file()])

    def test_multipart_substages_have_latency_but_no_artificial_throughput(self):
        """Purpose: run actual multipart create/part/complete and readback; expect SDK latency/count evidence, no substage throughput denominator or rate, and a real verified total upload rate."""
        with tempfile.TemporaryDirectory() as temporary:
            report = self.execute(Path(temporary))
            stages = [s for s in report["summary"] if s["op"].startswith(("multipart_create_", "multipart_part_", "multipart_complete_"))]
            self.assertEqual(len(stages), 3)
            for stage in stages:
                self.assertEqual(stage["failures"], 0)
                self.assertIsNotNone(stage["p50_ms"])
                self.assertIsNone(stage["verified_ops_s"])
                self.assertIsNone(stage["verified_mib_s"])
                self.assertEqual(stage["round_results"], [])
                self.assertEqual(report["operation_metrics"][stage["op"]], "latency_only")
            total = next(s for s in report["summary"] if s["op"].startswith("multipart_total_"))
            self.assertGreater(total["verified_mib_s"], 0)
            self.assertEqual(len(total["round_results"]), 2)
            self.assertEqual(total["percentile_sample_count"], 2)
            self.assertAlmostEqual(total["verified_mib_s"], 34/2**20/total["measured_seconds"])
            text = (Path(temporary)/"report/report.md").read_text()
            self.assertIn("逐轮吞吐 min–max / CV", text)
            self.assertIn("分片子阶段仅报告延迟与次数", text)
            persisted = json.loads((Path(temporary)/"report/report.json").read_text())
            self.assertEqual(persisted["summary"], report["summary"])

    def test_round_throughput_uses_each_round_verified_bytes_and_actual_wall(self):
        """Purpose: inject delay around a real SDK read in one round only; expect per-round rates to reflect that delay, warmup exclusion, and the aggregate rate to equal total verified bytes/total recorded time."""
        original = Worker.call
        delayed = False
        def observe(worker, request):
            nonlocal delayed
            if request["op"] == "get" and "/objbench/measure-1/block/" in request.get("key", "") and not delayed:
                delayed = True
                time.sleep(.1)
            return original(worker, request)
        with tempfile.TemporaryDirectory() as temporary, patch.object(Worker, "call", observe):
            report = self.execute(Path(temporary))
            row = next(s for s in report["summary"] if s["op"] == "get")
            rates = row["round_results"]
            self.assertEqual([r["round"] for r in rates], [0, 1])
            self.assertGreaterEqual(rates[1]["wall_seconds"], .1)
            for rate in rates:
                self.assertAlmostEqual(rate["verified_mib_s"]*rate["wall_seconds"]*2**20, 24)
            self.assertAlmostEqual(row["verified_mib_s"]*row["measured_seconds"]*2**20, 48)
