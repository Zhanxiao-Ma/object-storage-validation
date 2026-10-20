"""Optional real shared-SDK-client concurrency and basic Ceph boundary regressions."""
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from osv.ceph_basic_boundaries import CASES,concurrent_acl
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("OSV_RUN_BASIC_BOUNDARY_TESTS")=="1","enable after building actual shared-client SDK workers")
class BasicBoundaryWireTest(unittest.TestCase):
    """Observe genuine concurrent connections from a single persistent SDK worker using an independent HTTP synchronization barrier."""
    def test_fifty_acl_requests_are_simultaneously_active_on_one_worker(self):
        """Hold every native SDK ACL request until all 50 reach HTTP; expect one worker PID, 50 distinct active connections, original ACL XML and authoritative cleanup."""
        from sdk_fixtures import fixture_worker,FixtureHandler
        class FiftyAclHandler(FixtureHandler):
            """Synchronize real ACL HTTP requests without implementing storage, signing or ACL semantics."""
            def respond(self):
                if self.command=="PUT" and "acl" in parse_qs(urlsplit(self.path).query,keep_blank_values=True):
                    with self.server.lock:
                        self.server.active+=1;self.server.maximum=max(self.server.maximum,self.server.active)
                        self.server.peers.add(self.client_address)
                    try:
                        self.server.barrier.wait(timeout=10)
                        super().respond()
                    finally:
                        with self.server.lock:self.server.active-=1
                else:super().respond()
            do_PUT=respond
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b"")]+[(200,{},b"")]*50+[(200,{},b""),(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",{"OSV_CONNECTIONS":"50"},FiftyAclHandler) as (worker,server):
                server.socket.listen(128);server.lock=threading.Lock();server.active=0;server.maximum=0;server.peers=set();server.barrier=threading.Barrier(50)
                self.assertIn("concurrent_requests",worker.capabilities)
                pid=worker.process.pid;directory=Path(temporary)
                evidence=concurrent_acl(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})))
                self.assertEqual(worker.process.pid,pid);self.assertEqual(evidence["sdk_clients"],1)
                self.assertEqual(evidence["concurrent_requests"],50);self.assertTrue(evidence["cleanup_confirmed"])
                self.assertEqual(server.maximum,50);self.assertEqual(len(server.peers),50);self.assertEqual(server.active,0)
                acl=[request for request in server.requests if request[0]=="PUT" and "acl" in parse_qs(urlsplit(request[1]).query,keep_blank_values=True)]
                self.assertEqual(len(acl),50)
                self.assertEqual(len(server.requests),56)
                for request in acl:
                    headers={name.lower():value for name,value in request[2].items()}
                    self.assertEqual(headers["x-amz-acl"],"public-read");self.assertIn("x-amz-acl",headers["authorization"])


@unittest.skipUnless(os.environ.get("OSV_RUN_BASIC_BOUNDARY_TESTS")=="1","enable after building actual SDK workers")
class BasicBoundarySdkTest(unittest.TestCase):
    """Execute actual Java/C++ SDK source cases against an isolated local process and retain original service differences."""
    @classmethod
    def setUpClass(cls):
        """Start the common isolated local Moto service; all credentials and endpoints remain dummy loopback values."""
        import boto3
        from local_moto import LocalMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server=LocalMotoServer();cls.server.start();cls.addClassCleanup(cls.server.stop)
        host,port=cls.server.get_host_and_port()
        cls.environment=patch.dict(os.environ,{"OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"",
            "OSV_ENDPOINT":f"http://{host}:{port}","OSV_REGION":"us-east-1","OSV_BUCKET":"osv-basic-boundaries","OSV_CONNECTIONS":"50",
            "OSV_PATH_STYLE":"true","OSV_RETRIES":"0","OSV_ALT_ACCESS_KEY":"","OSV_ALT_SECRET_KEY":"","OSV_TENANT_ACCESS_KEY":"","OSV_TENANT_SECRET_KEY":""})
        cls.environment.start();cls.addClassCleanup(cls.environment.stop)
        cls.client=boto3.client("s3",endpoint_url=f"http://{host}:{port}",region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-basic-boundaries")
        cls.commands={"java":["java","-Xmx256m","-jar",str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"),"s3"],"cpp":[str(ROOT/"drivers/cpp/build/osv-cpp-s3")]}

    def test_original_basic_boundaries_preserve_outcomes_and_cleanup(self):
        """Preserve all five original oracles, actual shared-client concurrent version results and cleanup; local expected-owner behavior is retained as a source failure."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},60)
                try:
                    directory=Path(temporary)
                    rows=run_cases(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"identities":{"main":{"canonical_id":"independent-main-user"}}}),CASES)
                    self.assertEqual(len(rows),5)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},{"ceph_basic_expected_owner"},rows)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="pass"},{item.name for item in CASES}-{"ceph_basic_expected_owner"})
                    self.assertFalse(any(row["status"]=="not_run" for row in rows),rows)
                    evidence={row["case"]:row.get("evidence") for row in rows}
                    self.assertEqual(evidence["ceph_basic_concurrent_acl"],{"sdk_clients":1,"concurrent_requests":50,"cleanup_confirmed":True})
                    phased=evidence["ceph_basic_versions_phased"]
                    self.assertEqual(phased["sdk_clients"],1);self.assertEqual(phased["requests"],50)
                    self.assertEqual(phased["concurrent_requests_per_phase"],5);self.assertEqual(phased["source_thread_errors"],[])
                    self.assertTrue(phased["cleanup_confirmed"])
                    self.assertEqual({item["Name"] for item in self.client.list_buckets()["Buckets"]},{"osv-basic-boundaries"})
                finally:worker.close()
