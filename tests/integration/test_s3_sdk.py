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
                "workload": {"object_size": 8192, "range_size": 127, "part_size": 5*1024*1024,
                             "profile": "complete", "concurrency": 2, "requests": 5, "rounds": 2, "warmup_requests": 1,
                             "operations": ["get", "range", "put", "head", "list"]}}))
            report = run(load_config(manifest), directory/"results")
            self.assertEqual(report["coverage"]["profile"], "complete")
            self.assertEqual(len(report["coverage"]["catalog"]), 32)
            self.assertFalse([case for case in report["cases"] if case["status"] == "fail"], report["cases"])
            self.assertEqual(len(report["summary"]), 5*len(self.targets))
            self.assertEqual(len(report["samples"]), 55*len(self.targets))
            self.assertFalse([case for case in report["cases"] if case["status"] == "fail"], report["cases"])
            self.assertTrue(all(sample["ok"] for sample in report["samples"]), report["samples"])
            self.assertEqual(report["comparisons"], [])  # cross-language results must not imply an SDK comparison
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-sdk-integration").get("KeyCount"), 0)

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
