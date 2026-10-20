"""Optional real Java/C++ SDK regression for original Ceph metadata relationships."""
import logging
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote
from unittest.mock import patch
from osv.ceph_metadata_edges import CASES,compare_dates
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[2]


class MetadataDateBoundaryTest(unittest.TestCase):
    """Check the source date comparator's asymmetric precision against independent timestamps."""
    def test_only_first_timestamp_is_truncated(self):
        """Keep source microsecond truncation on the first argument only; fractional second arguments must fail."""
        compare_dates(1700000000123,1700000000000)
        compare_dates(1700000000000,1700000000000)
        with self.assertRaises(AssertionError):compare_dates(1700000000000,1700000000123)
        with self.assertRaises(AssertionError):compare_dates(1700000000999,1700000001000)
        with self.assertRaises(AssertionError):compare_dates(1700000001000,1700000000000)


@unittest.skipUnless(os.environ.get("OSV_RUN_METADATA_EDGES_TESTS")=="1","enable after building both real SDK workers")
class MetadataEdgesSdkTest(unittest.TestCase):
    """Run production SDK producers and consumers against an isolated local HTTP service, retaining service incompatibilities."""
    @classmethod
    def setUpClass(cls):
        """Start a serial local Moto service and explicit main/alternate SDK credentials; no cloud endpoints are contacted."""
        import boto3
        from local_moto import LocalMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server=LocalMotoServer();cls.server.start();cls.addClassCleanup(cls.server.stop)
        host,port=cls.server.get_host_and_port()
        cls.environment=patch.dict(os.environ,{"OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"",
            "OSV_ALT_ACCESS_KEY":"alternate-test","OSV_ALT_SECRET_KEY":"alternate-test","OSV_ALT_SESSION_TOKEN":"",
            "OSV_ENDPOINT":f"http://{host}:{port}","OSV_REGION":"us-east-1","OSV_BUCKET":"osv-metadata-edges",
            "OSV_PATH_STYLE":"true","OSV_RETRIES":"0","OSV_TENANT_ACCESS_KEY":"","OSV_TENANT_SECRET_KEY":""})
        cls.environment.start();cls.addClassCleanup(cls.environment.stop)
        cls.client=boto3.client("s3",endpoint_url=f"http://{host}:{port}",region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-metadata-edges")
        cls.commands={"java":["java","-Xmx256m","-jar",str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"),"s3"],
            "cpp":[str(ROOT/"drivers/cpp/build/osv-cpp-s3")]}
        cls.languages=os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")
        if not cls.languages or set(cls.languages)-set(cls.commands):raise ValueError("OSV_S3_TEST_LANGUAGES must select java,cpp")

    def test_original_metadata_relationships_and_cleanup(self):
        """Execute all 18 original metadata/ACL/copy scenarios; preserve actual known service diagnostics and prove every owned bucket is removed."""
        identity={"canonical_id":"75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a","display_name":"webfile"}
        workload={"identities":{"main":identity,"alt":{"canonical_id":"independent-alt-user","display_name":"alternate"}}}
        for language in self.languages:
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},30)
                try:
                    directory=Path(temporary)
                    context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)
                    rows=run_cases(context,CASES)
                    self.assertEqual(len(rows),18)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="not_run"},set())
                    expected={"ceph_metadata_edges_owner_read","ceph_metadata_edges_owner_full","ceph_metadata_edges_acl_headers","ceph_metadata_edges_content_encoding"}
                    if language=="cpp":expected.update({"ceph_metadata_edges_special_keys","ceph_metadata_edges_copy_versions","ceph_metadata_edges_copy_encoded"})
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected,rows)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="pass"},{item.name for item in CASES}-expected)
                    failures={row["case"]:row["error"] for row in rows if row["status"]=="fail"}
                    self.assertIn("list index out of range",failures["ceph_metadata_edges_owner_read"])
                    self.assertIn("list index out of range",failures["ceph_metadata_edges_owner_full"])
                    self.assertIn("FULL_CONTROL",failures["ceph_metadata_edges_acl_headers"])
                    self.assertIn("gzip, aws-chunked",failures["ceph_metadata_edges_content_encoding"])
                    if language=="cpp":
                        self.assertIn("specified key does not exist",failures["ceph_metadata_edges_copy_versions"])
                        self.assertIn("specified key does not exist",failures["ceph_metadata_edges_copy_encoded"])
                    self.assertEqual({item["Name"] for item in self.client.list_buckets()["Buckets"]},{"osv-metadata-edges"})
                finally:worker.close()

    def test_missing_independent_identity_metadata_stays_not_run(self):
        """Do not infer expected identities from the service: metadata-dependent ACL scenarios must report explicit missing prerequisites."""
        names={"ceph_metadata_edges_owner_read","ceph_metadata_edges_owner_full","ceph_metadata_edges_owner_stable",
            "ceph_metadata_edges_attributes_stable","ceph_metadata_edges_acl_headers","ceph_metadata_edges_copy_granted"}
        cases=tuple(item for item in CASES if item.name in names)
        for language in self.languages:
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},30)
                try:
                    directory=Path(temporary)
                    rows=run_cases(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}),cases)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="not_run"},names)
                    self.assertTrue(all(row.get("missing_requirements") for row in rows),rows)
                    self.assertEqual({item["Name"] for item in self.client.list_buckets()["Buckets"]},{"osv-metadata-edges"})
                finally:worker.close()


