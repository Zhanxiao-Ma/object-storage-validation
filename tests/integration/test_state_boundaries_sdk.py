"""Validate state-boundary source cases and five genuinely concurrent requests on the same SDK client."""
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker,FixtureHandler
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_state_boundaries import CASES,concurrent_version_delete

class ConcurrentDeleteHandler(FixtureHandler):
    """Require five real SDK HTTP delete requests to reach the server together, without implementing object storage or delete semantics."""
    def respond(self):
        query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
        deleting=self.command=="POST" and "delete" in query
        if deleting:
            with self.server.delete_lock:
                self.server.active_deletes+=1
                self.server.peak_deletes=max(self.server.peak_deletes,self.server.active_deletes)
            try:
                self.server.delete_barrier.wait(timeout=15)
                super().respond()
            finally:
                with self.server.delete_lock:self.server.active_deletes-=1
        else:super().respond()
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

class OrderedHeadHandler(FixtureHandler):
    """Return independent static HEAD documents in a controlled completion order to detect result reordering and cross-thread HTTP observation leaks."""
    def do_HEAD(self):
        name=urlsplit(self.path).path.rsplit("/",1)[-1]
        self.server.requests.append((self.command,self.path,dict(self.headers),b""))
        self.server.order_barrier.wait(timeout=15)
        if name=="delayed":self.server.fast_complete.wait(timeout=15)
        self.send_response(404 if name=="missing" else 200)
        self.send_header("Content-Length","0");self.send_header("x-amz-meta-order",name)
        self.end_headers()
        with self.server.order_lock:self.server.completions.append(name)
        if name=="fast":self.server.fast_complete.set()

