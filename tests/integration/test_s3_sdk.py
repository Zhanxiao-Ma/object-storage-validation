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
def expected_conditional_version_diagnostics(language):
    writes={"ceph_conditional_versions_"+name for name in ("put_basic","put_current_match","put_current_none","put_marker","multipart_basic","multipart_current_match","multipart_current_none")}
    deletes={"ceph_conditional_versions_delete_"+kind+suffix for kind in ("match","mtime","size") for suffix in ("","_current","_version")}
    bulk={"ceph_conditional_versions_bulk_"+kind+suffix for kind in ("match","mtime","size") for suffix in ("","_current","_version")}
    return writes|deletes|(bulk if language=="cpp" else set())


def expected_public_controls_diagnostics(language):
    status={"ceph_public_status_"+mode for mode in ("empty","public_acl","authenticated_acl","public_policy","source_ip","principal")}
    controls={"ceph_public_controls_"+mode for mode in ("bucket_acl","object_acl","policy","restrict")}
    return status|controls|({"ceph_public_controls_principal"} if language=="cpp" else set())


def expected_request_header_diagnostics():
    return {"ceph_header_object_"+name for name in ("md5_invalid_short","md5_bad","md5_empty","length_empty","length_negative","length_none","authorization_empty","authorization_none")}|{"ceph_header_bucket_"+name for name in ("length_empty","length_negative","authorization_empty","authorization_none")}|{"ceph_header_acl_bad_canned"}


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

def expected_acl_metadata_prerequisites():
    from osv.ceph_acl import CASES
    return {item.name for item in CASES if item.name not in {"ceph_acl_private_to_private","ceph_acl_revoke_all"} and not item.name.startswith("ceph_acl_user_")}

def expected_acl_identity_prerequisites():
    return {"ceph_acl_user_"+permission for permission in ("full_control","read","read_acp","write","write_acp")}

def expected_object_encryption_diagnostics(configured=False):
    return {"ceph_encryption_"+name for name in ("s3_head","no_key","not_declared","read_declare")}|({"ceph_encryption_kms_head"} if configured else set())

def expected_kms_prerequisites():
    return {"ceph_encryption_kms_"+name for name in ("default_1b","default_1kb","default_1mb","default_8mb","transfer_1b","transfer_1kb","transfer_1MB","transfer_13b","head","present")}

def expected_encrypted_multipart_diagnostics():
    return {"ceph_sse_multipart_"+name for name in ("wrong_part_key","wrong_part_md5","bad_download","get_part","ordinary")}

def expected_control_diagnostics():
    return {"ceph_public_block_deny"}|{"ceph_lock_config_"+name for name in ("invalid_bucket","enable_after","invalid_days","invalid_years","invalid_mode","invalid_status","suspend","get")}

def expected_sse_diagnostics():
    return {"ceph_sse_c_"+name for name in ("head","present","other","invalid_md5","no_md5","no_key","no_algorithm")}

def expected_config_diagnostics(language):
    modes=() if language=="cpp" else ("id_long","same_id","invalid_status","invalid_date","transition_invalid_date")
    return {"ceph_config_lifecycle_"+mode for mode in modes}


