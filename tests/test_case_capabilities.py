"""Verify partial SDK capabilities preserve unrelated real checks and block unsupported required conclusions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from osv.scenarios import Registry,Suite
from osv.suites import CORE
from test_harness import config,ROOT


class CaseCapabilityTest(unittest.TestCase):
    """Exercise genuine producer/consumer IO and the CLI instead of simulating capability dispatch in tests."""
    def test_missing_copy_runs_other_cases_but_blocks_measurement_and_exit(self):
        """Hide only copy in a real worker handshake; expect seven explicit not_run cases, real unrelated passes, zero measurements and exit 1."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);proxy=directory/"limited-worker.py";audit=directory/"requests.jsonl"
            proxy.write_text("import json,subprocess,sys\n"
                +f"child=subprocess.Popen([sys.executable,{str(ROOT/'drivers/reference.py')!r}],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\n"
                +"try:\n for line in sys.stdin:\n  q=json.loads(line)\n"
                +f"  with open({str(audit)!r},'a') as output: output.write(json.dumps(q)+'\\n')\n"
                +"  child.stdin.write(line);child.stdin.flush();reply=json.loads(child.stdout.readline())\n"
                +"  if q['op']=='info': reply['capabilities'].remove('copy')\n"
                +"  print(json.dumps(reply),flush=True)\nfinally: child.stdin.close();child.wait()\n")
            data=config(directory);data["targets"][0]["command"]=[sys.executable,str(proxy)]
            data["workload"].update(compatibility_suites=["core_objects","ceph_copy"],operations=["head"],requests=1,rounds=1,concurrency=1,warmup_requests=0)
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            result=subprocess.run([sys.executable,"-m","osv","run",str(path),"--output",str(directory/"results")],cwd=ROOT,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,1,result.stdout+result.stderr)
            report=json.loads((directory/"results/report.json").read_text())
            missing=[row for row in report["cases"] if row["status"]=="not_run"]
            self.assertEqual({row["case"] for row in missing},{"ceph_copy_zero","ceph_copy_16m","ceph_copy_same_bucket","ceph_copy_content_type","ceph_copy_self_rejected","ceph_copy_keep_metadata","ceph_copy_missing_key"})
            self.assertTrue(all(row["reason"]=="unsupported_operations" and row["gate"] for row in missing))
            self.assertTrue(all(row["missing_operations"]==["copy"] for row in missing))
            self.assertEqual(report["samples"],[])
            self.assertFalse([row for row in report["cases"] if row["status"]=="fail"],report["cases"])
            by_name={row["case"]:row for row in report["cases"]}
            self.assertEqual(by_name["object_roundtrip"]["status"],"pass")
            self.assertEqual(by_name["ceph_copy_replace_metadata"]["status"],"pass")
            requests=[json.loads(line) for line in audit.read_text().splitlines()]
            self.assertFalse(any(row["op"]=="copy" for row in requests))
            self.assertTrue(any(row["op"]=="copy_replace" for row in requests))
            self.assertIn("| local | 23 | 15 | 0 | 1 | 7 |",(directory/"results/report.md").read_text())

    def test_missing_optional_observation_is_visible_without_blocking_valid_measurement(self):
        """Declare an unavailable optional probe beside a real roundtrip; expect not_run with gate=false, real HEAD measurement and CLI exit 0."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);plugin=directory/"optional_suite.py"
            plugin.write_text("from osv.suites import CORE,case,run_cases\nfrom osv.scenarios import Suite\n"
                +"def forbidden(p): raise AssertionError('unsupported observation must not run')\n"
                +"def register(registry):\n cases=(CORE[0],case('optional_probe','probe purpose','record unavailable capability','unavailable_observation',forbidden,'probe reference',observation=True))\n"
                +" registry.add_suite(Suite('optional_suite',frozenset({'put','get','head','unavailable_observation'}),lambda ctx:run_cases(ctx,cases),tuple(c.metadata() for c in cases),case_capabilities=True))\n")
            data=config(directory);data["plugins"]=["optional_suite"]
            data["workload"].update(compatibility_suites=["optional_suite"],operations=["head"],requests=1,rounds=1,concurrency=1,warmup_requests=0)
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            environment=dict(os.environ,PYTHONPATH=str(directory)+os.pathsep+str(ROOT))
            result=subprocess.run([sys.executable,"-m","osv","run",str(path),"--output",str(directory/"results")],cwd=ROOT,capture_output=True,text=True,timeout=30,env=environment)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            report=json.loads((directory/"results/report.json").read_text())
            row=next(row for row in report["cases"] if row["case"]=="optional_probe")
            self.assertEqual(row["status"],"not_run");self.assertFalse(row["gate"])
            self.assertEqual(len(report["samples"]),1);self.assertTrue(report["samples"][0]["ok"])
            self.assertIn("| local | 2 | 1 | 0 | 0 | 1 |",(directory/"results/report.md").read_text())

    def test_partial_capability_registration_requires_complete_operation_catalog(self):
        """Supply missing/mismatched case operation metadata; expect rejection before worker execution while valid exact metadata registers."""
        catalog=(CORE[0].metadata(),)
        for operations,rows,flag in ((frozenset({"put"}),catalog,True),(CORE[0].operations,(),True),
                                     (CORE[0].operations,({**catalog[0],"operations":[]},),True),
                                     (CORE[0].operations,catalog,"yes")):
            with self.subTest(operations=operations,flag=flag),self.assertRaises(ValueError):
                Registry().add_suite(Suite("probe",operations,lambda ctx:[],rows,case_capabilities=flag))
        registry=Registry();registry.add_suite(Suite("probe",CORE[0].operations,lambda ctx:[],catalog,case_capabilities=True))
        self.assertTrue(registry.suites["probe"].case_capabilities)
