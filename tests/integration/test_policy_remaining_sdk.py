"""Observe remaining Ceph policy/tenant/authentication requests through real AWS SDKs."""
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_policy_remaining import CASES,tenant,bad_auth,public_multipart,copy_parts
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_REMAINING_POLICY_TESTS")=="1","enable after building actual SDK workers")
class RemainingPolicySdkTest(unittest.TestCase):
    """Retain source inputs/oracles while validating actual transport, alternate bucket ownership and multipart cleanup."""
    @classmethod
    def setUpClass(cls):
        """Reuse isolated loopback setup; expect actual SDK workers and explicitly local credentials."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_tenant_namespaces_keep_original_identity_and_bare_policy_arns(self):
        """Replay one listed asdf object through both namespace directions; expect tenant-owned creation/cleanup or main-owned creation, opposite listing identity and exact :bucket/tenant:bucket syntax."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';one=b'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>asdf</Key><Size>4</Size></Contents></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(204,{},b""),(200,{},one),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_TENANT_ACCESS_KEY":"tenant-key","OSV_TENANT_SECRET_KEY":"tenant-secret"}
        for target in self.targets:
            for owned in (False,True):
                with self.subTest(language=target["language"],owned=owned),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"tenant_name":"exampletenant"})
                    tenant(Probe(ctx),owned);self.assertEqual(len(server.requests),11)
                    name=unquote(server.requests[1][1].split("?",1)[0]).strip("/");self.assertEqual(unquote(server.requests[4][1]).split("?",1)[0],"/"+("exampletenant:" if owned else ":")+name)
                    self.assertEqual(json.loads(server.requests[3][3])["Statement"][0]["Resource"],["arn:aws:s3:::"+name,"arn:aws:s3:::"+name+"/*"])
                    for index,request in enumerate(server.requests):
                        identity="local-test" if (owned and index==4) or (not owned and index!=4) else "tenant-key"
                        self.assertIn("Credential="+identity+"/",{k.lower():v for k,v in request[2].items()}["authorization"])
                    self.assertEqual(server.requests[2][3],b"asdf")

    def test_bad_authentication_uses_original_distinct_access_keys(self):
        """Run both actual bad-auth SDK clients against independent HTTP 403; expect badauth or actual main key in signatures, no session token and zero resource mutation."""
        denied=b'<Error><Code>AccessDenied</Code></Error>'
        for target in self.targets:
            for signature in (False,True):
                with self.subTest(language=target["language"],signature=signature),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],[(403,{},denied)],"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    bad_auth(Probe(ctx),signature);self.assertEqual(len(server.requests),1);self.assertEqual(server.requests[0][0],"GET")
                    headers={k.lower():v for k,v in server.requests[0][2].items()};self.assertIn("Credential="+("local-test" if signature else "badauth")+"/",headers["authorization"]);self.assertNotIn("x-amz-security-token",headers)

    def test_fifty_mib_policy_upload_uses_actual_part_etags_and_original_load(self):
        """Replay ten actual five-MiB SDK PUTs and independent ETags into real CompleteMultipartUpload; expect exact total/part sizes, stripped original ETags, source wildcard policy and abort-after-completion cleanup."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';upload=b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>foo</Key><UploadId>source-fifty-mib</UploadId></InitiateMultipartUploadResult>';complete=b'<CompleteMultipartUploadResult><Bucket>fixture</Bucket><Key>foo</Key><ETag>"final"</ETag></CompleteMultipartUploadResult>'
        fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(200,{},upload)]+[(200,{"ETag":'"independent-'+str(i)+'"'},b"") for i in range(1,11)]+[(200,{},complete),(404,{},b'<Error><Code>NoSuchUpload</Code></Error>'),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                public_multipart(Probe(ctx));self.assertEqual(len(server.requests),22)
                item=json.loads(server.requests[2][3])["Statement"][0];self.assertEqual(item["Principal"],"*");self.assertEqual(item["Action"],"*")
                self.assertEqual([len(r[3]) for r in server.requests[4:14]],[5*1024*1024]*10)
                for index,request in enumerate(server.requests[4:14],1):self.assertIn("partNumber="+str(index),request[1]);self.assertIn("uploadId=source-fifty-mib",request[1])
                root=ET.fromstring(server.requests[14][3]);actual=[]
                for part in root:
                    fields={n.tag.rsplit("}",1)[-1]:n.text for n in part};actual.append((int(fields["PartNumber"]),fields["ETag"]))
                self.assertEqual(actual,[(i,"independent-"+str(i)) for i in range(1,11)])
                self.assertEqual(server.requests[15][0],"DELETE");self.assertIn("uploadId=source-fifty-mib",server.requests[15][1])

    def test_default_group_keeps_moto_diagnostics_and_identity_prerequisites(self):
        """Run all six source functions with no alt/tenant credentials; expect three identity prerequisites, Java fifty-MiB success, genuine authentication diagnostics and no residual buckets."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                d=Path(temporary);worker=Worker(target,30)
                try:
                    rows=run_cases(Context(worker,"unused/",d,b"independent",d/"body",d/"output",{}),CASES)
                    missing={"ceph_remaining_policy_tenant_global","ceph_remaining_policy_tenant_owned","ceph_remaining_policy_copy_parts"}
                    passed={"ceph_remaining_policy_multipart_50mib"} if target["language"]=="java" else set()
                    self.assertEqual({r["case"] for r in rows if r["status"]=="not_run"},missing)
                    self.assertEqual({r["case"] for r in rows if r["status"]=="pass"},passed)
                    self.assertEqual({r["case"] for r in rows if r["status"]=="fail"},{c.name for c in CASES}-missing-passed)
                    self.assertEqual(len(rows),6);self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

    def test_policy_copy_parts_uses_alt_owned_bucket_and_actual_copy_etags(self):
        """Feed real UploadPartCopy outputs into CompleteMultipartUpload for both original public keys; expect alt-owned destination, unchanged quoted ETags, exact bodies and private source 403 with actual upload aborts."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';missing=(404,{},b'<Error><Code>NoSuchUpload</Code></Error>');denied=b'<Error><Code>AccessDenied</Code></Error>'
        def initiated(ident):return ("<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>target</Key><UploadId>"+ident+"</UploadId></InitiateMultipartUploadResult>").encode()
        def copied(tag):return ("<CopyPartResult><ETag>&quot;"+tag+"&quot;</ETag><LastModified>2026-10-01T00:00:00Z</LastModified></CopyPartResult>").encode()
        complete=b'<CompleteMultipartUploadResult><Bucket>fixture</Bucket><Key>target</Key><ETag>"complete"</ETag></CompleteMultipartUploadResult>'
        fixtures=[(404,{},b""),(200,{},b"")]+[(200,{},b"")]*3+[(204,{},b""),(404,{},b""),(200,{},b""),(200,{},initiated("copy-first")),(200,{},copied("etag-first")),(200,{},complete),(200,{},b"public/foo"),missing,(200,{},initiated("copy-second")),(200,{},copied("etag-second")),(200,{},complete),(200,{},b"public/bar"),missing,(200,{},initiated("copy-private")),(403,{},denied),(204,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b""),(204,{},b""),(200,{},b"")]+[(204,{},b"")]*3+[(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                copy_parts(Probe(ctx));self.assertEqual(len(server.requests),35)
                self.assertEqual([r[3] for r in server.requests[2:5]],[b"public/foo",b"public/bar",b"private/foo"])
                self.assertTrue(json.loads(server.requests[5][3])["Statement"][0]["Resource"].endswith("/public/*"))
                for index in range(6,27):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                for index,source in ((9,"public/foo"),(14,"public/bar"),(19,"private/foo")):self.assertTrue(unquote({k.lower():v for k,v in server.requests[index][2].items()}["x-amz-copy-source"]).endswith("/"+source))
                for index,tag in ((10,'"etag-first"'),(15,'"etag-second"')):
                    root=ET.fromstring(server.requests[index][3]);etags=[n.text for n in root.iter() if n.tag.rsplit("}",1)[-1]=="ETag"];self.assertEqual(etags,[tag])
                for index,ident in ((12,"copy-first"),(17,"copy-second"),(20,"copy-private")):self.assertEqual(server.requests[index][0],"DELETE");self.assertIn("uploadId="+ident,server.requests[index][1])
