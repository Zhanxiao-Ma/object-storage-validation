"""Verify multi-key benchmark phases using the real local producer, consumer and report pipeline."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from osv.cli import load_config,run
from osv.objbench import normalize
from test_harness import config,ROOT


class ObjbenchTest(unittest.TestCase):
    """Check dataset shape and end-to-end oracles rather than reproducing benchmark implementation in tests."""
    def manifest(self,directory):
        data=config(directory)
        data["workload"].update(requests=1,rounds=2,warmup_requests=0,
            objbench={"small_object_size":5,"small_objects":3,"block_size":8,"big_object_size":25,
                      "list_requests_per_lane":2})
        path=directory/"manifest.json";path.write_text(json.dumps(data));return path

    def test_real_multikey_phase_counts_content_and_cleanup(self):
        """Run a 3-small/4-block dataset twice; expect exact 72 multi-object requests, independent reads, 47 bytes/round and cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            report=run(load_config(self.manifest(directory)),directory/"results")
            self.assertEqual(sum(r["op"] in {"smallput","smallget","put","get","range","head","list","delete"} for r in report["samples"]),72)
            self.assertEqual({row["op"] for row in report["summary"] if not row["op"].startswith(("repeat_","parallel_","read_","multipart_"))},{"smallput","smallget","put","get","range","head","list","delete"})
            self.assertFalse([row for row in report["cases"] if row["status"]=="fail"])
            self.assertTrue(all(row["ok"] for row in report["samples"]))
            self.assertTrue(all(row["actual_bytes"]==47 and row["effective_lanes"]==2 for row in report["benchmark_datasets"]))
            self.assertEqual(len({row["key"] for row in report["samples"] if row["op"]=="put"}),8)
            listing=next(row for row in report["summary"] if row["op"]=="list")
            self.assertAlmostEqual(listing["verified_objects_s"],listing["verified_ops_s"]*7)
            self.assertEqual([file for file in (directory/"objects").rglob("*") if file.is_file()],[])

    def test_default_list_phase_executes_once_per_effective_lane(self):
        """Omit the LIST repetition override and run real IO; expect one verified enumeration per lane as in pinned objbench."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=self.manifest(directory)
            data=json.loads(path.read_text());data["workload"]["objbench"].pop("list_requests_per_lane")
            data["workload"]["rounds"]=1;path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            listings=[row for row in report["samples"] if row["op"]=="list"]
            self.assertEqual(len(listings),2)
            self.assertTrue(all(row["ok"] and row["objects"]==7 for row in listings))
            self.assertEqual(report["workload"]["objbench"]["list_requests_per_lane"],1)

    def test_warmup_rounds_are_real_but_excluded_from_summary(self):
        """Execute one additional complete warmup round; expect 36 multi-object warmup requests and only two measured rounds in summaries."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=self.manifest(directory)
            data=json.loads(path.read_text());data["workload"]["objbench"]["warmup_rounds"]=1;path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            self.assertEqual(sum(row["phase"]=="warmup" for row in report["samples"]),36)
            self.assertEqual(sum(row["attempts"] for row in report["summary"] if not row["op"].startswith(("repeat_","parallel_","read_","multipart_"))),72)

    def test_other_object_response_cannot_pass_integrity(self):
        """Redirect benchmark GET to a different genuinely stored object; expect content failures and failed dataflow gates."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=self.manifest(directory)
            proxy=directory/"wrong-object.py"
            # Forward every real operation. Only GET key selection is deliberately faulty; no storage or oracle is copied.
            proxy.write_text("import json,subprocess,sys\n"
                +f"child=subprocess.Popen([sys.executable,{str(ROOT/'drivers/reference.py')!r}],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\n"
                +"try:\n"
                +" for line in sys.stdin:\n"
                +"  q=json.loads(line)\n"
                +"  if q.get('op')=='get' and '/objbench/' in q.get('key','') and q['key'].endswith('000001'): q['key']=q['key'][:-6]+'000000'\n"
                +"  child.stdin.write(json.dumps(q)+'\\n');child.stdin.flush();print(child.stdout.readline().strip(),flush=True)\n"
                +"finally: child.terminate();child.wait()\n")
            data=json.loads(path.read_text());data["targets"][0]["command"]=[sys.executable,str(proxy)];path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            self.assertTrue(any(not row["ok"] and row["op"] in ("get","smallget") for row in report["samples"]))
            self.assertTrue(any(row["phase"].startswith("objbench_dataflow") for row in report["execution_errors"]))

    def test_upstream_defaults_and_parameter_boundaries(self):
        """Check pinned workload defaults and reject invalid/unbounded datasets before any worker or allocation starts."""
        defaults=normalize({})
        self.assertEqual(defaults["list_requests_per_lane"],1)
        self.assertEqual((defaults["small_objects"],defaults["small_object_size"],defaults["big_object_size"],defaults["block_size"]),
                         (100,128*1024,1024**3,4*1024**2))
        for parameters in ({"small_objects":0},{"block_size":True},{"block_size":257*1024**2},
                           {"big_object_size":17*1024**3},{"warmup_rounds":11},{"unknown":1}):
            with self.subTest(parameters=parameters),self.assertRaises(ValueError):normalize(parameters)
