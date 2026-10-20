"""Verify source conditional-write vectors against actual SDK wire output and local service diagnostics."""
import os
import logging
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_WIRE_HEADER_TESTS")=="1","enable after building genuine SDK workers")
class ConditionalWriteWireTest(unittest.TestCase):
    """Pass independent source ETags through real GET/PUT and copy operations, observing signed HTTP requests."""
    def test_actual_source_etag_flows_into_condition_and_body_readback(self):
        """Execute actual translated PUT/copy cases with independent service ETags; expect source-specific quote handling, signed conditions, original bar/zar bytes and complete scoped cleanup."""
        from osv.ceph_conditional_writes import put_condition,copy_condition
        from osv.scenarios import Context
        from osv.suites import Probe
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        tag='"0123456789abcdef0123456789abcdef-7"'
        copytag='"37B51D194A7513E45B56F6524F2D51F2"'
        copy=b'<CopyObjectResult><ETag>"37b51d194a7513e45b56f6524f2d51f2"</ETag><LastModified>2025-01-02T03:04:05Z</LastModified></CopyObjectResult>'
        put_fixtures=[(404,{},b''),(200,{},b''),(200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b''),(200,{"ETag":tag},b'bar'),(200,{"ETag":'"b24d4be77066cb0bf70247b7d9176ebb"'},b''),(200,{"ETag":'"b24d4be77066cb0bf70247b7d9176ebb"'},b'zar'),(200,{},b''),(204,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
        copy_fixtures=[(404,{},b''),(200,{},b''),(200,{"ETag":copytag},b''),(200,{},copy),(200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b'bar'),(200,{},b''),(204,{},b''),(204,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for mode,fixtures in (("put",put_fixtures),("copy",copy_fixtures)):
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                    directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))
                    (put_condition if mode=="put" else copy_condition)(probe,"match_etag")
                    self.assertEqual(len(server.requests),11)
                    request=server.requests[4 if mode=="put" else 3];headers={key.lower():value for key,value in request[2].items()}
                    header="if-match" if mode=="put" else "x-amz-copy-source-if-match"
                    self.assertEqual(headers[header],tag.replace('"','') if mode=="put" else copytag)
                    self.assertIn(header,headers["authorization"])
                    if mode=="put":self.assertEqual(request[3],b"zar")

@unittest.skipUnless(os.environ.get("OSV_RUN_SDK_TESTS")=="1","enable with Moto and actual SDK workers")
class ConditionalWriteServiceTest(unittest.TestCase):
    """Preserve original Ceph failures on the independent local service; never normalize source 404/412 differences."""
    def test_all_original_vectors_keep_diagnostics_and_cleanup(self):
        """Run twelve original cases through both real SDKs against Moto; expect explicit per-case outcomes and zero remaining buckets rather than substituting successful condition responses."""
        import boto3
        from local_moto import LocalMotoServer
        from osv.ceph_conditional_writes import CASES
        from osv.scenarios import Context
        from osv.suites import run_cases
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        service=LocalMotoServer();service.start()
        try:
            host,port=service.get_host_and_port();endpoint=f"http://{host}:{port}"
            client=boto3.client("s3",endpoint_url=endpoint,region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
            for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
                with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",{"OSV_ENDPOINT":endpoint,"OSV_REGION":"us-east-1"}) as (worker,_):
                    directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,CASES)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},{"ceph_put_condition_match_existing","ceph_put_condition_none_etag","ceph_copy_condition_match_bad"})
                    self.assertTrue(all(row["status"] in ("pass","fail") for row in rows),rows)
                    self.assertEqual(len(rows),12)
                    self.assertEqual(client.list_buckets()["Buckets"],[])
        finally:service.stop()
