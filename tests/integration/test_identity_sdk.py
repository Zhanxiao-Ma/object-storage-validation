"""Prove actual SDK requests use distinct credential providers and genuine anonymous transport."""
import os
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_IDENTITY_TESTS")=="1","enable after building both actual AWS SDK workers")
class IdentitySdkTest(unittest.TestCase):
    """Observe signed requests and errors without simulating identity selection or authorization policy evaluation."""
    def test_named_anonymous_and_main_clients_do_not_share_credentials(self):
        """Alternate real SDK clients and an anonymous call, then return to main; expect exact access-key/session-token identities and no inherited anonymous Authorization."""
        identities=("main","alt","tenant","bad_auth","anonymous","alt","main")
        overrides={"OSV_SESSION_TOKEN":"primary-token","OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret","OSV_ALT_SESSION_TOKEN":"alternate-token",
                   "OSV_TENANT_ACCESS_KEY":"tenant-key","OSV_TENANT_SECRET_KEY":"tenant-secret","OSV_TENANT_SESSION_TOKEN":"tenant-token",
                   "OSV_BAD_AUTH_ACCESS_KEY":"invalid-key","OSV_BAD_AUTH_SECRET_KEY":"invalid-secret","OSV_BAD_AUTH_SESSION_TOKEN":""}
        expected={"main":("local-test","primary-token"),"alt":("alternate-key","alternate-token"),"tenant":("tenant-key","tenant-token"),"bad_auth":("invalid-key",None)}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{"Content-Length":"7"},b"")]*len(identities),"s3",overrides) as (worker,server):
                self.assertTrue({"identity_main","identity_alt","identity_tenant","identity_bad_auth","identity_anonymous"}<=set(worker.info["capabilities"]))
                for identity in identities:
                    result=worker.call({"op":"head_http","key":"object","identity":identity})
                    self.assertTrue(result["ok"],result);self.assertEqual(result["size"],7)
                self.assertEqual(len(server.requests),len(identities))
                for identity,request in zip(identities,server.requests):
                    headers={key.lower():value for key,value in request[2].items()}
                    if identity=="anonymous":
                        self.assertNotIn("authorization",headers);self.assertNotIn("x-amz-security-token",headers)
                    else:
                        access_key,token=expected[identity]
                        self.assertIn("Credential="+access_key+"/",headers["authorization"])
                        self.assertEqual(headers.get("x-amz-security-token"),token)
                self.assertNotIn("alternate-secret",str(worker.info));self.assertNotIn("tenant-secret",str(worker.info))

    def test_missing_or_invalid_identity_never_falls_back_to_main(self):
        """Request absent alternate credentials and invalid names/types; expect errors before HTTP, no alternate capabilities and a subsequent valid main request."""
        overrides={"OSV_ALT_ACCESS_KEY":"","OSV_ALT_SECRET_KEY":"","OSV_TENANT_ACCESS_KEY":"","OSV_TENANT_SECRET_KEY":"","OSV_BAD_AUTH_ACCESS_KEY":"","OSV_BAD_AUTH_SECRET_KEY":""}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"3"},b"")],"s3",overrides) as (worker,server):
                self.assertFalse({"identity_alt","identity_tenant","identity_bad_auth"}&set(worker.info["capabilities"]))
                for identity in ("alt","tenant","bad_auth","unknown","",None,3,False):
                    result=worker.call({"op":"head_http","key":"object","identity":identity})
                    self.assertFalse(result["ok"],result);self.assertNotIn("status_code",result)
                self.assertEqual(server.requests,[])
                result=worker.call({"op":"head_http","key":"object"})
                self.assertTrue(result["ok"],result);self.assertEqual(len(server.requests),1)
                self.assertIn("Credential=local-test/",{key.lower():value for key,value in server.requests[0][2].items()}["authorization"])

    def test_identity_selection_reaches_generic_model_operations(self):
        """Call a real generated GetBucketCors through alternate then anonymous clients; expect selected signer contexts and independent decoded SDK response rules."""
        response=b'<CORSConfiguration><CORSRule><AllowedMethod>GET</AllowedMethod><AllowedOrigin>https://service.example</AllowedOrigin></CORSRule></CORSConfiguration>'
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},response)]*2,"s3",overrides) as (worker,server):
                for identity in ("alt","anonymous"):
                    result=worker.call({"op":"api.GetBucketCors","input":{"Bucket":"fixture"},"identity":identity})
                    self.assertTrue(result["ok"],result);self.assertEqual(result["data"]["CORSRules"][0]["AllowedOrigins"],["https://service.example"])
                first={key.lower():value for key,value in server.requests[0][2].items()};second={key.lower():value for key,value in server.requests[1][2].items()}
                self.assertIn("Credential=alternate-key/",first["authorization"]);self.assertNotIn("authorization",second)

    def test_source_cross_bucket_policy_uses_real_get_output_and_alternate_signer(self):
        """Run the actual Ceph cross-bucket policy translation with independent policy bytes; expect GET output to reach the second PUT unchanged and both lists to use alternate credentials."""
        from osv.ceph_bucket_policy import policy
        from osv.scenarios import Context
        from osv.suites import Probe
        observed_policy=' {"Version":"2012-10-17", "Statement":[], "Id":"independent-service-only"}\n'
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        def listed(key):return ('<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>'+key+'</Key><Size>4</Size></Contents></ListBucketResult>').encode()
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(204,{},b""),
                  (404,{},b""),(200,{},b""),(200,{},b""),(200,{"Content-Type":"application/json"},observed_policy.encode()),(204,{},b""),
                  (200,{"Content-Type":"application/xml"},listed("asdf")),(200,{"Content-Type":"application/xml"},listed("abcd")),
                  (200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b""),
                  (200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for version in (1,2):
                with self.subTest(language=language,version=version),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",overrides) as (worker,server):
                    directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    evidence=policy(Probe(ctx),version,True)
                    self.assertEqual(len(evidence["isolated_buckets"]),2);self.assertEqual(len(server.requests),21)
                    self.assertEqual(server.requests[8][3],observed_policy.encode())
                    self.assertNotEqual(server.requests[3][3],server.requests[8][3])
                    self.assertEqual(server.requests[2][3],b"asdf");self.assertEqual(server.requests[6][3],b"abcd")
                    for index in (9,10):
                        headers={key.lower():value for key,value in server.requests[index][2].items()}
                        self.assertIn("Credential=alternate-key/",headers["authorization"])
                    for index in (0,1,2,3,4,5,6,7,8,11,12,13,14,15,16,17,18,19,20):
                        headers={key.lower():value for key,value in server.requests[index][2].items()}
                        self.assertIn("Credential=local-test/",headers["authorization"])
