"""Verify unmet upstream configuration remains explicit and free of cloud mutations."""
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

    def test_explicit_source_skip_reaches_report_without_blocking_measurement(self):
        """Feed an unmet prerequisite into the production case runner with a real worker; expect not_run with retained requirements grouped before full details alongside independent successful performance measurements."""
        def missing(probe):raise CaseNotRun("independent missing source input","provided-source-input")
        check=case("needs_source_input","source prerequisite","required evidence cannot be skipped silently","head",missing,"independent source prerequisite")
        registry=build_registry({});registry.add_suite(Suite("source_prerequisite",check.operations,lambda ctx:run_cases(ctx,(check,)),(check.metadata(),),case_capabilities=True))
        with tempfile.TemporaryDirectory() as temporary,patch("osv.cli.build_registry",return_value=registry):
            directory=Path(temporary);data=config(directory);data["workload"]["compatibility_suites"]=["source_prerequisite"]
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            row=next(row for row in report["cases"] if row["case"]=="needs_source_input")
            self.assertEqual(row["status"],"not_run");self.assertNotIn("gate",row)
            self.assertEqual(row["reason"],"missing_prerequisites");self.assertEqual(row["missing_requirements"],["provided-source-input"])
            self.assertTrue(report["samples"]);self.assertTrue(all(s["ok"] for s in report["samples"]));self.assertTrue(report["summary"])
            text = (directory/"results/report.md").read_text()
            self.assertIn("independent missing source input", text)
            reasons = text.split("## 未执行原因", 1)[1].split("## 兼容性", 1)[0]
            self.assertIn("| local | 1 |", reasons)
            self.assertIn("未满足前置条件：provided-source-input", reasons)
            self.assertLess(text.index("## 未执行原因"), text.index("<details>"))

    def test_empty_prerequisite_reasons_cannot_silently_skip(self):
        """Construct incomplete skip signals; expect rejection rather than an unexplained skipped case."""
        for message,requirements in (("",("input",)),("missing",()),("missing",("",)),(None,("input",))):
            with self.subTest(message=message,requirements=requirements),self.assertRaises(ValueError):CaseNotRun(message,*requirements)

    def test_missing_main_kms_input_precedes_actual_bucket_mutation(self):
        """Call the real KMS object source paths with absent/null configuration and a real worker; expect explicit missing input and zero bucket/object requests."""
        from osv.ceph_encryption_objects import transfer
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            original=worker.call;requests=[]
            def observe(request):requests.append(request);return original(request)
            worker.call=observe
            try:
                for workload in ({},{"kms_key_id":None}):
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)
                    for mode in ("kms_transfer","kms_default","kms_head","kms_present"):
                        with self.assertRaises(CaseNotRun) as raised:transfer(Probe(ctx),mode,13)
                        self.assertEqual(raised.exception.requirements,["workload.kms_key_id"])
                self.assertEqual(requests,[])
            finally:worker.close()

    def test_missing_acl_expectations_precede_owned_bucket_creation(self):
        """Call real canned/explicit ACL source paths without independent main-user metadata; expect the required canonical ID missing and zero worker requests."""
        from osv.ceph_acl import canned,change_permission,explicit_bucket
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            original=worker.call;requests=[]
            def observe(request):requests.append(request);return original(request)
            worker.call=observe
            try:
                ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                for action in (lambda:canned(Probe(ctx),"bucket","default"),lambda:canned(Probe(ctx),"object","cycle"),lambda:change_permission(Probe(ctx),"READ"),lambda:explicit_bucket(Probe(ctx),"group")):
                    with self.assertRaises(CaseNotRun) as raised:action()
                    self.assertEqual(raised.exception.requirements,["workload.identities.main.canonical_id"])
                self.assertEqual(requests,[])
            finally:worker.close()

    def test_explicit_http_lifecycle_and_lock_configuration_is_validated_before_io(self):
        """Load actual manifests with valid source prerequisites and malformed alternatives; expect preserved values or validation failure before starting any worker."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            valid={"ceph_http":{"endpoint":"https://storage.example:443/prefix/","addressing_style":"path","access_key_env":"AK_NAME","secret_key_env":"SK_NAME"},"lifecycle_debug_interval_seconds":1.25,"object_lock_cleanup_authorized":False,"ceph_literal_bucket_names_authorized":False,"ceph_api_name":"s3","tenant_name":"independent-tenant","secondary_kms_key_id":"independent-secondary","restore_debug_interval_seconds":1,"restore_processor_period_seconds":2,"cloud_lifecycle":{"storage_class":"CLOUD","retain_head_object":"False","target_by_bucket":False,"read_through_days":0,"target_by_bucket_prefix":"rgwx-${zonegroup}-${storage_class}-${bucket}"}}
            data=config(directory);data["workload"].update(valid);path.write_text(json.dumps(data));loaded=load_config(path)
            for field,value in valid.items():self.assertEqual(loaded["workload"][field],value)
            invalid=[{"lifecycle_debug_interval_seconds":value} for value in (None,False,0,-1,float("inf"),float("nan"),1<<4096,"1")]
            invalid += [{field:value} for field in ("restore_debug_interval_seconds","restore_processor_period_seconds") for value in (None,False,0,-1,float("inf"),1<<4096,"1")]
            invalid += [{"ceph_literal_bucket_names_authorized":value} for value in (None,0,"true")]
            invalid += [{"secondary_kms_key_id":value} for value in (False,0,[],{})]
            invalid += [{"cloud_lifecycle":{"target_by_bucket_prefix":value}} for value in ("fixed-shared-bucket","${bucket}-${bucket}","${bucket}-${unknown}","${bucket}-${unfinished")]
            invalid += [{"tenant_name":value} for value in (None,"",3,False,[])]
            invalid += [{"ceph_api_name":value} for value in (None,"",3)]
            invalid += [{"cloud_lifecycle":value} for value in (None,[],{"unknown":"x"},{"storage_class":0},{"target_by_bucket":"false"},{"read_through_days":False},{"read_through_days":-1})]
            invalid += [{"object_lock_cleanup_authorized":value} for value in (None,0,1,"true")]
            invalid += [{"ceph_http":value} for value in (None,[],{}, {"endpoint":"https://storage.example","addressing_style":"guess"}, {"endpoint":"https://storage.example","addressing_style":"path","secret":"inline"})]
            invalid += [{"ceph_http":{"endpoint":value,"addressing_style":"path"}} for value in (None,"","file:///tmp/a","https://user:secret@storage.example","https://user@storage.example","https://storage.example?x=1","https://storage.example#fragment","https://storage.example:0","https://storage.example:70000","https://storage.example:bad")]
            invalid += [{"ceph_http":{"endpoint":"https://storage.example","addressing_style":"path","access_key_env":value}} for value in (None,"",2,"AK-NAME","A B","${KEY}")]
            for fields in invalid:
                data=config(directory);data["workload"].update(fields);path.write_text(json.dumps(data))
                with self.subTest(fields=fields),self.assertRaises(ValueError):load_config(path)
