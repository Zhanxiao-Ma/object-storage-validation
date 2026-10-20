"""Exercise real SDK permission matrices and related object interoperability source vectors."""
import hashlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import FixtureHandler,fixture_worker

class PeerHandler(FixtureHandler):
    """Record actual TCP peers so separate production SDK sessions can be verified without substituting worker or client implementations."""
    def respond(self):
        if not hasattr(self.server,"peers"):self.server.peers=[]
        self.server.peers.append(self.client_address)
        return super().respond()
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

@unittest.skipUnless(os.environ.get("OSV_RUN_IDENTITY_TESTS")=="1","enable after building genuine SDK workers")
class AccessWireTest(unittest.TestCase):
    """Observe literal source bodies, signatures and distinct sessions through independent raw HTTP responses."""
    def test_original_encryption_conflict_and_upload_tags_reach_real_service(self):
        """Run two production source cases with independent 400 and ordered tagging XML; expect original empty encryption body, exact KMS/SSE-C headers and literal upload tag query, followed by full content/tag consumption and cleanup."""
        from osv.ceph_object_interop import conflict,tags_on_upload
        from osv.scenarios import Context
        from osv.suites import Probe
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        tags=b'<Tagging><TagSet><Tag><Key>bar</Key><Value></Value></Tag><Tag><Key>foo</Key><Value>bar</Value></Tag></TagSet></Tagging>'
        error=b'<Error><Code>InvalidArgument</Code><Message>conflicting declarations</Message></Error>'
        cleanup=[(200,{},b''),(204,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
        fixtures=[(404,{},b''),(200,{},b''),(400,{},error)]+cleanup
        fixtures += [(404,{},b''),(200,{},b''),(200,{"ETag":'"'+hashlib.md5(b'A'*100).hexdigest()+'"'},b''),(200,{},b'A'*100),(200,{},tags)]+cleanup
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"kms_key_id":"independent-kms-resource"}))
                conflict(probe,"c_kms");tags_on_upload(probe)
                self.assertEqual(len(server.requests),18)
                headers={key.lower():value for key,value in server.requests[2][2].items()}
                self.assertEqual(headers["x-amz-server-side-encryption"],"aws:kms")
                self.assertEqual(headers["x-amz-server-side-encryption-aws-kms-key-id"],"independent-kms-resource")
                self.assertEqual(headers["x-amz-server-side-encryption-customer-algorithm"],"AES256")
                self.assertEqual(headers["x-amz-server-side-encryption-customer-key"],"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=")
                self.assertEqual(headers["x-amz-server-side-encryption-customer-key-md5"],"DWygnHRtgiJ77HCm+1rvHw==")
                self.assertEqual(server.requests[2][3],b'')
                self.assertIn("x-amz-server-side-encryption-customer-key",headers["authorization"])
                upload=server.requests[10];headers={key.lower():value for key,value in upload[2].items()}
                self.assertEqual(headers["x-amz-tagging"],"foo=bar&bar");self.assertEqual(upload[3],b'A'*100)
    def test_permission_matrix_preserves_all_paths_and_fresh_clients(self):
        """Execute private/private, public-read/read and public-read-write/write production matrices; expect all source 403/allow decisions, exact bodies/list keys, real alternate signatures and fresh connections after denied writes."""
        from osv.ceph_access import matrix
        from osv.scenarios import Context
        from osv.suites import Probe
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        listing=b'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>bar</Key></Contents><Contents><Key>foo</Key></Contents></ListBucketResult>'
        denied=(403,{"Content-Type":"application/xml"},b'<Error><Code>AccessDenied</Code><Message>independent rejection</Message></Error>')
        def put(body):return (200,{"ETag":'"'+hashlib.md5(body).hexdigest()+'"'},b'')
        setup=[(404,{},b''),(200,{},b''),(200,{},b''),put(b'foocontent'),(200,{},b''),put(b'barcontent')]
        cleanup=[(200,{},b''),(204,{},b''),(204,{},b''),(204,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
        get=(200,{"ETag":'"'+hashlib.md5(b'foocontent').hexdigest()+'"'},b'foocontent')
        vectors=(("private","private",[denied]*6),("public-read","public-read",[get,denied,denied,denied,(200,{},listing),denied]),("public-read-write","public-read-write",[get,put(b'foooverwrite'),denied,put(b'baroverwrite'),(200,{},listing),put(b'newcontent')]))
        fixtures=[]
        for _,_,actions in vectors:fixtures.extend(setup+actions+cleanup)
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",overrides,PeerHandler) as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))
                for index,(bucket_acl,object_acl,_) in enumerate(vectors):
                    matrix(probe,bucket_acl,object_acl)
                    offset=index*19;requests=server.requests[offset:offset+19];self.assertEqual(len(requests),19)
                    for position,request in enumerate(requests):
                        headers={key.lower():value for key,value in request[2].items()}
                        self.assertIn("Credential="+("alternate-key" if 6<=position<12 else "local-test")+"/",headers["authorization"])
                    expected_bodies=[b'barcontent',b'baroverwrite',b'newcontent'] if index==0 else [b'foooverwrite',b'baroverwrite',b'newcontent']
                    self.assertEqual([request[3] for request in requests[6:12] if request[0]=="PUT"],expected_bodies)
                    peers=server.peers[offset:offset+19]
                    if index==0:
                        self.assertNotIn(peers[10],peers[6:10]);self.assertNotIn(peers[11],peers[6:11])
                    elif index==1:
                        self.assertNotIn(peers[8],peers[6:8]);self.assertNotIn(peers[10],peers[6:10])

@unittest.skipUnless(os.environ.get("OSV_RUN_SDK_TESTS")=="1","enable with Moto and genuine SDK workers")
class AccessServiceTest(unittest.TestCase):
    """Retain independent local-service diagnostics for every translated source case and confirm owned resource cleanup."""
    def test_access_and_interop_source_diagnostics_and_cleanup(self):
        """Run thirty source vectors with configured real alternate/anonymous clients; expect explicit pass/fail results and no remaining scoped buckets, including all early assertion failures."""
        import boto3
        from local_moto import LocalMotoServer
        from osv.ceph_access import CASES as access
        from osv.ceph_object_interop import CASES as interop
        from osv.scenarios import Context
        from osv.suites import run_cases
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        service=LocalMotoServer();service.start()
        try:
            host,port=service.get_host_and_port();endpoint=f"http://{host}:{port}"
            client=boto3.client("s3",endpoint_url=endpoint,region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
            overrides={"OSV_ENDPOINT":endpoint,"AWS_EC2_METADATA_DISABLED":"true","OSV_REGION":"us-east-1","OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
            for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
                with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",overrides) as (worker,_):
                    directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    rows=run_cases(ctx,access+interop)
                    expected={item.name for item in access if "_matrix_" in item.name}|{"ceph_access_anonymous_v1_private","ceph_access_anonymous_v2_private","ceph_interop_put_anonymous_private","ceph_interop_upload_tags"}|{"ceph_interop_conflict_"+mode for mode in ("c_s3","c_kms","s3_kms","bad_enc_kms")}
                    if language=="cpp":expected|={"ceph_interop_policy_roundtrip","ceph_interop_tag_policy_get","ceph_interop_tag_policy_put","ceph_interop_tag_policy_delete"}
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},expected)
                    self.assertEqual(len(rows),30)
                    self.assertTrue(all(row["status"] in ("pass","fail") for row in rows),rows)
                    self.assertEqual(client.list_buckets()["Buckets"],[])
        finally:service.stop()
