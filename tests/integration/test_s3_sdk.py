"""Optional real Java/C++ SDK HTTP integration against Moto, never cloud certification."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from osv.cli import load_config, run
from osv.worker import Worker

ROOT = Path(__file__).resolve().parents[2]

# Pinned Moto response/template discrepancies and actual typed SDK outputs are independent of the source case oracles.
# Original Ceph expectations remain unchanged; these helpers only assert that the local regression report preserves known failures.
def expected_exact_list_diagnostics(language):
    common={"ceph_exact_list_"+name for name in (
        "v1_maxkeys_zero","v2_maxkeys_zero","v1_delimiter_prefix","v1_delimiter_prefix_underscore",
        "v1_marker_none","v1_marker_empty","v1_marker_unreadable","v1_marker_not_in_list","v1_marker_after_list",
        "v2_continuationtoken_empty","v2_continuationtoken_walk","v2_continuationtoken_both","v2_fetchowner_empty")}
    if language=="java":common.update({"ceph_exact_list_v1_encoding_basic","ceph_exact_list_v2_encoding_basic"})
    else:
        common.update("ceph_exact_list_v"+str(version)+"_"+name for version in (1,2) for name in ("delimiter_whitespace","delimiter_unreadable","prefix_unreadable"))
        common.update({"ceph_exact_list_v1_encoding_basic","ceph_exact_list_v2_startafter_unreadable"})
    return common

def expected_version_diagnostics(language):
    names={"ceph_version_"+name for name in ("suspend_versions","bulk_marker_create","multipart_id","marker_header_plain","marker_header_suspended")}
    if language=="cpp":names.add("ceph_version_special")
    return names

def expected_config_diagnostics(language):
    modes=("set","get","delete","no_id","id_long","same_id","invalid_status","date","invalid_date","transition_invalid_date","noncurrent","marker","multipart") if language=="cpp" else ("id_long","same_id","invalid_status","invalid_date","transition_invalid_date")
    return {"ceph_config_lifecycle_"+mode for mode in modes}


@unittest.skipUnless(os.environ.get("OSV_RUN_SDK_TESTS") == "1", "enable explicitly after building both SDK workers")
class SdkIntegrationTest(unittest.TestCase):
    """Verify real SDK producers and consumers over HTTP rather than replacing storage calls in tests."""

    @classmethod
    def setUpClass(cls):
        """Start an isolated loopback service; use dummy credentials and require both real worker binaries."""
        import boto3
        from moto.server import ThreadedMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        host, port = cls.server.get_host_and_port()
        cls.environment = patch.dict(os.environ, {
            "OSV_ACCESS_KEY": "local-test", "OSV_SECRET_KEY": "local-test", "OSV_SESSION_TOKEN": "",
            "OSV_ENDPOINT": f"http://{host}:{port}", "OSV_REGION": "us-east-1",
            "OSV_BUCKET": "osv-sdk-integration", "OSV_PATH_STYLE": "true", "OSV_RETRIES": "0"})
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.client = boto3.client("s3", endpoint_url=f"http://{host}:{port}", region_name="us-east-1",
                                  aws_access_key_id="local-test", aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-sdk-integration")
        cls.targets = []
        commands = [("java", ["java", "-Xmx256m", "-jar", str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"), "s3"]),
                    ("cpp", [str(ROOT/"drivers/cpp/build/osv-cpp-s3")])]
        languages = os.environ.get("OSV_S3_TEST_LANGUAGES", "java,cpp").split(",")
        if not languages or set(languages) - {"java", "cpp"}:
            raise ValueError("OSV_S3_TEST_LANGUAGES must select java,cpp")
        for language, command in commands:
            if language not in languages:
                continue
            worker = Worker({"command": command, "language": language, "api": "s3"}, 30)
            try:
                cls.targets.append({"id": language, "provider": "moto-local-only", "language": language,
                    "api": "s3", "sdk": "aws-sdk-"+language, "sdk_version": worker.info["sdk_version"], "command": command})
            finally:
                worker.close()

    def test_real_sdk_requests_reach_production_harness_and_report(self):
        """Exercise actual Java/C++ SDK IO, multipart and Range; expect all gates and measurements to pass with cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = directory/"manifest.json"
            manifest.write_text(json.dumps({"schema_version": 1, "targets": self.targets,
                "workload": {"performance_model":"operations", "large_object_size":65536, "object_size": 8192, "range_size": 127, "part_size": 5*1024*1024,
                             "profile": "normal", "concurrency": 2, "requests": 5, "rounds": 2, "warmup_requests": 1,
                             "operations": ["get", "range", "put", "head", "list"]}}))
            report = run(load_config(manifest), directory/"results")
            self.assertEqual(report["coverage"]["profile"], "normal")
            self.assertEqual(len(report["coverage"]["catalog"]), 24)
            self.assertEqual([case for case in report["cases"] if case["status"] == "fail"], [])
            self.assertEqual(len(report["summary"]), 5*len(self.targets))
            self.assertEqual(len(report["samples"]), 55*len(self.targets))
            self.assertEqual([case for case in report["cases"] if case["status"] == "fail"], [])
            self.assertTrue(all(sample["ok"] for sample in report["samples"]), report["samples"])
            self.assertEqual(report["comparisons"], [])  # cross-language results must not imply an SDK comparison
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"), 0)

    def test_ceph_http_ranges_and_tags_run_through_actual_sdk_storage(self):
        """Run the real migrated producers and consumers against Moto; expect only Moto excess-tag BadRequest mismatch, blocked measurements and complete cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["ceph_http_range","ceph_tags","ceph_headers"],"performance_model":"operations","object_size":1024,"range_size":17,
                "part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0,"operations":["head"]}}))
            report=run(load_config(path),directory/"results")
            self.assertEqual(len(report["coverage"]["catalog"]),19)
            failures=[(row["target"],row["case"]) for row in report["cases"] if row["status"]!="pass"]
            self.assertEqual(failures,[(target["id"],"ceph_tags_excess") for target in self.targets])
            for row in report["cases"]:
                if row["status"]=="fail":self.assertIn("BadRequest",row["error"])
            self.assertEqual(report["samples"],[])
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"),0)

    def test_exact_ceph_listing_keeps_original_failures_and_cleans_every_bucket(self):
        """Execute all 74 exact LIST translations through actual SDK IO; expect known Moto/SDK discrepancies, blocked performance and no leaked buckets."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["ceph_exact_listing"],"performance_model":"operations","object_size":1024,"range_size":17,
                "part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0,"operations":["head"]}}))
            report=run(load_config(path),directory/"results")
            self.assertEqual(len(report["coverage"]["catalog"]),74)
            failures={(row["target"],row["case"]) for row in report["cases"] if row["status"]=="fail"}
            self.assertEqual(failures,{(target["id"],name) for target in self.targets for name in expected_exact_list_diagnostics(target["language"])})
            self.assertFalse([row for row in report["cases"] if row["status"]=="not_run"])
            self.assertEqual(report["samples"],[])
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_ceph_bulk_delete_keeps_limit_rejection_expectations(self):
        """Execute four source bulk cases through real SDK IO; expect Moto accepting 1001 keys to remain a failure and all buckets to be cleaned."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["ceph_delete_many"],"performance_model":"operations","object_size":1024,"range_size":17,
                "part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0,"operations":["head"]}}))
            report=run(load_config(path),directory/"results")
            failures={(row["target"],row["case"]) for row in report["cases"] if row["status"]=="fail"}
            self.assertEqual(failures,{(target["id"],"ceph_delete_many_v"+str(version)+"_limit") for target in self.targets for version in (1,2)})
            self.assertEqual(len(report["coverage"]["catalog"]),4);self.assertEqual(report["samples"],[])
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_exact_ceph_multipart_keeps_real_sizes_and_source_failures(self):
        """Run 13 multipart translations through actual SDK IO; expect only Moto empty/missing-upload 500 mismatches and no remaining test buckets."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["ceph_exact_multipart"],"performance_model":"operations","object_size":1024,"range_size":17,
                "part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0,"operations":["head"]}}))
            report=run(load_config(path),directory/"results")
            failures={(row["target"],row["case"]) for row in report["cases"] if row["status"]=="fail"}
            self.assertEqual(failures,{(target["id"],"ceph_exact_multipart_"+name) for target in self.targets for name in ("empty","missing_upload")})
            self.assertEqual(len(report["coverage"]["catalog"]),13);self.assertEqual(report["samples"],[])
            self.assertFalse([row for row in report["cases"] if row["status"]=="not_run"])
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_cross_sdk_bulk_delete_consumes_actual_other_sdk_objects(self):
        """Write through one SDK and bulk-delete through the other in each direction; expect real empty listings and verified owned-bucket cleanup."""
        if len(self.targets)!=2:self.skipTest("requires both language workers")
        from osv.bucket_scope import BucketScope
        from osv.scenarios import Context
        from osv.suites import Probe
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);workers=[Worker(target,30) for target in self.targets]
            try:
                for producer,consumer in (workers,workers[::-1]):
                    ctx=Context(producer,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    with BucketScope(Probe(ctx)) as bucket:
                        for index in range(3):bucket.put("cross-"+str(index),("value-"+str(index)).encode())
                        self.assertEqual(len(bucket.call("list_v2_observed")["objects"]),3)
                        result=consumer.call({"op":"delete_many","bucket":bucket.name,"objects":[{"key":"cross-"+str(index)} for index in range(3)]})
                        self.assertTrue(result["ok"],result);self.assertFalse(result["errors_present"]);self.assertEqual(len(result["deleted"]),3)
                        self.assertEqual(bucket.call("list_v2_observed")["keys"],[])
                    self.assertTrue(bucket.cleanup_completed)
            finally:
                for worker in workers:worker.close()
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_owned_bucket_cleans_opaque_keys_after_actual_sdk_listing(self):
        """Store real whitespace keys then fail after a true SDK listing; expect original producer keys cleaned even when a typed XML getter trims them."""
        from osv.bucket_scope import BucketScope
        from osv.scenarios import Context
        from osv.suites import Probe
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    scope=BucketScope(Probe(ctx))
                    with self.assertRaisesRegex(AssertionError,"deliberate failure"):
                        with scope as bucket:
                            for key in (" "," real "):bucket.put(key,b"source-value")
                            bucket.call("list",prefix="")
                            raise AssertionError("deliberate failure")
                    self.assertTrue(scope.cleanup_completed)
                    self.assertEqual(worker.call({"op":"bucket_head","bucket":scope.name})["status_code"],404)
                finally:worker.close()
        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_versioned_cleanup_purges_real_first_pages_and_opaque_keys(self):
        """Create real versions/markers including whitespace keys and limit actual list requests to one entry; expect all six exact versions removed without new markers or default-bucket changes."""
        from osv.version_scope import VersionScope
        from osv.scenarios import Context
        from osv.suites import Probe
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30);original=worker.call;deletions=[];pages=[]
                def capped(request):
                    if request["op"]=="versions_list":
                        request={**request,"max_keys":1};pages.append(request)
                    if request["op"]=="delete_version" and "version_id" in request:deletions.append((request["key"],request["version_id"]))
                    return original(request)
                worker.call=capped
                try:
                    self.assertNotIn("versioning_status",original({"op":"versioning_get"}))
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    scope=VersionScope(Probe(ctx));expected=set()
                    with self.assertRaisesRegex(AssertionError,"case failed after real versions"):
                        with scope as bucket:
                            bucket.call("versioning_put",status="Enabled")
                            for key in (" ","ordinary"):
                                for payload in (b"first-real-version",b"second-real-version"):
                                    ctx.body.write_bytes(payload)
                                    result=bucket.call("put_version",key=key,body_path=str(ctx.body));expected.add((key,result["version_id"]))
                                result=bucket.call("delete_version",key=key);self.assertTrue(result["delete_marker"]);expected.add((key,result["version_id"]))
                            raise AssertionError("case failed after real versions")
                    self.assertTrue(scope.cleanup_completed);self.assertEqual(set(deletions),expected);self.assertEqual(len(deletions),6)
                    self.assertGreaterEqual(len(pages),7)
                    self.assertNotIn("versioning_status",original({"op":"versioning_get"}))
                    self.assertEqual(original({"op":"bucket_head","bucket":scope.name})["status_code"],404)
                finally:worker.close()
        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_configured_storage_classes_execute_original_transition_input(self):
        """Provide the actual source's three-class prerequisite; expect Java to send the full original transition rule, C++ to retain its legacy-Prefix model error, and no leaked buckets."""
        from osv.ceph_bucket_config import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30);original=worker.call;requests=[]
                def observe(request):
                    if request["op"]=="api.PutBucketLifecycleConfiguration":requests.append(request)
                    return original(request)
                worker.call=observe
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"storage_classes":["STANDARD","GLACIER","DEEP_ARCHIVE"]})
                    rows=run_cases(ctx,(next(c for c in CASES if c.name=="ceph_config_lifecycle_noncurrent_transition"),))
                    self.assertEqual(len(requests),1)
                    rule=requests[0]["input"]["LifecycleConfiguration"]["Rules"][0]
                    self.assertEqual(rule["NoncurrentVersionTransitions"],[{"NoncurrentDays":2,"StorageClass":"GLACIER"},{"NoncurrentDays":4,"StorageClass":"DEEP_ARCHIVE"}])
                    self.assertEqual(rows[0]["status"],"fail" if target["language"]=="cpp" else "pass",rows)
                    if target["language"]=="cpp":self.assertIn("SDK model has no member: Prefix",rows[0]["error"])
                    self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_corrupt_version_oracles_fail_and_clean_real_history(self):
        """Corrupt one actual marker/count/ID observation after genuine SDK writes; expect source assertions to fail and every historical object/marker bucket to be removed."""
        from osv.ceph_versioning import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            for mode in ("head","stack","atomic_id"):
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30);original=worker.call;faulted=[]
                    def corrupt(request):
                        result=original(request)
                        if not faulted and result["ok"]:
                            if mode=="head" and request["op"]=="delete_version" and "version_id" not in request:
                                result["delete_marker"]=False;faulted.append(True)
                            elif mode=="stack" and request["op"]=="versions_list" and len(result["delete_markers"])==3:
                                result["delete_markers"]=result["delete_markers"][:-1];faulted.append(True)
                            elif mode=="atomic_id" and request["op"]=="put_version":
                                result.pop("version_id",None);faulted.append(True)
                        return result
                    worker.call=corrupt
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                        rows=run_cases(ctx,(next(c for c in CASES if c.name=="ceph_version_"+mode),))
                        self.assertTrue(faulted);self.assertEqual(rows[0]["status"],"fail",rows)
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_ceph_bucket_cases_create_and_clean_actual_owned_buckets(self):
        """Run the seven real SDK bucket scenarios; expect exact source responses, a usable default bucket and no extra buckets afterward."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["ceph_buckets"],"performance_model":"operations","object_size":1024,"range_size":17,
                "part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0,"operations":["head"]}}))
            report=run(load_config(path),directory/"results")
            self.assertEqual(len(report["coverage"]["catalog"]),7)
            self.assertEqual([row for row in report["cases"] if row["status"]!="pass"],[])
            self.assertTrue(report["samples"])
            scopes=[row["evidence"] for row in report["cases"] if row.get("evidence")]
            self.assertEqual(len(scopes),4*len(self.targets));self.assertTrue(all(row["cleanup_confirmed"] for row in scopes))
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_real_sdk_bucket_scope_cleans_after_failed_assertion(self):
        """Fail inside the real bucket owner helper after a genuine SDK write/read; expect cleanup while independently stored default-bucket bytes survive."""
        from osv.bucket_scope import BucketScope
        from osv.scenarios import Context
        from osv.suites import Probe
        self.client.put_object(Bucket="osv-sdk-integration",Key="scope-sentinel",Body=b"configured-data")
        try:
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                        scope=BucketScope(Probe(ctx))
                        with self.assertRaisesRegex(AssertionError,"deliberate source failure"):
                            with scope as bucket:
                                bucket.put("scope-sentinel",b"owned-data")
                                bucket.call("get",key="scope-sentinel",output_path=str(ctx.output));self.assertEqual(ctx.output.read_bytes(),b"owned-data")
                                raise AssertionError("deliberate source failure")
                        self.assertTrue(scope.cleanup_completed)
                        result=worker.call({"op":"get","key":"scope-sentinel","output_path":str(ctx.output)})
                        self.assertTrue(result["ok"],result);self.assertEqual(ctx.output.read_bytes(),b"configured-data")
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()
        finally:self.client.delete_object(Bucket="osv-sdk-integration",Key="scope-sentinel")

    def test_real_sdk_multikey_objbench_model(self):
        """Run the new multi-object model through real Java/C++ AWS SDKs; expect 31 verified requests per target and no leftover objects."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "profile":"normal","performance_model":"objbench","object_size":1024,"range_size":17,
                "large_object_size":65536,"part_size":5*1024*1024,"concurrency":2,"requests":1,"rounds":1,
                "objbench":{"small_object_size":128,"small_objects":3,"block_size":1024,"big_object_size":3072,"list_requests_per_lane":2}}}))
            report=run(load_config(path),directory/"results")
            self.assertEqual([row for row in report["cases"] if row["status"]=="fail"],[])
            self.assertEqual(len(report["samples"]),31*len(self.targets))
            self.assertEqual(len(report["summary"]),8*len(self.targets))
            self.assertTrue(all(row["ok"] for row in report["samples"]))
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"),0)

    def test_complete_profile_preserves_moto_zero_maxkeys_incompatibility(self):
        """Run the complete catalog through real SDKs; expect the pinned Moto/SDK diagnostic set and blocked performance."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "profile":"complete","performance_model":"operations","object_size":1024,"range_size":17,
                "large_object_size":65536,"part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0}}))
            report=run(load_config(path),directory/"results")
            failures=[(row["target"],row["case"]) for row in report["cases"] if row["status"]=="fail"]
            expected=[(target["id"],"ceph_list_maxkeys_zero") for target in self.targets]
            expected.extend((target["id"],"ceph_tags_excess") for target in self.targets)
            expected.extend((target["id"],name) for target in self.targets for name in expected_exact_list_diagnostics(target["language"]))
            expected.extend((target["id"],"ceph_delete_many_v"+str(version)+"_limit") for target in self.targets for version in (1,2))
            expected.extend((target["id"],"ceph_exact_multipart_"+name) for target in self.targets for name in ("empty","missing_upload"))
            expected.extend((target["id"],name) for target in self.targets for name in ("ceph_exact_multipart_copy_invalid","ceph_exact_multipart_copy_improper","ceph_get_part_multiple","ceph_get_part_single","ceph_get_part_ordinary"))
            expected.extend((target["id"],name) for target in self.targets for name in expected_version_diagnostics(target["language"]))
            expected.extend((target["id"],name) for target in self.targets for name in expected_config_diagnostics(target["language"]))
            expected += [(target["id"],name) for target in self.targets if target["language"]=="cpp"
                         for name in ("ceph_list_delimiter_space","ceph_list_delimiter_newline")]
            self.assertEqual(sorted(failures),sorted(expected))
            for target in self.targets:
                observed=next(row for row in report["cases"] if row["target"]==target["id"] and row["case"]=="juicefs_range_start_overflow")
                self.assertEqual(observed["status"],"observation")
                self.assertFalse(observed["evidence"]["source_matches"])
                self.assertEqual(observed["evidence"]["observed"]["error_code"],"InvalidRange")
                strict=next(row for row in report["cases"] if row["target"]==target["id"] and row["case"]=="ceph_range_invalid_start")
                self.assertEqual(strict["status"],"pass")
            self.assertEqual(report["samples"],[])
            self.assertEqual(report["summary"],[])
            self.assertFalse(next(row for row in report["upstream_alignment"]["summary"] if row["project"]=="ceph")["complete"])
            self.assertEqual(len(report["coverage"]["catalog"]),274)
            missing=[row for row in report["cases"] if row["status"]=="not_run"]
            self.assertEqual({(row["target"],row["case"]) for row in missing},{(target["id"],"ceph_config_lifecycle_noncurrent_transition") for target in self.targets})
            self.assertTrue(all(row["reason"]=="missing_prerequisites" and row["missing_requirements"]==["workload.storage_classes: at least 3"] for row in missing))
            self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_partial_capabilities_keep_real_sdk_copy_replacement_checks(self):
        """Mask copy in actual Java/C++ SDK handshakes; expect seven unexecuted rows per target, successful genuine copy_replace checks and no performance evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);targets=[]
            for target in self.targets:
                proxy=directory/(target["id"]+"-limited.py")
                proxy.write_text("import subprocess,json,sys\n"
                    +f"child=subprocess.Popen({target['command']!r},stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\n"
                    +"try:\n for line in sys.stdin:\n  q=json.loads(line);child.stdin.write(line);child.stdin.flush();reply=json.loads(child.stdout.readline())\n"
                    +"  if q['op']=='info': reply['capabilities'].remove('copy')\n"
                    +"  print(json.dumps(reply),flush=True)\nfinally: child.stdin.close();child.wait()\n")
                targets.append({**target,"command":[sys.executable,str(proxy)]})
            path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":targets,"workload":{
                "compatibility_suites":["ceph_copy"],"performance_model":"operations","operations":["head"],
                "object_size":1024,"range_size":17,"part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0}}))
            report=run(load_config(path),directory/"results")
            for target in targets:
                rows=[row for row in report["cases"] if row["target"]==target["id"]]
                self.assertEqual(sum(row["status"]=="not_run" for row in rows),7)
                self.assertEqual({row["case"] for row in rows if row["status"]=="pass"},{"ceph_copy_self_metadata","ceph_copy_replace_metadata"})
                self.assertFalse([row for row in rows if row["status"]=="fail"],rows)
            self.assertEqual(report["samples"],[])
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"),0)

    def test_cross_sdk_real_producer_and_consumer(self):
        """Feed Java-produced bytes to C++ GET and reverse the direction; expect exact independent bytes and Range output."""
        if len(self.targets) != 2:
            self.skipTest("cross-SDK check requires both language workers")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            expected = bytes(range(256))*13+b"independent-tail"
            body = directory/"body.bin"
            output = directory/"output.bin"
            body.write_bytes(expected)
            workers = [Worker(target, 30) for target in self.targets]
            try:
                for producer, consumer in (workers, workers[::-1]):
                    key = "cross-sdk/round-trip"
                    self.assertTrue(producer.call({"op":"put", "key":key, "body_path":str(body)})["ok"])
                    self.assertTrue(consumer.call({"op":"get", "key":key, "output_path":str(output)})["ok"])
                    self.assertEqual(output.read_bytes(), expected)
                    self.assertTrue(consumer.call({"op":"get", "key":key, "offset":251, "length":19, "output_path":str(output)})["ok"])
                    self.assertEqual(output.read_bytes(), expected[251:270])
                    self.assertTrue(producer.call({"op":"delete", "key":key})["ok"])
            finally:
                self.client.delete_object(Bucket="osv-sdk-integration", Key="cross-sdk/round-trip")
                for worker in workers:
                    worker.close()


if __name__ == "__main__":
    unittest.main()
