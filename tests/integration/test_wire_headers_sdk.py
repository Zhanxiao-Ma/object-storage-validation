"""Validate before-signing header edits through genuine Java/C++ S3 SDK clients."""
import os
import tempfile
import unittest
import time
from pathlib import Path
from sdk_fixtures import fixture_worker,FixtureHandler

class TrickleHandler(FixtureHandler):
    """Expose total-deadline bugs with a continuously progressing HTTP response; later ordinary requests use the real fixture response path."""
    def respond(self):
        if self.headers.get("X-Osv-Trickle")!="true":return super().respond()
        self.server.requests.append((self.command,self.path,dict(self.headers),b""))
        self.wfile.write(b"HTTP/1.1 200 OK\r\n")
        for index in range(50):
            self.wfile.write(("X-Progress-%d: " % index).encode()+b"x"*2048+b"\r\n")
            self.wfile.flush();time.sleep(.1)
        self.wfile.write(b"Content-Length: 0\r\n\r\n");self.wfile.flush()
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

class InvalidLengthHandler(FixtureHandler):
    """Reject deliberately malformed HTTP lengths before body parsing, leaving SDK upload/EOF behavior observable rather than waiting on a fixture parser."""
    def do_PUT(self):
        self.server.requests.append((self.command,self.path,dict(self.headers),None))
        self.send_response(400)
        self.send_header("Content-Type","application/xml")
        payload=b'<Error><Code>InvalidArgument</Code><Message>invalid request length</Message></Error>'
        self.send_header("Content-Length",str(len(payload)));self.send_header("Connection","close")
        self.end_headers();self.wfile.write(payload);self.wfile.flush();self.close_connection=True

