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
        """Run strict cases through real SDKs; expect Moto MaxKeys=0 plus C++ whitespace parsing discrepancies to fail and block performance."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            path.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{
                "profile":"complete","performance_model":"operations","object_size":1024,"range_size":17,
                "large_object_size":65536,"part_size":5*1024*1024,"concurrency":1,"requests":1,"rounds":1,"warmup_requests":0}}))
            report=run(load_config(path),directory/"results")
            failures=[(row["target"],row["case"]) for row in report["cases"] if row["status"]=="fail"]
            expected=[(target["id"],"ceph_list_maxkeys_zero") for target in self.targets]
            expected.extend((target["id"],"ceph_tags_excess") for target in self.targets)
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