@unittest.skipUnless(os.environ.get("OSV_RUN_SDK_TESTS") == "1", "enable explicitly after building both SDK workers")
class SdkIntegrationTest(unittest.TestCase):
    """Verify real SDK producers and consumers over HTTP rather than replacing storage calls in tests."""

    @classmethod
    def setUpClass(cls):
        """Start an isolated loopback service; use dummy credentials and require both real worker binaries."""
        import boto3
        from local_moto import LocalMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server = LocalMotoServer()
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        host, port = cls.server.get_host_and_port()
        cls.environment = patch.dict(os.environ, {
            "OSV_ACCESS_KEY": "local-test", "OSV_SECRET_KEY": "local-test", "OSV_SESSION_TOKEN": "",
            "OSV_ENDPOINT": f"http://{host}:{port}", "AWS_EC2_METADATA_DISABLED":"true","OSV_REGION": "us-east-1",
            "OSV_BUCKET": "osv-sdk-integration", "OSV_PATH_STYLE": "true", "OSV_RETRIES": "0",
            "OSV_ALT_ACCESS_KEY":"","OSV_ALT_SECRET_KEY":"","OSV_ALT_SESSION_TOKEN":"",
            "OSV_TENANT_ACCESS_KEY":"","OSV_TENANT_SECRET_KEY":"","OSV_BAD_AUTH_ACCESS_KEY":"","OSV_BAD_AUTH_SECRET_KEY":""})
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

    def test_sse_customer_source_cases_preserve_service_diagnostics_and_cleanup(self):
        """Execute all original SSE-C inputs through real SDKs; expect pinned Moto missing-header/key validation failures, positive transfers, and no leaked owned buckets."""
        from osv.ceph_sse_customer import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,CASES)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected_sse_diagnostics())
                    self.assertTrue(all(row["status"]=="pass" for row in rows if row["case"] not in expected_sse_diagnostics()))
                    self.assertEqual(len(rows),11)
                    self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_bucket_controls_source_inputs_keep_diagnostics_and_cleanup(self):
        """Execute actual SDK bucket-control scenarios; retain original service diagnostics and prove no objects or owned buckets remain."""
        from osv.ceph_bucket_controls import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,CASES)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected_control_diagnostics())
                    self.assertTrue(all(row["status"]=="pass" for row in rows if row["case"] not in expected_control_diagnostics()))
                    self.assertEqual(len(rows),17)
                    self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_encrypted_multipart_source_cases_and_cleanup(self):
        """Execute original 30-MiB encrypted uploads/ranges and part reads through actual SDKs; expect pinned validation/part diagnostics and no residual buckets or uploads."""
        from osv.ceph_sse_multipart import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,CASES)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected_encrypted_multipart_diagnostics())
                    self.assertTrue(all(row["status"]=="pass" for row in rows if row["case"] not in expected_encrypted_multipart_diagnostics()))
                    self.assertEqual(len(rows),7)
                    self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_object_encryption_with_configured_kms_and_without_prerequisite(self):
        """Run genuine SDK encryption requests with and without the source KMS input; expect exact missing prerequisites or service diagnostics and no leaked buckets."""
        from osv.ceph_encryption_objects import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            for configured in (False,True):
                with self.subTest(language=target["language"],configured=configured),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"kms_key_id":"independent-moto-test-key"} if configured else {})
                        rows=run_cases(ctx,CASES)
                        self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected_object_encryption_diagnostics(configured))
                        self.assertEqual({row["case"] for row in rows if row["status"]=="not_run"},set() if configured else expected_kms_prerequisites())
                        self.assertTrue(all(row["status"]=="pass" for row in rows if row["case"] not in expected_object_encryption_diagnostics(configured) and (configured or row["case"] not in expected_kms_prerequisites())))
                        self.assertEqual(len(rows),22)
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_alternate_policy_source_cases_require_configured_identity(self):
        """Run source policies with actual alternate SDK credentials; expect genuine local diagnostics and every owned bucket removed, without inferring cloud authorization certification."""
        from osv.ceph_bucket_policy import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"alternate-test-key","OSV_ALT_SECRET_KEY":"alternate-test-secret","OSV_ALT_SESSION_TOKEN":""}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        self.assertIn("identity_alt",worker.info["capabilities"])
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                        rows=run_cases(ctx,CASES)
                        self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},{row.name for row in CASES} if target["language"]=="cpp" else {"ceph_policy_deny_acl_v1","ceph_policy_deny_acl_v2"})
                        self.assertTrue(all(("XML_ERROR_PARSING_TEXT" in row["error"]) if target["language"]=="cpp" else ('expected service rejection (403' in row["error"] and '\"status_code\": 200' in row["error"]) for row in rows if row["status"]=="fail"),rows)
                        self.assertEqual(len(rows),6)
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_acl_source_cases_with_independent_identity_metadata(self):
        """Execute original canned/explicit ACL vectors through real SDKs using fixed independent Moto account metadata; retain source and service diagnostics and confirm every owned bucket is removed."""
        from osv.ceph_acl import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        identity={"canonical_id":"75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a","display_name":"webfile"}
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"alternate-test-key","OSV_ALT_SECRET_KEY":"alternate-test-secret"}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"identities":{"main":identity,"alt":{"canonical_id":"independent-alt-user","display_name":"alternate","email":"alternate@example.invalid"}}})
                        rows=run_cases(ctx,CASES)
                        self.assertEqual({row["case"] for row in rows if row["status"]=="pass"},{"ceph_acl_private_to_private","ceph_acl_revoke_all"})
                        self.assertTrue(all(row["status"]=="fail" for row in rows if row["case"] not in {"ceph_acl_private_to_private","ceph_acl_revoke_all"}),rows)
                        self.assertEqual(len(rows),26)
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_service_bucket_inventory_source_and_actual_cleanup(self):
        """Create source five-bucket datasets through genuine SDKs; expect membership/time checks and retained Moto anonymous-visibility mismatch, with no bucket leftovers."""
        from osv.ceph_bucket_inventory import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,CASES)
                    self.assertEqual([(row["case"],row["status"]) for row in rows],[("ceph_inventory_membership","pass"),("ceph_inventory_ctime","pass"),("ceph_inventory_anonymous","fail")],rows)
                    self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_original_v4_request_headers_keep_sdk_and_service_diagnostics(self):
        """Run original before-signing header changes through actual SDKs with bounded local timeouts; retain genuine normalizations/errors and confirm every header-test bucket is removed."""
        from osv.ceph_request_headers import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        with patch.dict(os.environ,{"OSV_HEADER_REQUEST_TIMEOUT_MS":"1000"}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,10)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                        rows=run_cases(ctx,CASES)
                        self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected_request_header_diagnostics())
                        self.assertTrue(all(row["status"] in ("pass","fail") for row in rows),rows)
                        self.assertEqual(len(rows),27)
                        self.assertEqual([bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_configured_storage_classes_execute_original_transition_input(self):
        """Provide the actual source's three-class prerequisite; expect Java to send the full original rule, C++ to report its missing legacy-Prefix model before HTTP, and no leaked buckets."""
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
                    if target["language"]=="cpp":
                        self.assertEqual(requests,[]);self.assertEqual(rows[0]["status"],"not_run",rows)
                        self.assertEqual(rows[0]["missing_operations"],["lifecycle_legacy_prefix"])
                    else:
                        self.assertEqual(len(requests),1)
                        rule=requests[0]["input"]["LifecycleConfiguration"]["Rules"][0]
                        self.assertEqual(rule["NoncurrentVersionTransitions"],[{"NoncurrentDays":2,"StorageClass":"GLACIER"},{"NoncurrentDays":4,"StorageClass":"DEEP_ARCHIVE"}])
                        self.assertEqual(rows[0]["status"],"pass",rows)
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

    def test_concurrent_overwrites_keep_genuine_requests_without_moto_buffer_race(self):
        """Run two real SDK lanes overwriting the same 2-MiB object through the production runner; expect all 32 measurements per SDK to succeed and actual object cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "compatibility_suites":["core_objects"],"performance_model":"operations","operations":["put"],
                "object_size":2*1024*1024,"range_size":17,"large_object_size":65536,"part_size":5*1024*1024,
                "concurrency":2,"requests":16,"rounds":2,"warmup_requests":0}}))
            report=run(load_config(path),directory/"results")
            self.assertEqual([row for row in report["cases"] if row["status"]=="fail"],[])
            self.assertEqual(len(report["samples"]),32*len(self.targets))
            self.assertTrue(all(sample["ok"] for sample in report["samples"]),[row for row in report["samples"] if not row["ok"]])
            self.assertEqual({row["op"] for row in report["samples"]},{"put"})
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"),0)

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
            expected.extend((target["id"],name) for target in self.targets for name in expected_sse_diagnostics())
            expected.extend((target["id"],name) for target in self.targets for name in expected_control_diagnostics())
            expected.extend((target["id"],name) for target in self.targets for name in expected_encrypted_multipart_diagnostics())
            expected.extend((target["id"],name) for target in self.targets for name in expected_object_encryption_diagnostics())
            expected.extend((target["id"],name) for target in self.targets for name in ("ceph_put_condition_match_existing","ceph_put_condition_none_etag","ceph_copy_condition_match_bad"))
            expected.extend((target["id"],name) for target in self.targets for name in expected_request_header_diagnostics())
            expected.extend((target["id"],name) for target in self.targets for name in ({"ceph_access_anonymous_v1_private","ceph_access_anonymous_v2_private","ceph_interop_put_anonymous_private","ceph_interop_upload_tags"}|{"ceph_interop_conflict_"+mode for mode in ("c_s3","c_kms","s3_kms","bad_enc_kms")}|({"ceph_interop_policy_roundtrip"} if target["language"]=="cpp" else set())))
            expected.extend((target["id"],"ceph_inventory_anonymous") for target in self.targets)
            expected += [(target["id"],name) for target in self.targets if target["language"]=="cpp"
                         for name in ("ceph_list_delimiter_space","ceph_list_delimiter_newline")]
            from osv.ceph_checksum_attributes import CASES as checksum_cases
            policy_fail={"ceph_policy_condition_"+name for name in ("ssec_required","ssec_algorithm","s3_noenc","s3_incorrect_algo_sse_s3","ifexists","notprincipal")}
            checksum_missing={"ceph_checksum_object_crc64nvme"}|{"ceph_checksum_helper_"+name for name in ("sha256","crc64nvme","crc32","crc32c","sha1")}
            for target in self.targets:
                extra=policy_fail|{"ceph_metadata_edges_content_encoding"}
                if target["language"]=="cpp":
                    extra=extra|{"ceph_policy_condition_s3_kms","ceph_policy_condition_kms_s3"}|{"ceph_metadata_edges_"+name for name in ("special_keys",)}
                    extra=extra|({item.name for item in checksum_cases}-{"ceph_attributes_object","ceph_attributes_versioned","ceph_checksum_attributes_sha256"})
                else:extra=extra|({item.name for item in checksum_cases}-checksum_missing)
                expected.extend((target["id"],name) for name in extra)
            expected.extend((target["id"],name) for target in self.targets for name in expected_conditional_version_diagnostics(target["language"])|expected_public_controls_diagnostics(target["language"]))
            state_fail={"ceph_state_"+name for name in ("condition_not_match","condition_not_modified","access_raw_get_object_acl","recreate_acl_overwrite","recreate_acl_new")}
            policy_tail_fail={"ceph_remaining_policy_bad_access_key","ceph_remaining_policy_bad_signature"}
            expected.extend((target["id"],name) for target in self.targets for name in state_fail|policy_tail_fail|({"ceph_remaining_policy_multipart_50mib"} if target["language"]=="cpp" else set()))
            new_failure_common={"ceph_query_"+mode for mode in ("head_usage","unordered_v1","unordered_v2","invalid_maxkeys","head_extended","response_headers")}|{"ceph_object_lock_invalid_get_retention","ceph_object_lock_invalid_get_legal_hold","ceph_lc_expiration_header_tag","ceph_names_nonalpha","ceph_tail_unreadable","ceph_encryption_tail_torrent"}
            for target in self.targets:
                extra={"ceph_lc_expiration_days_zero","ceph_lc_expiration_header_put","ceph_lc_expiration_header_head"} if target["language"]=="java" else set()
                expected.extend((target["id"],name) for name in new_failure_common|extra)
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
            self.assertEqual(len(report["coverage"]["catalog"]),769)
            from osv.ceph_access import CASES as access_cases
            access_missing={item.name for item in access_cases if "_matrix_" in item.name}|{"ceph_interop_tag_policy_"+mode for mode in ("get","put","delete")}
            policy_missing={"ceph_policy_condition_"+name for name in ("kms_noenc","existing_get","existing_get_tagging","existing_put_tagging","existing_get_acl","request_tag","acl","grant","copy_source","copy_metadata","head_prefix","multipart")}
            metadata_missing={"ceph_metadata_edges_"+name for name in ("owner_read","owner_full","owner_stable","attributes_stable","acl_headers","copy_canned","copy_granted")}
            public_missing={"ceph_public_controls_ignore_acl"}|{"ceph_public_ownership_"+when+mode for when in ("create_","put_") for mode in ("bucket_owner_enforced","bucket_owner_preferred","object_writer")}
            copy_missing={"ceph_copy_contract_other_owner","ceph_copy_contract_multipart_owners"}
            conditional_missing={"ceph_conditional_versions_bulk_"+kind+suffix for kind in ("match","mtime","size") for suffix in ("","_current","_version")}
            state_missing={"ceph_state_version_acl_explicit","ceph_state_version_acl_current"}
            basic_missing={"ceph_basic_expected_owner"}
            policy_tail_missing={"ceph_remaining_policy_tenant_global","ceph_remaining_policy_tenant_owned","ceph_remaining_policy_copy_parts"}
            logging_missing={"ceph_logging_controls_"+mode for mode in ("roundtrip","errors","owner","permissions","remove")}
            from osv.ceph_raw_http import CASES as raw_cases
            from osv.ceph_lifecycle_expiration import CASES as expiration_cases
            from osv.ceph_lifecycle_transition import CASES as transition_cases
            from osv.ceph_object_locks import CASES as lock_cases
            from osv.ceph_logging_records import CASES as record_cases
            new_missing={c.name for c in raw_cases if c.name!="ceph_raw_continue_retry"}|{"ceph_query_list_buckets_paginated"}|{c.name for c in lock_cases if c.name not in {"ceph_object_lock_invalid_get_retention","ceph_object_lock_invalid_get_legal_hold"}}|{c.name for c in record_cases}|{c.name for c in transition_cases}|{"ceph_lc_delete_marker_expiration"}|{c.name for c in expiration_cases if c.name not in {"ceph_lc_expiration_days_zero","ceph_lc_expiration_header_put","ceph_lc_expiration_header_head","ceph_lc_expiration_header_tag","ceph_lc_expiration_header_and_tags"}}
            from osv.ceph_cloud_lifecycle import CASES as cloud_cases
            from osv.ceph_logging_workloads import CASES as logging_workload_cases
            from osv.ceph_logging_extensions import CASES as logging_extension_cases
            from osv.ceph_encrypted_forms import CASES as encrypted_form_cases
            new_missing|={c.name for c in cloud_cases}|{c.name for c in logging_workload_cases}|{c.name for c in logging_extension_cases}|{c.name for c in encrypted_form_cases}|{"ceph_names_"+mode for mode in ("short_one","short_two","ip","underscore","dash_end","dot_dot","dot_dash","dash_dot","location","recreate_nonowner")}|{"ceph_tail_bucket_header_grants"}
            from osv.ceph_encryption_tail import CASES as encryption_tail_cases
            from osv.ceph_logging_policy_transitions import CASES as logging_transition_cases
            from osv.ceph_logging_prefixes import CASES as logging_prefix_cases
            new_missing|={c.name for c in encryption_tail_cases if c.name not in {"ceph_encryption_tail_torrent","ceph_encryption_tail_s3_default"}}|{c.name for c in logging_transition_cases}|{c.name for c in logging_prefix_cases}
            new_missing|={"ceph_encrypted_copy_matrix","ceph_encrypted_part_copy_matrix"}
            cpp_expiration_missing={"ceph_lc_expiration_days_zero","ceph_lc_expiration_header_put","ceph_lc_expiration_header_head"}
            missing=[row for row in report["cases"] if row["status"]=="not_run"]
            legacy_missing={"ceph_config_lifecycle_"+mode for mode in ("set","get","delete","no_id","id_long","same_id","invalid_status","date","invalid_date","transition_invalid_date","noncurrent","marker","multipart","noncurrent_transition")}
            self.assertEqual({(row["target"],row["case"]) for row in missing},{(target["id"],name) for target in self.targets for name in (new_missing|(legacy_missing|cpp_expiration_missing if target["language"]=="cpp" else set())|access_missing|policy_missing|metadata_missing|public_missing|copy_missing|state_missing|basic_missing|policy_tail_missing|logging_missing|(conditional_missing if target["language"]=="java" else set())|(checksum_missing if target["language"]=="java" else set())|{"ceph_config_lifecycle_noncurrent_transition"}|expected_kms_prerequisites()|expected_acl_metadata_prerequisites()|expected_acl_identity_prerequisites()|{"ceph_policy_deny_acl_v"+str(version) for version in (1,2)}|{"ceph_policy_list_v"+str(version)+suffix for version in (1,2) for suffix in ("","_another")})})
            for row in missing:
                if row["case"] in new_missing|cpp_expiration_missing:
                    self.assertIn(row["reason"],("missing_prerequisites","unsupported_operations"))
                    self.assertTrue(row.get("missing_requirements") or row.get("missing_operations"))
                    continue
                if row["target"]=="cpp" and row["case"] in legacy_missing:
                    self.assertEqual(row["reason"],"unsupported_operations");self.assertEqual(row["missing_operations"],["lifecycle_legacy_prefix"])
                    continue
                if row["case"] in state_missing|basic_missing|policy_tail_missing|logging_missing:
                    if row["case"] in policy_tail_missing|{"ceph_logging_controls_owner"}:
                        self.assertEqual(row["reason"],"unsupported_operations")
                        self.assertEqual(row["missing_operations"],["identity_tenant"] if row["case"].startswith("ceph_remaining_policy_tenant_") else ["identity_alt"])
                    elif row["case"] in {"ceph_logging_controls_roundtrip","ceph_logging_controls_errors"} and row["target"]=="java":
                        self.assertEqual(row["reason"],"unsupported_operations");self.assertEqual(row["missing_operations"],["logging_target_key_format"])
                    else:
                        self.assertEqual(row["reason"],"missing_prerequisites");self.assertTrue(row["missing_requirements"])
                    continue
                if row["case"] in public_missing|copy_missing|conditional_missing:
                    self.assertEqual(row["reason"],"unsupported_operations")
                    self.assertEqual(row["missing_operations"],["delete_many_conditions"] if row["case"] in conditional_missing else ["identity_alt"])
                    continue
                if row["case"] in checksum_missing:
                    self.assertEqual(row["reason"],"unsupported_operations")
                    self.assertTrue(row["missing_operations"])
                    continue
                if row["case"] in policy_missing:
                    if row["case"]=="ceph_policy_condition_kms_noenc":
                        self.assertEqual(row["reason"],"missing_prerequisites");self.assertEqual(row["missing_requirements"],["workload.kms_key_id"])
                    else:
                        self.assertEqual(row["reason"],"unsupported_operations");self.assertEqual(row["missing_operations"],["identity_alt"])
                    continue
                if row["case"] in metadata_missing:
                    if row["case"] in {"ceph_metadata_edges_owner_read","ceph_metadata_edges_owner_full","ceph_metadata_edges_owner_stable","ceph_metadata_edges_copy_canned","ceph_metadata_edges_copy_granted"}:
                        self.assertEqual(row["reason"],"unsupported_operations");self.assertEqual(row["missing_operations"],["identity_alt"])
                    else:
                        self.assertEqual(row["reason"],"missing_prerequisites");self.assertTrue(row["missing_requirements"])
                    continue
                if row["case"].startswith(("ceph_policy_","ceph_acl_user_","ceph_access_matrix_","ceph_interop_tag_policy_")):
                    self.assertEqual(row["reason"],"unsupported_operations");self.assertEqual(row["missing_operations"],["identity_alt"])
                else:
                    self.assertEqual(row["reason"],"missing_prerequisites")
                    self.assertEqual(row["missing_requirements"],["workload.identities.main.canonical_id","workload.identities.main.display_name"] if row["case"].startswith("ceph_acl_") else ["workload.storage_classes: at least 3"] if row["case"]=="ceph_config_lifecycle_noncurrent_transition" else ["workload.kms_key_id"])
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
