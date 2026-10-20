"""Verify unmet upstream configuration remains explicit, blocking, and free of cloud mutations."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from osv.ceph_bucket_config import lifecycle
from osv.cli import load_config,run
from osv.scenarios import Context,Suite,build_registry
from osv.suites import CaseNotRun,Probe,case,run_cases
from osv.worker import Worker
from test_harness import config
ROOT=Path(__file__).resolve().parents[1]

class SourcePrerequisitesTest(unittest.TestCase):
    """Do not replace missing source configuration with fabricated storage classes or passing cases."""
    def test_original_storage_class_prerequisite_is_checked_before_bucket_io(self):
        """Call the actual lifecycle translation with 0/1/2 classes; expect its explicit missing requirement before any bucket/object RPC."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            original=worker.call;requests=[]
            def observe(request):requests.append(request);return original(request)
            worker.call=observe
            try:
                for classes in ([],["STANDARD"],["STANDARD","GLACIER"]):
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"storage_classes":classes})
                    with self.assertRaises(CaseNotRun) as raised:lifecycle(Probe(ctx),"noncurrent_transition")
                    self.assertEqual(raised.exception.requirements,["workload.storage_classes: at least 3"])
                self.assertEqual(requests,[])
            finally:worker.close()

    def test_explicit_source_skip_reaches_real_report_and_blocks_measurement(self):
        """Feed an unmet prerequisite into the production case runner with a real worker; expect not_run, retained requirements, no performance and no false pass."""
        def missing(probe):raise CaseNotRun("independent missing source input","provided-source-input")
        check=case("needs_source_input","source prerequisite","required evidence cannot be skipped silently","head",missing,"independent source prerequisite")
        registry=build_registry({});registry.add_suite(Suite("source_prerequisite",check.operations,lambda ctx:run_cases(ctx,(check,)),(check.metadata(),),case_capabilities=True))
        with tempfile.TemporaryDirectory() as temporary,patch("osv.cli.build_registry",return_value=registry):
            directory=Path(temporary);data=config(directory);data["workload"]["compatibility_suites"]=["source_prerequisite"]
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            row=next(row for row in report["cases"] if row["case"]=="needs_source_input")
            self.assertEqual(row["status"],"not_run");self.assertTrue(row["gate"])
            self.assertEqual(row["reason"],"missing_prerequisites");self.assertEqual(row["missing_requirements"],["provided-source-input"])
            self.assertEqual(report["samples"],[]);self.assertEqual(report["summary"],[])
            self.assertIn("independent missing source input",(directory/"results/report.md").read_text())

    def test_empty_prerequisite_reasons_cannot_silently_skip(self):
        """Construct incomplete skip signals; expect rejection rather than an unexplained unexecuted gate."""
        for message,requirements in (("",("input",)),("missing",()),("missing",("",)),(None,("input",))):
            with self.subTest(message=message,requirements=requirements),self.assertRaises(ValueError):CaseNotRun(message,*requirements)
