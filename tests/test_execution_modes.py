"""Exercise independent CLI modes with real filesystem workers and independent input bytes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from osv.cli import load_config
from test_harness import ROOT, config


class ExecutionModesTest(unittest.TestCase):
    """Verify scheduling and report contracts using genuine storage IO, not a duplicate scheduler."""

    def execute(self, directory, mode=None, model="operations", verdict="fail", corrupt=False):
        """Run a real CLI, recording real worker requests for independent assertions."""
        plugin=directory/"mode_suite.py"
        plugin.write_text("from osv.suites import case,run_cases,CaseNotRun\nfrom osv.scenarios import Suite\n"
            "def probe(p):\n p.put(b'independent functional bytes')\n assert p.read()==b'independent functional bytes'\n"
            +(" raise CaseNotRun('missing external identity','identity')\n" if verdict=="not_run" else " raise AssertionError('independent functional failure')\n")
            +"def register(r):\n c=case('independent_function','verify functional isolation','intentional failure or missing prerequisite','put get',probe,'independent oracle')\n r.add_suite(Suite('independent_suite',c.operations,lambda ctx:run_cases(ctx,(c,)),(c.metadata(),),case_capabilities=True))\n")
        audit=directory/"requests.jsonl";proxy=directory/"worker.py"
        proxy.write_text("import json,subprocess,sys\n"
            +f"child=subprocess.Popen([sys.executable,{str(ROOT/'drivers/reference.py')!r}],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\n"
            +"try:\n for line in sys.stdin:\n  q=json.loads(line)\n"
            +f"  with open({str(audit)!r},'a') as stream: stream.write(line)\n"
            +"  child.stdin.write(line);child.stdin.flush();r=json.loads(child.stdout.readline())\n"
            # The real producer and real consumer still run; only fault the actual bytes received.
            +("  if q.get('op')=='get' and '/performance/' in q.get('key','') and r.get('ok'): open(q['output_path'],'wb').write(b'wrong bytes')\n" if corrupt else "")
            +"  if q['op']=='info': r['capabilities']=[x for x in r['capabilities'] if not x.startswith('mp_')]\n"
            +"  print(json.dumps(r),flush=True)\nfinally: child.stdin.close();child.wait()\n")
        data=config(directory);data["targets"][0]["command"]=[sys.executable,str(proxy)];data["plugins"]=["mode_suite"]
        data["workload"].pop("mode");data["workload"].update(compatibility_suites=["independent_suite"],performance_model=model,
            requests=2,rounds=1,concurrency=1,warmup_requests=0,operations=["get"],
            objbench={"small_objects":2,"small_object_size":5,"big_object_size":17,"block_size":8,"list_requests_per_lane":1})
        manifest=directory/"manifest.json";manifest.write_text(json.dumps(data))
        command=[sys.executable,"-m","osv","run",str(manifest),"--output",str(directory/"results")]
        if mode is not None:command += ["--mode",mode]
        env=dict(os.environ,PYTHONPATH=str(directory)+os.pathsep+str(ROOT))
        result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
        self.assertTrue((directory/"results/report.json").exists(),result.stdout+result.stderr)
        return result,json.loads((directory/"results/report.json").read_text()),[json.loads(x) for x in audit.read_text().splitlines()]

    def test_default_and_explicit_functional_never_execute_performance(self):
        """Purpose: select default/explicit functionality with a real failing case; expect exit 1, its real readback, no samples/comparisons or performance namespace IO."""
        for mode in (None,"functional"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as temporary:
                result,report,requests=self.execute(Path(temporary),mode)
                self.assertEqual(result.returncode,1,result.stderr)
                self.assertEqual(report["workload"]["mode"],"functional")
                self.assertEqual(report["cases"][0]["status"],"fail")
                self.assertEqual(report["samples"],[]);self.assertEqual(report["comparisons"],[])
                self.assertTrue(any(q["op"]=="get" and "/functional/" in q.get("key","") for q in requests))
                self.assertFalse(any("/performance/" in q.get("key",q.get("prefix","")) for q in requests))

    def test_performance_only_never_invokes_functional_suites_for_either_model(self):
        """Purpose: run both genuine performance models with unavailable multipart capability; expect exit 0, verified samples and no functional case, audit or request."""
        for model in ("operations","objbench"):
            with self.subTest(model=model),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);result,report,requests=self.execute(directory,"performance",model)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(report["cases"],[]);self.assertEqual(report["coverage"]["catalog"],[])
                self.assertFalse((directory/"results/alignment.json").exists())
                self.assertTrue(report["samples"]);self.assertTrue(all(s["ok"] for s in report["samples"]))
                self.assertFalse(any("/functional/" in q.get("key",q.get("prefix","")) for q in requests))
                self.assertFalse(any(q["op"].startswith("mp_") for q in requests))
                self.assertEqual(list((directory/"objects").rglob("*.bin")),[])

    def test_combined_failure_and_skip_cannot_suppress_performance(self):
        """Purpose: real functional IO ends in failure/missing prerequisite; expect unchanged verdict, independent valid GET measurements and exit 1 only for actual failure."""
        for verdict,code in (("fail",1),("not_run",0)):
            with self.subTest(verdict=verdict),tempfile.TemporaryDirectory() as temporary:
                result,report,requests=self.execute(Path(temporary),"all",verdict=verdict)
                self.assertEqual(result.returncode,code,result.stdout+result.stderr)
                self.assertEqual(report["cases"][0]["status"],verdict)
                self.assertTrue(report["samples"]);self.assertTrue(all(s["ok"] for s in report["samples"]))
                self.assertTrue(any(q["op"]=="get" and "/performance/" in q.get("key","") for q in requests))
                self.assertNotIn("gate",report["cases"][0])

    def test_performance_content_corruption_remains_measured_failure(self):
        """Purpose: corrupt bytes returned by real storage in either benchmark model; expect nonzero exit, failed requests, zero verified bytes for failed requests and no fabricated functional verdict."""
        for model in ("operations","objbench"):
            with self.subTest(model=model),tempfile.TemporaryDirectory() as temporary:
                result,report,_=self.execute(Path(temporary),"performance",model,corrupt=True)
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                self.assertEqual(report["cases"],[])
                failed=[s for s in report["samples"] if not s["ok"]]
                self.assertTrue(failed);self.assertTrue(all(s["bytes"]==0 for s in failed))
                self.assertTrue(any(s["failures"] for s in report["summary"]))

    def test_mode_override_and_invalid_values_are_checked_before_io(self):
        """Purpose: load mode defaults and CLI-equivalent overrides; expect explicit precedence and reject malformed values without starting a worker."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory);path=directory/"manifest.json"
            data["workload"].pop("mode");path.write_text(json.dumps(data))
            self.assertEqual(load_config(path)["workload"]["mode"],"functional")
            self.assertEqual(load_config(path,mode_override="performance")["workload"]["mode"],"performance")
            for invalid in ("unknown",False,None,[],{}):
                data["workload"]["mode"]=invalid;path.write_text(json.dumps(data))
                with self.subTest(invalid=invalid),self.assertRaises(ValueError):load_config(path)
            self.assertFalse((directory/"objects").exists())

    def test_performance_startup_failure_is_not_a_functional_verdict(self):
        """Purpose: start a missing worker in performance mode; expect execution error/exit 1, no fabricated functional cases or successful measurements."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory)
            data["targets"][0]["command"]=["/does-not-exist/osv-worker"]
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            result=subprocess.run([sys.executable,"-m","osv","run",str(path),"--mode","performance","--output",str(directory/"results")],
                                  cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,1,result.stderr)
            report=json.loads((directory/"results/report.json").read_text())
            self.assertEqual(report["cases"],[]);self.assertEqual(report["samples"],[])
            self.assertEqual(report["execution_errors"][0]["phase"],"worker_setup")