@unittest.skipUnless(os.environ.get("OSV_RUN_METADATA_EDGES_TESTS")=="1","enable after building real SDK workers")
class MetadataCopyWireTest(unittest.TestCase):
    """Inspect real typed SDK CopyObject serialization with independent HTTP responses rather than an emulated copy implementation."""
    def test_reserved_copy_source_version_and_acl_wire_values(self):
        """Keep raw key/version reserved characters through one URL decoding and verify typed public-read ACL appears in the signed request."""
        from sdk_fixtures import fixture_worker
        response=b'<CopyObjectResult><ETag>"independent-copy-etag"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></CopyObjectResult>'
        key="folder/a?b&c +%/é";version="ver+1/2?x&y=%25"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},response)],"s3") as (worker,server):
                self.assertIn("copy_source_version",worker.capabilities);self.assertIn("copy_acl",worker.capabilities)
                observed=worker.call({"op":"copy","bucket":"destination","key":"new?key&value","source_bucket":"source-bucket",
                    "source_key":key,"source_version_id":version,"acl":"public-read"})
                self.assertTrue(observed["ok"],observed);self.assertEqual(observed["etag"],'"independent-copy-etag"')
                self.assertEqual(len(server.requests),1)
                headers={name.lower():value for name,value in server.requests[0][2].items()}
                # These pinned SDKs intentionally expose distinct query-separator serialization.
                self.assertEqual("?versionId=" in headers["x-amz-copy-source"],language=="java")
                if language=="cpp":self.assertIn("%3FversionId%3D",headers["x-amz-copy-source"])
                self.assertEqual(unquote(headers["x-amz-copy-source"]).lstrip("/"),"source-bucket/"+key+"?versionId="+version)
                self.assertEqual(headers["x-amz-acl"],"public-read")
                self.assertIn("x-amz-acl",headers["authorization"]);self.assertIn("x-amz-copy-source",headers["authorization"])


    def test_translated_version_copy_consumes_actual_head_version(self):
        """Drive the real encoded-key scenario with independent HEAD version IDs; expect that exact producer output in CopyObject and verified cleanup."""
        from sdk_fixtures import fixture_worker
        from osv.ceph_metadata_edges import version_copy
        from osv.suites import Probe
        version="actual-head+source-id"
        empty=b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>'
        versions=b'<ListVersionsResult><IsTruncated>false</IsTruncated><Version><Key>foo?bar</Key><VersionId>put-id</VersionId></Version><Version><Key>bar&amp;foo</Key><VersionId>copy-id</VersionId></Version></ListVersionsResult>'
        copied=b'<CopyObjectResult><ETag>"copied"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></CopyObjectResult>'
        xml={"Content-Type":"application/xml"}
        head={"ETag":'"source"',"Last-Modified":"Thu, 01 Jan 2026 00:00:00 GMT","x-amz-version-id":version}
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,xml,b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>'),
            (200,{"x-amz-version-id":"put-id"},b""),(200,head,b""),(200,xml,copied),(200,{**head,"x-amz-version-id":"copy-id"},b""),
            (200,{},b""),(200,xml,versions),(204,{},b""),(204,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary)
                version_copy(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})),True)
                self.assertEqual(len(server.requests),15)
                headers={name.lower():value for name,value in server.requests[6][2].items()}
                bucket=server.requests[0][1].strip("/").split("?",1)[0]
                self.assertEqual(unquote(headers["x-amz-copy-source"]).lstrip("/"),bucket+"/foo?bar?versionId="+version)
                self.assertEqual(server.requests[5][0],"HEAD");self.assertEqual(server.requests[7][0],"HEAD")
