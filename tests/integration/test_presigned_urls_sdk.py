"""Verify official SDK URL producers feed actual HTTP consumers without a replacement signer."""
import os
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,unquote
from sdk_fixtures import fixture_worker,FixtureHandler
from test_lifecycle_expiration_sdk import scope_begin,scope_end,XML
from osv.ceph_presigned_urls import CASES,vector
from osv.scenarios import Context
from osv.suites import Probe,run_cases

class OptionsHandler(FixtureHandler):
    """Replay independently supplied OPTIONS documents as well as ordinary SDK HTTP requests."""
    do_OPTIONS=FixtureHandler.respond

def context(worker,temporary):
    path=Path(temporary)
    return Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"}})

def languages():return os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")

@unittest.skipUnless(os.environ.get("OSV_RUN_PRESIGNED_URL_TESTS")=="1","enable actual SDK presign integration")
class PresignedUrlSdkTest(unittest.TestCase):
    """Pin raw source inputs and independent protocol expectations; never regenerate signatures in tests."""
    def test_official_presigned_urls_are_consumed_with_real_main_and_tenant_credentials(self):
        """Sign GET/PUT using distinct actual main/tenant SDK contexts and feed each exact URL to requests; expect credential isolation, original 100000 expiry and ACL/body on the resulting HTTP request."""
        import requests
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[(200,{},b"source body"),(200,{},b"")],"s3",handler=OptionsHandler,overrides={"OSV_TENANT_ACCESS_KEY":"independent-tenant-access","OSV_TENANT_SECRET_KEY":"independent-tenant-secret"}) as (worker,server):
                urls=[]
                for identity,method in (("main","GET"),("tenant","PUT")):
                    before=len(server.requests)
                    result=worker.call({"op":"presign","identity":identity,"bucket":"original-bucket","key":"space +/%key","method":method,"expires_seconds":100000,**({"acl":"private"} if method=="PUT" else {})})
                    self.assertTrue(result["ok"],result);self.assertEqual(len(server.requests),before)
                    url=result["url"];urls.append(url);parts=urlsplit(url);query=parse_qs(parts.query)
                    self.assertEqual(unquote(parts.path),"/original-bucket/space +/%key")
                    self.assertEqual(query["X-Amz-Algorithm"],["AWS4-HMAC-SHA256"])
                    self.assertEqual(query["X-Amz-Expires"],["100000"])
                    self.assertTrue(query["X-Amz-Credential"][0].startswith(("local-test" if identity=="main" else "independent-tenant-access")+"/"))
                    self.assertEqual(len(query["X-Amz-Signature"][0]),64)
                    if method=="PUT":self.assertTrue("x-amz-acl" in query.get("X-Amz-SignedHeaders",[""])[0].split(";") or query.get("x-amz-acl")==["private"],query)
                    response=requests.request(method,url,timeout=10,**({"data":b"hello world","headers":{"x-amz-acl":"private"}} if method=="PUT" else {}))
                    self.assertEqual(response.status_code,200)
                self.assertEqual(len(server.requests),2)
                for row,url in zip(server.requests,urls):self.assertEqual(row[1],urlsplit(url).path+"?"+urlsplit(url).query)
                self.assertEqual(server.requests[1][3],b"hello world");self.assertEqual(server.requests[1][2]["x-amz-acl"],"private")

    def test_exact_expiry_inputs_preserve_local_sdk_rejections_and_omission(self):
        """Pass omitted, zero, negative and over-limit source durations to the real SDK; expect no HTTP during signing, omission metadata preserved, and local failures to have no invented HTTP status."""
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for expiry in (None,100000,0,609901,-7,-1000):
                    request={"op":"presign","bucket":"source-bucket","key":"foo","method":"GET"}
                    if expiry is not None:request["expires_seconds"]=expiry
                    result=worker.call(request)
                    if result["ok"]:
                        self.assertEqual(result["expires_input_present"],expiry is not None)
                        actual=int(parse_qs(urlsplit(result["url"]).query)["X-Amz-Expires"][0])
                        self.assertEqual(actual,(3600 if language=="java" else 604800) if expiry is None else expiry)
                    else:
                        self.assertNotIn("status_code",result)
                        self.assertIn(expiry,(0,609901,-7,-1000))
                    self.assertEqual(server.requests,[])

    def test_original_out_of_range_sources_keep_server_403_distinct_from_local_errors(self):
        """Execute original zero/over-max/negative/expired-PUT inputs without rewriting them; expect literal server403 only when official SDK produces a URL, otherwise source scenario fails locally and still cleans its owned bucket."""
        for language in languages():
            for mode,expiry in (("zero",0),("above_max",609901),("negative",-7),("expired_put",-1000)):
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",handler=OptionsHandler) as (worker,server):
                    probe=worker.call({"op":"presign","bucket":"expiry-probe","key":"foo","method":"PUT" if mode=="expired_put" else "GET","expires_seconds":expiry})
                    self.assertEqual(server.requests,[])
                    documents=scope_begin()+[(200,{},b"")]+([(403,{},b"independent source expiry rejection")] if probe["ok"] else [])+scope_end(("foo",))
                    server.fixtures.extend(documents)
                    if probe["ok"]:vector(Probe(context(worker,temporary)),mode)
                    else:
                        self.assertNotIn("status_code",probe)
                        with self.assertRaises(RuntimeError):vector(Probe(context(worker,temporary)),mode)
                    self.assertEqual(len(server.requests),len(documents));self.assertEqual(len(server.fixtures),0)
                    signed=[row for row in server.requests if "X-Amz-Signature=" in row[1]]
                    self.assertEqual(len(signed),1 if probe["ok"] else 0)
                    if signed:self.assertEqual(parse_qs(urlsplit(signed[0][1]).query)["X-Amz-Expires"],[str(expiry)])

    def test_invalid_bridge_inputs_never_send_http_or_fall_back_identity(self):
        """Give malformed method/expiry/ACL/signature/identity inputs to the real bridge; expect local errors and zero HTTP, especially no tenant-to-main or V2-to-V4 fallback."""
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for change in ({"method":"OPTIONS"},{"expires_seconds":True},{"expires_seconds":1.5},{"expires_seconds":"100000"},{"expires_seconds":None},{"acl":"private"},{"signature_version":"v2"},{"identity":"missing"},{"identity":None}):
                    result=worker.call({"op":"presign","bucket":"source-bucket","key":"foo","method":"GET",**change})
                    self.assertFalse(result["ok"],result);self.assertNotIn("status_code",result);self.assertEqual(server.requests,[])

    def test_original_successful_expiry_acl_and_cors_vectors(self):
        """Execute original valid GET, signed ACL PUT/readback and all CORS method/ACL variants using official produced URLs; expect exact OPTIONS/GET/PUT status, body and both CORS headers plus owner-scoped cleanup."""
        for language in languages():
            for mode in ("not_expired","put_acl","cors_get","cors_put","cors_put_acl"):
                for identity in ("main","tenant"):
                    documents=scope_begin()+([] if mode=="put_acl" else [(200,{},b"")])
                    if mode=="not_expired":documents += [(400,{},b""),(200,{},b"")]
                    elif mode=="put_acl":documents += [(200,{},b""),(200,{},b"hello world")]
                    else:documents += [(400,{},b""),(200,{},b""),(200,{"access-control-allow-origin":"example","access-control-allow-methods":"GET" if mode=="cors_get" else "PUT"},b"")]
                    documents += scope_end(("foo",))
                    with self.subTest(language=language,mode=mode,identity=identity),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3",handler=OptionsHandler,overrides={"OSV_TENANT_ACCESS_KEY":"tenant-original","OSV_TENANT_SECRET_KEY":"tenant-secret"}) as (worker,server):
                        vector(Probe(context(worker,temporary)),mode,identity)
                        self.assertEqual(len(server.requests),len(documents));self.assertEqual(len(server.fixtures),0)
                        signed=[row for row in server.requests if "X-Amz-Signature=" in row[1]]
                        self.assertEqual(len(signed),2)
                        for row in signed:
                            query=parse_qs(urlsplit(row[1]).query)
                            self.assertTrue(query["X-Amz-Credential"][0].startswith(("local-test" if identity=="main" else "tenant-original")+"/"))
                            self.assertEqual(query["X-Amz-Expires"],["100000"] if mode!="put_acl" else ["3600" if language=="java" else "604800"])
                        if mode.startswith("cors_"):
                            headers={key.lower():value for key,value in signed[-1][2].items()}
                            self.assertEqual(headers["origin"],"example");self.assertEqual(headers["access-control-request-method"],"GET" if mode=="cors_get" else "PUT")
                        if mode=="put_acl":self.assertEqual(signed[0][3],b"hello world");self.assertEqual(signed[0][2]["x-amz-acl"],"private")

    def test_v2_requires_genuine_sdk_capability_and_missing_http_config_sends_nothing(self):
        """Run all V2 sources and omit explicit raw transport config; expect precise not_run before bucket setup, with no V4 substitution or HTTP side effects."""
        for language in languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                ctx=context(worker,temporary);results=run_cases(ctx,[case for case in CASES if case.name.endswith("_v2")])
                self.assertEqual([row["status"] for row in results],["not_run"]*4)
                ctx.workload.clear();results=run_cases(ctx,[CASES[0]])
                self.assertEqual(results[0]["status"],"not_run");self.assertEqual(server.requests,[])

    def test_real_sdk_session_token_query_is_redacted_in_diagnostics(self):
        """Generate actual SDK URLs with a slash/plus session token; expect the original token in real query decoding but complete removal of its encoded form from final diagnostics."""
        token="temporary+token/value="
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3",{"OSV_SESSION_TOKEN":token}) as (worker,server):
                result=worker.call({"op":"presign","method":"GET","bucket":"unused","key":"foo","expires_seconds":10000,"signature_version":"v4"})
                self.assertTrue(result["ok"],result)
                query=parse_qs(urlsplit(result["url"]).query)
                self.assertEqual(query["X-Amz-Security-Token"],[token])
                diagnostic=worker.secret_registry.redact(json.dumps(result))
                self.assertNotIn(token,diagnostic);self.assertNotIn("temporary%2Btoken%2Fvalue%3D",diagnostic)
                self.assertEqual(server.requests,[])