@unittest.skipUnless(os.environ.get("OSV_RUN_WIRE_HEADER_TESTS")=="1","enable after building real AWS SDK workers")
class WireHeadersSdkTest(unittest.TestCase):
    """Observe final transport headers and independent SDK service outcomes rather than recreating signing or HTTP clients."""
    def test_null_body_upload_cannot_read_or_block_on_rpc_input(self):
        """Send empty and negative Content-Length with a genuine null-body SDK CreateBucket; expect bounded service errors and subsequent RPC success instead of libcurl blocking on the control pipe."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"7"},b"")]*2,"s3",{"OSV_HEADER_REQUEST_TIMEOUT_MS":"500"},InvalidLengthHandler) as (worker,server):
                for length in ("","-1"):
                    start=time.monotonic()
                    result=worker.call({"op":"bucket_create_raw","bucket":"fixture","wire_headers":{"Content-Length":length}})
                    self.assertFalse(result["ok"],result);self.assertLess(time.monotonic()-start,3)
                    result=worker.call({"op":"head_http","key":"object"});self.assertTrue(result["ok"],result)
                if language=="cpp":
                    self.assertEqual([request[2]["content-length"] for request in server.requests if request[0]=="PUT"],["","-1"])
    def test_total_deadline_stops_progressing_response_and_restores_context(self):
        """Trickle headers above Curl's low-speed threshold for five seconds; expect the 200-ms compatibility deadline to abort within three seconds and a later ordinary HEAD to succeed without header leakage."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"7"},b"")],"s3",{"OSV_HEADER_REQUEST_TIMEOUT_MS":"200"},TrickleHandler) as (worker,server):
                start=time.monotonic()
                result=worker.call({"op":"head_http","key":"object","wire_headers":{"X-Osv-Trickle":"true"}})
                self.assertFalse(result["ok"],result);self.assertLess(time.monotonic()-start,3)
                result=worker.call({"op":"head_http","key":"object"})
                self.assertTrue(result["ok"],result)
                self.assertNotIn("x-osv-trickle",{key.lower() for key in server.requests[-1][2]})
    def test_literal_header_addition_removal_and_signing_are_real(self):
        """Modify headers after SDK marshalling and before official signing; expect custom signed values, actual cache-header removal and no patch on subsequent normal requests."""
        fixtures=[(200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b"")]*2
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                path=Path(temporary)/"body";path.write_bytes(b"bar")
                result=worker.call({"op":"put_http","key":"object","body_path":str(path),"cache_control":"remove-this-original-model-value","wire_headers":{"X-Osv-Value":"original-custom-value","Cache-Control":None,"Content-Type":"text/plain"}})
                self.assertTrue(result["ok"],result)
                result=worker.call({"op":"put_http","key":"object","body_path":str(path),"cache_control":"normal-model-value"});self.assertTrue(result["ok"],result)
                first={key.lower():value for key,value in server.requests[0][2].items()};second={key.lower():value for key,value in server.requests[1][2].items()}
                self.assertEqual(first["x-osv-value"],"original-custom-value");self.assertEqual(first["content-type"],"text/plain");self.assertNotIn("cache-control",first)
                self.assertIn("x-osv-value",first["authorization"])
                self.assertNotIn("x-osv-value",second);self.assertEqual(second["cache-control"],"normal-model-value")
                self.assertEqual([request[3] for request in server.requests],[b"bar",b"bar"])

    def test_generic_acl_override_retains_actual_service_error_and_cleans_context(self):
        """Override a valid SDK canned ACL with the exact bad source header, then perform a normal HEAD; expect literal wire ACL, actual 400/InvalidArgument and no leaked header patch."""
        error=b'<Error><Code>InvalidArgument</Code><Message>bad canned ACL</Message></Error>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(400,{"Content-Type":"application/xml"},error),(200,{"Content-Length":"7"},b"")],"s3") as (worker,server):
                result=worker.call({"op":"api.PutBucketAcl","input":{"Bucket":"fixture","ACL":"public-read"},"wire_headers":{"x-amz-acl":"public-ready"}})
                self.assertFalse(result["ok"],result);self.assertEqual((result["status_code"],result["error_code"]),(400,"InvalidArgument"))
                result=worker.call({"op":"head_http","key":"object"});self.assertTrue(result["ok"],result)
                first={key.lower():value for key,value in server.requests[0][2].items()};second={key.lower():value for key,value in server.requests[1][2].items()}
                self.assertEqual(first["x-amz-acl"],"public-ready");self.assertIn("x-amz-acl",first["authorization"]);self.assertNotIn("x-amz-acl",second)

    def test_before_signing_auth_removal_still_uses_official_signer(self):
        """Remove Authorization before signing as the source hook does; expect the SDK's genuine fresh signature on the final request rather than a fabricated unsigned rejection."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"1"},b"")],"s3") as (worker,server):
                result=worker.call({"op":"head_http","key":"object","wire_headers":{"Authorization":None,"X-Osv-Proof":"signed-after-removal"}})
                self.assertTrue(result["ok"],result)
                headers={key.lower():value for key,value in server.requests[0][2].items()}
                self.assertTrue(headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=local-test/"));self.assertIn("x-osv-proof",headers["authorization"])

    def test_invalid_header_shapes_and_ambiguous_names_never_reach_service(self):
        """Supply invalid values, CR/LF, empty names and duplicate case-insensitive names; expect explicit pre-HTTP errors and a later valid request proving context restoration."""
        invalid=[None,[],{"X-Test":3},{"X-Test":False},{"X-Test":"line\r\nbreak"},{"":"empty-name"},{"X-Test":"a","x-test":"b"},{"bad name":"space"}]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"1"},b"")],"s3") as (worker,server):
                for patch in invalid:
                    result=worker.call({"op":"head_http","key":"object","wire_headers":patch})
                    self.assertFalse(result["ok"],result);self.assertNotIn("status_code",result)
                self.assertEqual(server.requests,[])
                result=worker.call({"op":"head_http","key":"object","wire_headers":{"x-valid":"after-error"}});self.assertTrue(result["ok"],result)
                self.assertEqual({key.lower():value for key,value in server.requests[0][2].items()}["x-valid"],"after-error")

    def test_header_client_keeps_alternate_and_anonymous_identity_isolation(self):
        """Use the compatibility client with alternate and anonymous credentials; expect independent real signing contexts and no fallback to main on either path."""
        overrides={"OSV_ALT_ACCESS_KEY":"alternate-key","OSV_ALT_SECRET_KEY":"alternate-secret","OSV_ALT_SESSION_TOKEN":"alternate-token","OSV_HEADER_REQUEST_TIMEOUT_MS":"1000"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Length":"1"},b"")]*2,"s3",overrides) as (worker,server):
                self.assertEqual(worker.info["header_request_timeout_ms"],"1000")
                self.assertIn("wire_headers",worker.info["capabilities"])
                for identity in ("alt","anonymous"):
                    result=worker.call({"op":"head_http","key":"object","identity":identity,"wire_headers":{"X-Osv-Value":identity}});self.assertTrue(result["ok"],result)
                first={key.lower():value for key,value in server.requests[0][2].items()};second={key.lower():value for key,value in server.requests[1][2].items()}
                self.assertIn("Credential=alternate-key/",first["authorization"]);self.assertEqual(first["x-amz-security-token"],"alternate-token")
                self.assertNotIn("authorization",second);self.assertNotIn("x-amz-security-token",second)
                self.assertEqual((first["x-osv-value"],second["x-osv-value"]),("alt","anonymous"))

    def test_when_required_avoids_optional_checksum_trailer_switch(self):
        """Use the source negative-length prerequisite on a valid control PUT; expect no optional unsigned-checksum trailer mode, literal body/length and an actual SDK service response."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b"")],"s3") as (worker,server):
                path=Path(temporary)/"body";path.write_bytes(b"bar")
                result=worker.call({"op":"put_http","key":"object","body_path":str(path),"content_length":3,"wire_headers":{"X-Osv-Value":"required-checksums"},"checksum_when_required":True})
                self.assertTrue(result["ok"],result);self.assertEqual(server.requests[0][3],b"bar")
                headers={key.lower():value for key,value in server.requests[0][2].items()}
                self.assertNotIn("x-amz-trailer",headers)
                self.assertNotEqual(headers.get("x-amz-content-sha256"),"STREAMING-UNSIGNED-PAYLOAD-TRAILER")

    def test_ambiguous_creation_cleanup_requires_real_matching_owner(self):
        """Feed independent HEAD/ACL responses through genuine SDKs into the production creation scope; expect deletion only for a fresh bucket with verified matching owner, and refusal for different/missing/denied ownership or ambiguous HEAD."""
        from osv.ceph_request_headers import creation
        from osv.scenarios import Context
        from osv.suites import Probe
        def acl(owner):return ('<AccessControlPolicy><Owner>'+('' if owner is None else '<ID>'+owner+'</ID>')+'</Owner><AccessControlList/></AccessControlPolicy>').encode()
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        rejected=(400,{"Content-Type":"application/xml"},b'<Error><Code>InvalidArgument</Code><Message>original rejected header</Message></Error>')
        cleanup=[(200,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
        variants=("same","different","missing","denied","head_denied","absent")
        responses=[];segments={}
        for mode in variants:
            current=[(404,{},b''),rejected]
            if mode in ("head_denied","absent"):
                current.append((403 if mode=="head_denied" else 404,{},b''))
            else:
                current.extend([(200,{},b''),(404,{},b''),(200,{},b''),(200,{},acl("known-owner"))])
                current.append((403,{},b'') if mode=="denied" else (200,{},acl("different-owner" if mode=="different" else None if mode=="missing" else "known-owner")))
                if mode=="same":current.extend(cleanup)
                current.extend(cleanup)
            segments[mode]=len(current);responses.extend(current)
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,responses,"s3") as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))
                offset=0
                for mode in variants:
                    with self.subTest(mode=mode):
                        def execute():
                            with creation(probe,{"X-Osv-Rejected":"true"}) as (bucket,response):
                                self.assertFalse(response["ok"],response)
                                self.assertEqual((response["status_code"],response["error_code"]),(400,"InvalidArgument"))
                        if mode in ("same","absent"):execute()
                        else:
                            with self.assertRaisesRegex(RuntimeError,"ownership|owner"):execute()
                        requests=server.requests[offset:offset+segments[mode]];self.assertEqual(len(requests),segments[mode])
                        candidate_path=requests[0][1]
                        deleted=[request[1] for request in requests if request[0]=="DELETE"]
                        self.assertEqual(candidate_path in deleted,mode=="same",(mode,requests))
                        offset+=segments[mode]
