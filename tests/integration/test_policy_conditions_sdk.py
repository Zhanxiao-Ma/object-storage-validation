"""Exercise migrated Ceph condition cases through actual Java/C++ SDKs and independent HTTP fixtures."""
import os
import json
import xml.etree.ElementTree as ET
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_policy_conditions import CASES,tag_existing,customer,request_tag
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_POLICY_CONDITION_TESTS")=="1","enable after building actual SDK workers")
class PolicyConditionsSdkTest(unittest.TestCase):
    """Keep original Ceph failures visible while checking actual SDK traffic and owned-resource cleanup."""
    @classmethod
    def setUpClass(cls):
        """Reuse the isolated local service setup; expect genuine Java/C++ binaries and no live cloud credentials."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_migrated_conditions_keep_real_diagnostics_and_cleanup(self):
        """Execute all original condition vectors with real alternate credentials; expect pinned local service differences and no owned bucket leaks."""
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret","OSV_ALT_SESSION_TOKEN":""}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);worker=Worker(target,30)
                    try:
                        ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                        rows=run_cases(ctx,CASES)
                        not_run={"ceph_policy_condition_kms_noenc","ceph_policy_condition_grant"}
                        passed={"ceph_policy_condition_s3_kms","ceph_policy_condition_kms_s3"} if target["language"]=="java" else set()
                        self.assertEqual({r["case"] for r in rows if r["status"]=="not_run"},not_run)
                        self.assertEqual({r["case"] for r in rows if r["status"]=="pass"},passed)
                        self.assertEqual({r["case"] for r in rows if r["status"]=="fail"},{c.name for c in CASES}-not_run-passed)
                        self.assertTrue(all("missing_prerequisites"==r["reason"] for r in rows if r["status"]=="not_run"))
                        if target["language"]=="cpp":self.assertTrue(all("XML_ERROR_PARSING_TEXT" in r["error"] for r in rows if r["status"]=="fail" and r["case"]!="ceph_policy_condition_multipart"))
                        self.assertEqual(len(rows),20)
                        self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_customer_policy_inputs_and_real_success_failure_paths(self):
        """Run both original SSE-C policies with independent HTTP 403/200 replies; expect exact policy condition, body/key/MD5 and policy removal before bucket cleanup."""
        denied=b'<Error><Code>AccessDenied</Code><Message>independent denied response</Message></Error>'
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(403,{},denied),(200,{},b""),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        for target in self.targets:
            for mode in ("required","algorithm"):
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    customer(Probe(ctx),mode)
                    self.assertEqual(len(server.requests),11)
                    policy_doc=json.loads(server.requests[2][3]);item=policy_doc["Statement"][0]
                    expected_condition={"Null":{"s3:x-amz-server-side-encryption-customer-algorithm":"true"}} if mode=="required" else {"StringNotEquals":{"s3:x-amz-server-side-encryption-customer-algorithm":"AES256"}}
                    self.assertEqual(item["Condition"],expected_condition);self.assertEqual(item["Action"],"s3:PutObject");self.assertEqual(item["Effect"],"Deny");self.assertEqual(item["Principal"],{"AWS":"*"})
                    headers=[{k.lower():v for k,v in r[2].items()} for r in server.requests]
                    self.assertEqual(server.requests[3][3],b"bar" if mode=="required" else b"")
                    self.assertEqual(headers[3].get("x-amz-server-side-encryption-customer-algorithm"),None if mode=="required" else "AES192")
                    self.assertNotIn("x-amz-server-side-encryption-customer-key",headers[3])
                    self.assertEqual(server.requests[4][3],b"bar")
                    self.assertEqual(headers[4]["x-amz-server-side-encryption-customer-algorithm"],"AES256")
                    self.assertEqual(headers[4]["x-amz-server-side-encryption-customer-key"],"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=")
                    self.assertEqual(headers[4]["x-amz-server-side-encryption-customer-key-md5"],"DWygnHRtgiJ77HCm+1rvHw==")
                    self.assertEqual(server.requests[5][0],"DELETE");self.assertIn("policy",server.requests[5][1]);self.assertEqual(server.requests[7][0],"DELETE");self.assertTrue(server.requests[7][1].endswith("/foo"))

    def test_existing_tag_transition_uses_actual_sdk_tags_and_alternate_signer(self):
        """Replay independent denied writes around a real public-to-private tag transition; expect all original tag XMLs, alt signatures and later denial without manufacturing SDK output."""
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b"")]+[(200,{},b"")]*3+[(204,{},b"")]+[(200,{},b"")]*3+[(403,{},denied),(200,{},b""),(403,{},denied),(204,{},b""),(200,{},b"")]+[(204,{},b"")]*3+[(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                tag_existing(Probe(ctx),"put_tagging")
                self.assertEqual(len(server.requests),20)
                self.assertEqual([r[3] for r in server.requests[2:5]],[b"publictag",b"privatetag",b"invalidtag"])
                self.assertEqual(json.loads(server.requests[5][3])["Statement"][0]["Condition"],{"StringEquals":{"s3:ExistingObjectTag/security":"public"}})
                def tags(index):
                    root=ET.fromstring(server.requests[index][3]);return [(next(v.text for v in tag if v.tag.rsplit("}",1)[-1]=="Key"),next(v.text for v in tag if v.tag.rsplit("}",1)[-1]=="Value")) for tag in root.iter() if tag.tag.rsplit("}",1)[-1]=="Tag"]
                for index,expected in ((6,[("security","public"),("foo","bar")]),(7,[("security","private")]),(8,[("security","public"),("foo","bar")]),(9,[("security","public"),("foo","bar")]),(10,[("security","private")]),(11,[("security","public"),("foo","bar")])):self.assertEqual(tags(index),expected)
                for index in range(8,12):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                self.assertEqual(server.requests[12][0],"DELETE");self.assertIn("policy",server.requests[12][1])

    def test_source_request_tag_hook_defect_stays_visible_and_cleans(self):
        """Give both untagged alt writes independent 403 responses; expect first rejection accepted, second source success oracle to fail, and finally policy/bucket cleanup to execute."""
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(403,{},denied),(403,{},denied),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                with self.assertRaises(RuntimeError):request_tag(Probe(ctx))
                self.assertEqual(len(server.requests),11)
                for index in (3,4):
                    headers={k.lower():v for k,v in server.requests[index][2].items()}
                    self.assertNotIn("x-amz-tagging",headers);self.assertIn("Credential=alternate-key/",headers["authorization"]);self.assertEqual(server.requests[index][3],b"testobj")
                self.assertEqual(server.requests[5][0],"DELETE");self.assertIn("policy",server.requests[5][1]);self.assertEqual(server.requests[-1][0],"HEAD")

    def test_get_acl_source_negative_operations_are_not_silently_repaired(self):
        """Replay original GetACL success then GET/tagging denials; expect negative private/invalid calls to remain GetObjectTagging exactly as pinned upstream."""
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';acl=b'<AccessControlPolicy><Owner><ID>independent-owner</ID></Owner><AccessControlList/></AccessControlPolicy>'
        fixtures=[(404,{},b""),(200,{},b"")]+[(200,{},b"")]*3+[(204,{},b"")]+[(200,{},b"")]*3+[(200,{},acl)]+[(403,{},denied)]*3+[(204,{},b""),(200,{},b"")]+[(204,{},b"")]*3+[(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                tag_existing(Probe(ctx),"get_acl")
                self.assertEqual(len(server.requests),21)
                self.assertIn("acl",server.requests[9][1]);self.assertIn("/publictag",server.requests[9][1])
                self.assertNotIn("?",server.requests[10][1]);self.assertTrue(server.requests[10][1].endswith("/publictag"))
                for index,key in ((11,"privatetag"),(12,"invalidtag")):
                    self.assertIn("/"+key+"?tagging",server.requests[index][1]);self.assertEqual(server.requests[index][0],"GET")
                    self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])

    def test_missing_source_kms_and_identity_metadata_emit_no_requests(self):
        """Omit actual upstream KMS/user-ID prerequisites; expect explicit CaseNotRun before bucket creation and zero network requests, including partially specified identity metadata."""
        from osv.ceph_policy_conditions import encryption,grant_condition
        from osv.suites import CaseNotRun
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],[],"s3") as (worker,server):
                d=Path(temporary)
                for workload in ({},{"identities":{"main":{"canonical_id":"main-only"}}},{"identities":{"alt":{"canonical_id":"alt-only"}}}):
                    probe=Probe(Context(worker,"unused/",d,b"independent",d/"body",d/"output",workload))
                    with self.assertRaises(CaseNotRun):encryption(probe,"kms_noenc")
                    with self.assertRaises(CaseNotRun):grant_condition(probe)
                self.assertEqual(server.requests,[])

    def test_metadata_copy_condition_preserves_raw_directive_removal(self):
        """Feed real CopyObject and GetObject responses into the complete source scenario; expect COPY first, absent directive plus foo=bar metadata last, alternate signatures and both policies removed."""
        from osv.ceph_policy_conditions import copy_condition
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';copied=b'<CopyObjectResult><ETag>"independent-copy"</ETag><LastModified>2026-10-01T00:00:00Z</LastModified></CopyObjectResult>'
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(200,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},copied),(200,{},b"public/foo"),(403,{},denied),(204,{},b""),(204,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                copy_condition(Probe(ctx),True)
                self.assertEqual(len(server.requests),25)
                self.assertEqual(json.loads(server.requests[7][3])["Statement"][0]["Condition"],{"StringEquals":{"s3:x-amz-metadata-directive":"COPY"}})
                first={k.lower():v for k,v in server.requests[8][2].items()};last={k.lower():v for k,v in server.requests[10][2].items()}
                self.assertEqual(first["x-amz-metadata-directive"],"COPY");self.assertNotIn("x-amz-metadata-directive",last);self.assertEqual(last["x-amz-meta-foo"],"bar")
                self.assertIn("public/foo",first["x-amz-copy-source"]);self.assertIn("public/bar",last["x-amz-copy-source"])
                for index in (8,9,10):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                for index in (11,12):self.assertEqual(server.requests[index][0],"DELETE");self.assertIn("policy",server.requests[index][1])

    def test_grant_condition_compares_independent_ids_to_actual_acl_output(self):
        """Use real alternate writes and actual typed GetObjectAcl producers; expect grant header id=main, denied main ACL read and independently configured first-grant IDs."""
        from osv.ceph_policy_conditions import grant_condition
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        first=b'<AccessControlPolicy><Owner><ID>main-id</ID></Owner><AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>main-id</ID></Grantee><Permission>FULL_CONTROL</Permission></Grant></AccessControlList></AccessControlPolicy>'
        second=first.replace(b'main-id',b'alt-id')
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},b""),(200,{},b""),(200,{},first),(403,{},denied),(200,{},second),(204,{},b""),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"main-id"},"alt":{"canonical_id":"alt-id"}}})
                grant_condition(Probe(ctx))
                self.assertEqual(len(server.requests),23)
                self.assertEqual(json.loads(server.requests[4][3])["Statement"][0]["Condition"],{"StringEquals":{"s3:x-amz-grant-full-control":"id=main-id"}})
                first_headers={k.lower():v for k,v in server.requests[6][2].items()};second_headers={k.lower():v for k,v in server.requests[7][2].items()}
                self.assertEqual(first_headers["x-amz-grant-full-control"],"id=main-id");self.assertNotIn("x-amz-grant-full-control",second_headers)
                for index in (6,7,10):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                for index in (8,9):self.assertIn("Credential=local-test/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])

    def test_multipart_policy_keeps_bucket_object_arn_and_actual_upload_id(self):
        """Replay two distinct real service ClientError statuses then actual multipart ID; expect exact bucket-only/object ARNs, alt signing and abort using the SDK-produced ID."""
        from osv.ceph_policy_conditions import multipart
        denied=b'<Error><Code>AccessDenied</Code></Error>';missing=b'<Error><Code>NoSuchBucket</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';upload=b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>mpobj</Key><UploadId>independent-upload-9</UploadId></InitiateMultipartUploadResult>'
        fixtures=[(404,{},b""),(200,{},b""),(403,{},denied),(204,{},b""),(404,{},missing),(204,{},b""),(200,{},upload),(204,{},b""),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                multipart(Probe(ctx));self.assertEqual(len(server.requests),14)
                first=json.loads(server.requests[3][3])["Statement"][0];second=json.loads(server.requests[5][3])["Statement"][0]
                self.assertEqual(second["Resource"],first["Resource"]+"/mpobj");self.assertFalse(first["Resource"].endswith("/*"));self.assertEqual(first["Action"],"s3:PutObject")
                for index in (2,4,6,8):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                self.assertEqual(server.requests[8][0],"DELETE");self.assertIn("uploadId=independent-upload-9",server.requests[8][1])

    def test_head_prefix_policy_keeps_empty_bucket_and_distinct_http_errors(self):
        """Feed actual typed HEAD 404/403 under a bucket-only prefix policy; expect literal source keys, genuine alt identity, no initialization writes and policy-first cleanup."""
        from osv.ceph_policy_conditions import head_prefix
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(404,{},b""),(403,{},b""),(204,{},b""),(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                head_prefix(Probe(ctx));self.assertEqual(len(server.requests),10)
                item=json.loads(server.requests[2][3])["Statement"][0];self.assertEqual(item["Condition"],{"StringLike":{"s3:prefix":"public/*"}});self.assertFalse(item["Resource"].endswith("/*"))
                for index,key in ((3,"public/object"),(4,"private/object")):
                    self.assertEqual(server.requests[index][0],"HEAD");self.assertTrue(server.requests[index][1].endswith("/"+key));self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])
                self.assertEqual(server.requests[5][0],"DELETE");self.assertIn("policy",server.requests[5][1])

    def test_notprincipal_preserves_both_source_codes_and_rejects_other_codes(self):
        """Exercise real SDK HTTP 400 outcomes for both allowed source codes and an independent wrong code; expect only InvalidArgument/MalformedPolicy accepted and cleanup in every branch."""
        from osv.ceph_policy_conditions import notprincipal
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for target in self.targets:
            for code in ("InvalidArgument","MalformedPolicy","InvalidRequest"):
                rejected=("<Error><Code>"+code+"</Code></Error>").encode()
                fixtures=[(404,{},b""),(200,{},b""),(400,{},rejected),(204,{},b""),(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
                with self.subTest(language=target["language"],code=code),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    if code=="InvalidRequest":
                        with self.assertRaises(AssertionError):notprincipal(Probe(ctx))
                    else:notprincipal(Probe(ctx))
                    self.assertEqual(len(server.requests),8)
                    item=json.loads(server.requests[2][3])["Statement"][0];self.assertNotIn("Principal",item);self.assertEqual(item["NotPrincipal"],{"AWS":"arn:aws:iam::s3tenant1:root"});self.assertEqual(item["Effect"],"Allow")
                    self.assertEqual(server.requests[3][0],"DELETE");self.assertIn("policy",server.requests[3][1])

    def test_multipart_unexpected_creation_still_aborts_real_id_and_preserves_failure(self):
        """Replay success at each source creation stage; expect first/second unexpected success to fail the unchanged oracle, all actual IDs to reach alt Abort, and cleanup-only NoSuchUpload to be tolerated."""
        from osv.ceph_policy_conditions import multipart
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            for stage in (1,2,3):
                for already_aborted in (False,True):
                    ident="independent-stage-"+str(stage)+"-upload"
                    upload=("<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>mpobj</Key><UploadId>"+ident+"</UploadId></InitiateMultipartUploadResult>").encode()
                    abort=(404,{},b'<Error><Code>NoSuchUpload</Code></Error>') if already_aborted else (204,{},b"")
                    prefix=[(404,{},b""),(200,{},b"")]
                    if stage==1:replies=prefix+[(200,{},upload),abort];abort_index=3
                    elif stage==2:replies=prefix+[(403,{},denied),(204,{},b""),(200,{},upload),(204,{},b""),abort];abort_index=6
                    else:replies=prefix+[(403,{},denied),(204,{},b""),(403,{},denied),(204,{},b""),(200,{},upload),(204,{},b""),abort];abort_index=8
                    fixtures=replies+[(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
                    with self.subTest(language=target["language"],stage=stage,already_aborted=already_aborted),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                        d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                        if stage<3:
                            with self.assertRaisesRegex(AssertionError,"expected original service ClientError"):multipart(Probe(ctx))
                        else:multipart(Probe(ctx))
                        self.assertEqual(len(server.requests),len(fixtures))
                        abort_request=server.requests[abort_index]
                        self.assertEqual(abort_request[0],"DELETE");self.assertIn("uploadId="+ident,abort_request[1]);self.assertIn("/mpobj?",abort_request[1])
                        self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in abort_request[2].items()}["authorization"])
                        self.assertTrue(all("uploadId=" not in r[1] for r in server.requests[abort_index+1:]));self.assertEqual(server.requests[-1][0],"HEAD")

    def test_multipart_cleanup_does_not_swallow_unrelated_404(self):
        """Return NoSuchBucket for abort after genuine upload creation; expect explicit cleanup failure rather than treating arbitrary 404 as an already-aborted upload."""
        from osv.ceph_policy_conditions import multipart
        denied=b'<Error><Code>AccessDenied</Code></Error>';empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';upload=b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>mpobj</Key><UploadId>must-not-ignore</UploadId></InitiateMultipartUploadResult>'
        fixtures=[(404,{},b""),(200,{},b""),(403,{},denied),(204,{},b""),(403,{},denied),(204,{},b""),(200,{},upload),(204,{},b""),(404,{},b'<Error><Code>NoSuchBucket</Code></Error>'),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                with self.assertRaisesRegex(RuntimeError,"pending multipart upload cleanup failed"):multipart(Probe(ctx))
                self.assertEqual(len(server.requests),14);self.assertIn("uploadId=must-not-ignore",server.requests[8][1])
