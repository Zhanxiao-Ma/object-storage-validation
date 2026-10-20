"""Exercise translated copy contracts through actual SDK producers, consumers and cleanup."""
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_copy_contracts import CASES,version_copies

@unittest.skipUnless(os.environ.get("OSV_RUN_COPY_CONTRACT_TESTS")=="1","enable after building actual Java/C++ SDK workers")
class CopyContractsSdkTest(unittest.TestCase):
    """Keep the source copy/version assertions and observe real SDK requests without implementing object-store rules in tests."""
    @classmethod
    def setUpClass(cls):
        """Reuse only the local service/real-worker fixture; source cases still run through this module's production scenarios."""
        from test_s3_sdk import SdkIntegrationTest
        SdkIntegrationTest.setUpClass.__func__(cls)

    def test_original_copy_contracts_retain_service_diagnostics_and_cleanup(self):
        """Run all 16 source contracts against local Moto; expect explicit identity prerequisites, preserved SDK/service differences and no owned bucket leftovers."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    results=run_cases(ctx,CASES)
                    self.assertEqual(len(results),16)
                    expected_not_run={"ceph_copy_contract_other_owner","ceph_copy_contract_multipart_owners"}
                    self.assertEqual({row["case"] for row in results if row["status"]=="not_run"},expected_not_run,results)
                    self.assertEqual({row["case"] for row in results if row["status"]=="pass"},{item.name for item in CASES}-expected_not_run,results)
                    self.assertFalse([row for row in results if row["status"]=="fail"],results)
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_configured_alternate_identity_keeps_permission_and_owner_diagnostics(self):
        """Configure genuine alternate clients and independent expected identities; expect original permission/owner mismatches to remain failures and both users' resources cleaned."""
        selected=[item for item in CASES if item.name in {"ceph_copy_contract_other_owner","ceph_copy_contract_multipart_owners"}]
        identities={"main":{"canonical_id":"independent-main","display_name":"expected-main"},"alt":{"canonical_id":"independent-alt","display_name":"expected-alt"}}
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"local-alt","OSV_ALT_SECRET_KEY":"local-alt","OSV_ALT_SESSION_TOKEN":""}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"identities":identities})
                        results=run_cases(ctx,selected)
                        self.assertEqual(len(results),2)
                        self.assertEqual({row["case"] for row in results if row["status"]=="fail"},{item.name for item in selected},results)
                        self.assertFalse([row for row in results if row["status"]!="fail"],results)
                        self.assertIn("403",results[0]["error"])
                        self.assertIn("independent-main",results[1]["error"])
                    finally:worker.close()
                    self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_version_copy_consumes_actual_version_ids_and_original_contents(self):
        """Run the genuine three-version copy scenario with independent service IDs; expect each returned VersionId to feed actual CopySource twice, final copy to omit it and original content-0/1/2 assertions plus cleanup to hold."""
        enabled=b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
        copied=b"<CopyObjectResult><ETag>service-copy-etag</ETag></CopyObjectResult>"
        empty=b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
        empty_versions=b"<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>"
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{"Content-Type":"application/xml"},enabled)]
        ids=["independent-version-0+/%?&=","independent-version-1","independent-version-2"]
        fixtures += [(200,{"x-amz-version-id":ident},b"") for ident in ids]
        for i in range(3):fixtures += [(200,{"Content-Type":"application/xml"},copied),(200,{},("content-"+str(i)).encode())]
        fixtures += [(404,{},b""),(200,{},b"")]
        for i in (0,1,2,2):fixtures += [(200,{"Content-Type":"application/xml"},copied),(200,{},("content-"+str(i)).encode())]
        fixtures += [(200,{},b"")]+[(204,{},b"")]*4+[(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        fixtures += [(200,{},b""),(200,{"Content-Type":"application/xml"},empty_versions),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                version_copies(Probe(ctx))
                copies=[{k.lower():v for k,v in request[2].items()}["x-amz-copy-source"] for request in server.requests if "x-amz-copy-source" in {k.lower():v for k,v in request[2].items()}]
                self.assertEqual(len(copies),7)
                for source,ident in zip(copies[:6],ids+ids):
                    # Decode only the query value after proving its delimiter is literal;
                    # decoding the whole header would hide a version query encoded as key text.
                    self.assertIn("?versionId=",source)
                    self.assertEqual(unquote(source.split("?versionId=",1)[1]),ident)
                self.assertNotIn("?versionId=",copies[-1])
                self.assertEqual([request[3] for request in server.requests if request[0]=="PUT" and request[1].endswith("/testobj")],[b"content-0",b"content-1",b"content-2"])
                self.assertEqual(len(server.requests),len(fixtures));self.assertEqual(server.requests[-1][0],"HEAD")

    def test_versioned_copy_source_keeps_key_and_query_boundaries(self):
        """Copy sources with ?/%/+/space keys and absent/empty/null/opaque version IDs; expect separately encoded key/value, a literal query delimiter only when supplied and actual official signatures/results."""
        from urllib.parse import quote
        source_key="space +/percent%?versionId=literal&key"
        versions=(None,"","null","opaque+/%?&=value")
        payloads=[b"<CopyObjectResult><ETag>normal-copy-etag</ETag></CopyObjectResult>",b"<CopyPartResult><ETag>multipart-copy-etag</ETag></CopyPartResult>"]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},payload) for payload in payloads]*len(versions),"s3") as (worker,server):
                for version in versions:
                    for op in ("copy","mp_copy"):
                        request={"op":op,"key":"destination","source_bucket":"source-bucket","source_key":source_key}
                        if op=="mp_copy":request.update(upload_id="real-upload-id",part_number=1)
                        if version is not None:request["source_version_id" if op=="copy" else "version_id"]=version
                        result=worker.call(request);self.assertTrue(result["ok"],result)
                        self.assertEqual(result["etag"],"normal-copy-etag" if op=="copy" else "multipart-copy-etag")
                for request,version in zip(server.requests,[version for version in versions for _ in (0,1)]):
                    headers={k.lower():v for k,v in request[2].items()};source=headers["x-amz-copy-source"]
                    expected=quote("source-bucket/"+source_key,safe="/")
                    if version is not None:
                        # A literal slash is legal within a query value. The pinned Java
                        # SDK leaves it literal; C++ encodes it. Neither may encode the
                        # query delimiter as key text or leave +/%/?/&/= ambiguous.
                        expected+="?versionId="+quote(version,safe="/" if language=="java" else "")
                    self.assertEqual(source.lstrip("/"),expected)
                    self.assertIn("x-amz-copy-source",headers["authorization"])
