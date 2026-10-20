"""Validate Ceph public-access and ownership translations through actual Java/C++ SDKs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_public_controls import CASES,public_block,policy_status,ownership_actions,OwnershipScope
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_PUBLIC_CONTROL_TESTS")=="1","enable after building actual AWS SDK workers")
class PublicControlsSdkTest(unittest.TestCase):
    """Observe real signed HTTP requests, native model decoding, preserved source oracles and owned-resource cleanup."""
    @classmethod
    def setUpClass(cls):
        """Reuse isolated Moto setup; expect actual SDK binaries, explicit local credentials and no real cloud traffic."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_source_public_acl_denials_use_exact_configuration_and_headers(self):
        """Replay independent 403 responses for all source bucket/object canned ACLs; expect exact boolean configurations and original empty foo1..foo4 uploads, including private success."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';denied=b'<Error><Code>AccessDenied</Code></Error>'
        conf=b'<PublicAccessBlockConfiguration><BlockPublicAcls>true</BlockPublicAcls><IgnorePublicAcls>false</IgnorePublicAcls><BlockPublicPolicy>true</BlockPublicPolicy><RestrictPublicBuckets>false</RestrictPublicBuckets></PublicAccessBlockConfiguration>'
        for target in self.targets:
            for mode in ("bucket_acl","object_acl"):
                replies=[(404,{},b""),(200,{},b""),(200,{},b"")]
                if mode=="bucket_acl":replies+=[(200,{},conf)]+[(403,{},denied)]*3
                else:replies+=[(403,{},denied)]*3+[(200,{},b"")]
                fixtures=replies+[(200,{},b"")]+([(204,{},b"")]*4 if mode=="object_acl" else [])+[(200,{},empty),(204,{},b""),(404,{},b"")]
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    public_block(Probe(ctx),mode);self.assertEqual(len(server.requests),len(fixtures))
                    import xml.etree.ElementTree as ET
                    configuration={node.tag.rsplit("}",1)[-1]:node.text for node in ET.fromstring(server.requests[2][3])}
                    self.assertEqual(configuration,{"BlockPublicAcls":"true","IgnorePublicAcls":"false","BlockPublicPolicy":"true" if mode=="bucket_acl" else "false","RestrictPublicBuckets":"false"})
                    indices=(4,5,6) if mode=="bucket_acl" else (3,4,5,6)
                    expected=("public-read","public-read-write","authenticated-read")+(("private",) if mode=="object_acl" else ())
                    for index,acl in zip(indices,expected):
                        headers={k.lower():v for k,v in server.requests[index][2].items()}
                        if mode=="bucket_acl":self.assertEqual(headers["x-amz-acl"],acl)
                        else:self.assertEqual(headers["x-amz-acl"],acl);self.assertEqual(server.requests[index][3],b"");self.assertTrue(server.requests[index][1].endswith("/foo"+str(index-2)))

    def test_creation_time_ownership_header_is_limited_to_create_request(self):
        """Use real scoped creation and cleanup for all ownership modes; expect exact signed x-amz-object-ownership only on CreateBucket and no inherited header on later requests."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        for target in self.targets:
            for mode in ("BucketOwnerEnforced","BucketOwnerPreferred","ObjectWriter"):
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    with OwnershipScope(Probe(ctx),mode):pass
                    self.assertEqual(len(server.requests),6)
                    for index,request in enumerate(server.requests):
                        headers={k.lower():v for k,v in request[2].items()}
                        if index==1:self.assertEqual(headers["x-amz-object-ownership"],mode);self.assertIn("x-amz-object-ownership",headers["authorization"])
                        else:self.assertNotIn("x-amz-object-ownership",headers)

    def test_public_block_group_keeps_original_moto_diagnostics_and_cleanup(self):
        """Run all six new public block cases with real alternate/anonymous clients; expect preserved local service failures, only explicit-principal policy Java success and no bucket remnants."""
        selected=tuple(c for c in CASES if c.name.startswith("ceph_public_controls_"))
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    d=Path(temporary);worker=Worker(target,30)
                    try:
                        rows=run_cases(Context(worker,"unused/",d,b"independent",d/"body",d/"output",{}),selected)
                        passed={"ceph_public_controls_principal"} if target["language"]=="java" else set()
                        self.assertEqual({r["case"] for r in rows if r["status"]=="pass"},passed)
                        self.assertEqual({r["case"] for r in rows if r["status"]=="fail"},{c.name for c in selected}-passed)
                        self.assertEqual(len(rows),6)
                        self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_all_ownership_helpers_use_real_acl_parts_outputs_and_cleanup(self):
        """Replay independent ownership truth tables through all six full source cases; expect genuine PUT/multipart/COPY owners, original ACL errors, exact creation/setup requests and all SDK-produced upload IDs aborted."""
        from osv.ceph_public_controls import ownership
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        acl_error=b'<Error><Code>AccessControlListNotSupported</Code></Error>'
        copied=b'<CopyObjectResult><ETag>"independent-copy"</ETag><LastModified>2026-10-01T00:00:00Z</LastModified></CopyObjectResult>'
        # Independent pinned-source ownership table, not inferred from SDK output.
        vectors=(("BucketOwnerEnforced",("main-id","main-id",None)),("BucketOwnerPreferred",("alt-id","main-id","alt-id")),("ObjectWriter",("alt-id","alt-id","alt-id")))
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        def owner_xml(root,ident):
            name="main-name" if ident=="main-id" else "alt-name"
            return ("<"+root+"><Owner><ID>"+ident+"</ID><DisplayName>"+name+"</DisplayName></Owner></"+root+">").encode()
        for target in self.targets:
            for mode,owners in vectors:
                for creation in (True,False):
                    ownership_xml=("<OwnershipControls><Rule><ObjectOwnership>"+mode+"</ObjectOwnership></Rule></OwnershipControls>").encode()
                    setup=[(404,{},b""),(200,{},b"")]
                    if not creation:
                        if mode=="BucketOwnerEnforced":setup+=[(400,{},b'<Error><Code>InvalidBucketAclWithObjectOwnership</Code></Error>'),(200,{},b"")]
                        setup+=[(200,{},b"")]
                    setup+=[(200,{},ownership_xml),(204,{},b"")]
                    replies=[];upload_ids=[];operation_indices=[]
                    for kind in ("put-object","create-multipart-upload","copy-object"):
                        for suffix,acl,ident in zip(("no-acl","bucket-owner-full-control","private"),(None,"bucket-owner-full-control","private"),owners):
                            key=kind+"-"+suffix;operation_indices.append((len(setup)+len(replies),kind,key,acl))
                            if ident is None:replies+=[(400,{},acl_error)];continue
                            if kind=="create-multipart-upload":
                                upload="independent-"+suffix;upload_ids.append(upload)
                                body=("<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>"+key+"</Key><UploadId>"+upload+"</UploadId></InitiateMultipartUploadResult>").encode()
                                replies+=[(200,{},body),(200,{},owner_xml("ListPartsResult",ident))]
                            else:replies+=[(200,{},copied if kind=="copy-object" else b""),(200,{},owner_xml("AccessControlPolicy",ident))]
                    replies+=([(400,{},acl_error)]*2 if mode=="BucketOwnerEnforced" else [(200,{},b"")]*2)+[(204,{},b"")]*len(upload_ids)
                    fixtures=setup+replies+[(204,{},b""),(200,{},b"")]+[(204,{},b"")]*9+[(200,{},empty),(204,{},b""),(404,{},b"")]
                    with self.subTest(language=target["language"],mode=mode,creation=creation),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                        d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"main-id","display_name":"main-name"}}})
                        ownership(Probe(ctx),mode,creation);self.assertEqual(len(server.requests),len(fixtures))
                        created={k.lower():v for k,v in server.requests[1][2].items()}
                        if creation:self.assertEqual(created["x-amz-object-ownership"],mode)
                        elif mode=="BucketOwnerEnforced":self.assertEqual(created["x-amz-acl"],"public-read")
                        for index,kind,key,acl in operation_indices:
                            request=server.requests[index];headers={k.lower():v for k,v in request[2].items()}
                            self.assertIn("/"+key,request[1]);self.assertEqual(headers.get("x-amz-acl"),acl);self.assertIn("Credential=alternate-key/",headers["authorization"])
                            if kind=="put-object":self.assertEqual(request[3],b"")
                            elif kind=="copy-object":self.assertTrue(headers["x-amz-copy-source"].endswith("put-object-no-acl"))
                            elif owners[("no-acl","bucket-owner-full-control","private").index(key[len(kind)+1:])] is not None:
                                parts=server.requests[index+1];self.assertEqual(parts[0],"GET");self.assertIn("uploadId=independent-"+key[len(kind)+1:],parts[1])
                        aborted=[r for r in server.requests if r[0]=="DELETE" and "uploadId=" in r[1]]
                        self.assertEqual(len(aborted),len(upload_ids))
                        for request,ident in zip(aborted,upload_ids):self.assertIn("uploadId="+ident,request[1]);self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in request[2].items()}["authorization"])

    def test_policy_status_original_boolean_and_policy_inputs(self):
        """Replay independent SDK PolicyStatus XML for all six original status cases; expect source ACL-derived booleans, exact source-IP/principal inputs and policy cleanup."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';private=b'<PolicyStatus><IsPublic>false</IsPublic></PolicyStatus>';public=b'<PolicyStatus><IsPublic>true</IsPublic></PolicyStatus>'
        for target in self.targets:
            for mode in ("empty","public_acl","authenticated_acl","public_policy","source_ip","principal"):
                prefix=[(404,{},b""),(200,{},b"")]
                if mode=="empty":responses=[(200,{},private)]
                elif mode in ("public_acl","authenticated_acl"):responses=[(200,{},b""),(200,{},public)]
                elif mode=="principal":responses=[(204,{},b""),(200,{},private),(204,{},b"")]
                else:responses=[(200,{},private),(204,{},b""),(200,{},public if mode=="public_policy" else private),(204,{},b"")]
                fixtures=prefix+responses+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    policy_status(Probe(ctx),mode);self.assertEqual(len(server.requests),len(fixtures))
                    if mode in ("public_acl","authenticated_acl"):
                        headers={k.lower():v for k,v in server.requests[2][2].items()};self.assertEqual(headers["x-amz-acl"],"public-read" if mode=="public_acl" else "authenticated-read")
                    policies=[json.loads(r[3]) for r in server.requests if r[0]=="PUT" and "?policy" in r[1]]
                    if mode in ("public_policy","source_ip","principal"):
                        self.assertEqual(len(policies),1);item=policies[0]["Statement"][0];self.assertEqual(item["Action"],"s3:ListBucket")
                        self.assertEqual(item["Principal"],{"AWS":"arn:aws:iam::s3tenant1:root"} if mode=="principal" else {"AWS":"*"})
                        self.assertEqual(item.get("Condition"),{"IpAddress":{"aws:SourceIp":"10.0.0.0/32"}} if mode=="source_ip" else None)
                        self.assertEqual(len([r for r in server.requests if r[0]=="DELETE" and "?policy" in r[1]]),1)
                    else:self.assertEqual(policies,[])

    def test_restrict_and_ignore_public_access_preserve_actual_identities(self):
        """Replay public access before/after both source restrictions; expect unsigned anonymous calls, exact alternate keys, original foo/bar and key1/abcde bodies, and owner reads after anonymous denial."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';denied=b'<Error><Code>AccessDenied</Code></Error>'
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            for mode in ("restrict","ignore_acl"):
                if mode=="restrict":fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},b"bar"),(200,{},b""),(403,{},denied),(200,{},b"bar"),(204,{},b""),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
                else:fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{},empty),(200,{},b""),(200,{},b"abcde"),(200,{},b""),(200,{},b""),(403,{},denied),(403,{},denied),(200,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
                with self.subTest(language=target["language"],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                    d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                    public_block(Probe(ctx),mode);self.assertEqual(len(server.requests),15)
                    import xml.etree.ElementTree as ET
                    configuration={node.tag.rsplit("}",1)[-1]:node.text for node in ET.fromstring(server.requests[6][3])}
                    self.assertEqual(configuration,{"BlockPublicAcls":"false","IgnorePublicAcls":"true" if mode=="ignore_acl" else "false","BlockPublicPolicy":"false","RestrictPublicBuckets":"true" if mode=="restrict" else "false"})
                    if mode=="restrict":
                        self.assertEqual(server.requests[3][3],b"bar")
                        for index in (5,7):self.assertNotIn("authorization",{k.lower():v for k,v in server.requests[index][2].items()})
                        self.assertIn("Credential=local-test/",{k.lower():v for k,v in server.requests[8][2].items()}["authorization"])
                        self.assertEqual(server.requests[9][0],"DELETE");self.assertIn("?policy",server.requests[9][1])
                    else:
                        self.assertEqual(server.requests[4][3],b"abcde")
                        for index in (3,5,8,9):self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in server.requests[index][2].items()}["authorization"])

    def test_all_source_controls_keep_real_moto_diagnostics_and_prerequisites(self):
        """Run all 18 source cases with alternate credentials but no expected main identity metadata; retain raw SDK/service failures and six explicit ownership prerequisites without bucket leftovers."""
        with patch.dict(os.environ,{"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}):
            for target in self.targets:
                with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                    d=Path(temporary);worker=Worker(target,30)
                    try:
                        rows=run_cases(Context(worker,"unused/",d,b"independent",d/"body",d/"output",{}),CASES)
                        not_run={c.name for c in CASES if c.name.startswith("ceph_public_ownership_")}
                        passed={"ceph_public_controls_principal"} if target["language"]=="java" else set()
                        self.assertEqual({r["case"] for r in rows if r["status"]=="pass"},passed)
                        self.assertEqual({r["case"] for r in rows if r["status"]=="fail"},{c.name for c in CASES}-not_run-passed)
                        self.assertTrue(all(r["reason"]=="missing_prerequisites" for r in rows if r["status"]=="not_run"))
                        self.assertEqual(len(rows),18)
                        self.assertEqual({r["case"] for r in rows if r["status"]=="not_run"},{c.name for c in CASES if c.name.startswith("ceph_public_ownership_")})
                        self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                    finally:worker.close()

    def test_unexpected_forbidden_multipart_success_aborts_every_observed_id(self):
        """Let enforced private multipart creation unexpectedly succeed after two valid uploads; expect unchanged 400 oracle failure and three actual returned IDs aborted through the alternate SDK before owned-bucket deletion."""
        from osv.bucket_scope import BucketScope
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';acl_error=b'<Error><Code>AccessControlListNotSupported</Code></Error>'
        owner=b'<AccessControlPolicy><Owner><ID>main-id</ID><DisplayName>main-name</DisplayName></Owner></AccessControlPolicy>'
        parts=b'<ListPartsResult><Owner><ID>main-id</ID><DisplayName>main-name</DisplayName></Owner></ListPartsResult>'
        replies=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{},owner),(200,{},b""),(200,{},owner),(400,{},acl_error)]
        ids=("first-real-upload","second-real-upload","unexpected-private-upload")
        for index,ident in enumerate(ids):
            result=("<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>key</Key><UploadId>"+ident+"</UploadId></InitiateMultipartUploadResult>").encode()
            replies+=[(200,{},result)]
            if index<2:replies+=[(200,{},parts)]
        fixtures=replies+[(204,{},b"")]*3+[(200,{},b"")]+[(204,{},b"")]*6+[(200,{},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3",overrides) as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{})
                with self.assertRaisesRegex(AssertionError,"expected service rejection"):
                    with BucketScope(Probe(ctx)) as bucket:ownership_actions(Probe(ctx),bucket,"BucketOwnerEnforced",("main-id","main-name"))
                self.assertEqual(len(server.requests),len(fixtures))
                aborted=[r for r in server.requests if r[0]=="DELETE" and "uploadId=" in r[1]];self.assertEqual(len(aborted),3)
                for request,ident in zip(aborted,ids):self.assertIn("uploadId="+ident,request[1]);self.assertIn("Credential=alternate-key/",{k.lower():v for k,v in request[2].items()}["authorization"])