@unittest.skipUnless(os.environ.get("OSV_RUN_STATE_BOUNDARY_TESTS")=="1","enable after building actual Java/C++ SDK workers")
class StateBoundariesSdkTest(unittest.TestCase):
    """Execute actual SDK source scenarios and independently inspect their produced requests and cleanup."""
    @classmethod
    def setUpClass(cls):
        """Reuse the local server and real SDK worker fixture; never run or subclass its test methods."""
        # Fixtures already provide explicit dummy credentials and OSV_REGION.
        # Prevent the SDK default configuration constructor from probing EC2
        # metadata before those explicit settings are applied.
        metadata=patch.dict(os.environ,{"AWS_EC2_METADATA_DISABLED":"true"})
        metadata.start();cls.addClassCleanup(metadata.stop)
        from test_s3_sdk import SdkIntegrationTest
        SdkIntegrationTest.setUpClass.__func__(cls)

    def test_original_state_cases_preserve_diagnostics_and_cleanup(self):
        """Execute 22 source contracts; expect missing identity metadata explicitly not_run, actual SDK/service differences retained and every owned bucket removed."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    results=run_cases(ctx,CASES)
                    self.assertEqual(len(results),22)
                    expected_fail={"ceph_state_condition_not_match","ceph_state_condition_not_modified","ceph_state_access_raw_get_object_acl","ceph_state_recreate_acl_overwrite","ceph_state_recreate_acl_new"}
                    expected_not_run={"ceph_state_version_acl_explicit","ceph_state_version_acl_current"}
                    self.assertEqual({row["case"] for row in results if row["status"]=="fail"},expected_fail,results)
                    self.assertEqual({row["case"] for row in results if row["status"]=="not_run"},expected_not_run,results)
                    self.assertEqual({row["case"] for row in results if row["status"]=="pass"},{item.name for item in CASES}-expected_fail-expected_not_run,results)
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_version_acl_uses_independent_identity_expectations(self):
        """Run both version ACL source functions with fixed independently configured identity metadata; expect original field/order differences surfaced and complete version cleanup."""
        selected=[case for case in CASES if case.name.startswith("ceph_state_version_acl_")]
        identities={"main":{"canonical_id":"75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a","display_name":"webfile"}}
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"identities":identities})
                    results=run_cases(ctx,selected)
                    self.assertEqual(len(results),2)
                    self.assertEqual({row["case"] for row in results if row["status"]=="fail"},{item.name for item in selected},results)
                    self.assertTrue(all("webfile" in row["error"] for row in results),results)
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_five_real_delete_requests_consume_actual_version_list(self):
        """Run the real concurrent deletion scenario; expect five simultaneous real SDK requests from the same worker without any fork, each carrying exactly the 15 service-produced key/version pairs and the original absence assertions."""
        pairs=[("key_"+str(key),"independent-k"+str(key)+"-v"+str(version)) for version in range(3) for key in range(5)]
        versions=("<ListVersionsResult><IsTruncated>false</IsTruncated>"+"".join("<Version><Key>"+key+"</Key><VersionId>"+version+"</VersionId></Version>" for key,version in pairs)+"</ListVersionsResult>").encode()
        deleted=("<DeleteResult>"+"".join("<Deleted><Key>"+key+"</Key><VersionId>"+version+"</VersionId></Deleted>" for key,version in pairs)+"</DeleteResult>").encode()
        enabled=b"<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>"
        empty_versions=b"<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>"
        empty_objects=b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{"Content-Type":"application/xml"},enabled)]
        fixtures += [(200,{},b"")]*15+[(200,{"Content-Type":"application/xml"},versions)]
        fixtures += [(200,{"Content-Type":"application/xml"},deleted)]*5
        fixtures += [(200,{"Content-Type":"application/xml"},empty_objects),(200,{"Content-Type":"application/xml"},empty_versions)]
        fixtures += [(200,{},b""),(200,{"Content-Type":"application/xml"},empty_versions),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",overrides={"OSV_CONNECTIONS":"5"},handler=ConcurrentDeleteHandler) as (worker,server):
                server.delete_lock=threading.Lock();server.active_deletes=0;server.peak_deletes=0;server.delete_barrier=threading.Barrier(5)
                directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                with patch.object(worker,"fork",side_effect=AssertionError("source requires the same SDK client, not forked workers")) as fork:
                    concurrent_version_delete(Probe(ctx))
                    fork.assert_not_called()
                self.assertEqual(server.peak_deletes,5)
                requests=[request for request in server.requests if request[0]=="POST"]
                self.assertEqual(len(requests),5)
                for request in requests:
                    document=ET.fromstring(request[3]);actual=[(obj.find("{*}Key").text,obj.find("{*}VersionId").text) for obj in document.findall("{*}Object")]
                    self.assertEqual(actual,pairs)
                    headers={k.lower():v for k,v in request[2].items()};self.assertTrue(headers["authorization"].startswith("AWS4-HMAC-SHA256 "))
                self.assertEqual(len(server.requests),len(fixtures));self.assertEqual(server.requests[-1][0],"HEAD")

    def test_shared_client_batch_retains_input_order_and_thread_local_headers(self):
        """Send real concurrent HEAD200/404/200 and force out-of-order completion; expect results in input order and each request's true SDK status/header/metadata without cross-thread leakage."""
        from osv.ceph_state_boundaries import header
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[],"s3",overrides={"OSV_CONNECTIONS":"5"},handler=OrderedHeadHandler) as (worker,server):
                server.order_barrier=threading.Barrier(3);server.fast_complete=threading.Event();server.order_lock=threading.Lock();server.completions=[]
                names=("delayed","missing","fast")
                response=worker.call({"op":"concurrent_requests","requests":[{"op":"head_http","key":name} for name in names]})
                self.assertTrue(response["ok"],response);results=response["results"]
                self.assertEqual(len(results),3)
                for name,result,status in zip(names,results,(200,404,200)):
                    self.assertEqual(result["status_code"],status,result);self.assertEqual(result["ok"],status==200,result)
                    self.assertEqual(header(result,"x-amz-meta-order"),name)
                self.assertEqual(len(server.requests),3)
                self.assertLess(server.completions.index("fast"),server.completions.index("delayed"))

    def test_shared_client_batch_rejects_forbidden_inputs_before_any_http(self):
        """Reject malformed/bounds/nested or identity/header/checksum-changing batches before executing even an earlier valid item; expect no HTTP, then genuine one-item/64-item batches succeed and an unknown ordinary operation remains an isolated item failure."""
        valid={"op":"head_http","key":"obj"}
        invalid=[None,{},[],[valid]*65,["not-an-object"],[{}],[valid,{}],[valid,"not-an-object"],[valid,{"op":None}],[valid,{"op":3}],[valid,{"op":"info"}],[{"op":"concurrent_requests","requests":[valid]}]]
        invalid += [[valid,{**valid,name:value}] for name,value in (("identity","main"),("wire_headers",{}),("checksum_when_required",True))]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b"")]*67,"s3",overrides={"OSV_CONNECTIONS":"5"}) as (worker,server):
                for requests in invalid:
                    result=worker.call({"op":"concurrent_requests","requests":requests})
                    self.assertFalse(result["ok"],result)
                self.assertEqual(server.requests,[])
                for size in (1,64):
                    result=worker.call({"op":"concurrent_requests","requests":[valid]*size})
                    self.assertTrue(result["ok"],result);self.assertEqual(len(result["results"]),size)
                    self.assertTrue(all(row["ok"] and row["status_code"]==200 for row in result["results"]),result)
                self.assertEqual(len(server.requests),65)
                # A structurally valid ordinary request may fail independently;
                # the batch is not a transaction that rolls back its peers.
                result=worker.call({"op":"concurrent_requests","requests":[valid,{"op":"unknown_sdk_operation"},valid]})
                self.assertTrue(result["ok"],result)
                self.assertEqual([row["ok"] for row in result["results"]],[True,False,True])
                self.assertTrue(result["results"][1]["error"])
                self.assertEqual(len(server.requests),67)

    def test_multipart_tagging_uses_typed_header_and_rejects_non_text_before_io(self):
        """Create with literal typed Tagging and consume the real upload ID for abort; expect exact x-amz-tagging/signature plus no HTTP for null/numeric/boolean/array/object tagging values."""
        payload=b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>obj</Key><UploadId>actual-tagged-upload</UploadId></InitiateMultipartUploadResult>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},payload),(204,{},b"")],"s3") as (worker,server):
                response=worker.call({"op":"mp_create_metadata","key":"obj","tagging":"Hello=World&foo=bar"})
                self.assertTrue(response["ok"],response);self.assertEqual(response["upload_id"],"actual-tagged-upload")
                for value in (None,23,True,[],{}):
                    rejected=worker.call({"op":"mp_create_metadata","key":"obj","tagging":value})
                    self.assertFalse(rejected["ok"],rejected)
                self.assertEqual(len(server.requests),1)
                headers={k.lower():v for k,v in server.requests[0][2].items()}
                self.assertEqual(headers["x-amz-tagging"],"Hello=World&foo=bar")
                self.assertIn("x-amz-tagging",headers["authorization"])
                aborted=worker.call({"op":"mp_abort_raw","key":"obj","upload_id":response["upload_id"]})
                self.assertTrue(aborted["ok"],aborted)
                self.assertEqual(len(server.requests),2)
                self.assertEqual(parse_qs(urlsplit(server.requests[1][1]).query)["uploadId"],["actual-tagged-upload"])
