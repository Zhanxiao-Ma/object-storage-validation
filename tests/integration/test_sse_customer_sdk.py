"""Check actual signed SDK SSE-C requests, including deliberate missing headers."""
import os
import hashlib
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker
from osv.ceph_sse_customer import ALGORITHM,KEY,MD5,HEADERS,OTHER

@unittest.skipUnless(os.environ.get("OSV_RUN_SSE_CUSTOMER_TESTS")=="1","enable after building actual AWS SDK workers")
class SseCustomerSdkTest(unittest.TestCase):
    """Verify real wire inputs and independent response values without emulating encryption or SDK serialization."""
    def test_raw_headers_are_signed_and_missing_values_are_not_filled(self):
        """Send valid, invalid, absent and empty SSE-C headers; expect exact signed wire values, no automatic MD5 and no cross-request leakage."""
        etag='"'+hashlib.md5(b"A"*13).hexdigest()+'"'
        vectors=[HEADERS,{ALGORITHM:"AES256",KEY:HEADERS[KEY]}, {ALGORITHM:"AES256"},
                 {KEY:HEADERS[KEY],MD5:HEADERS[MD5]},dict(HEADERS,**{MD5:"AAAAAAAAAAAAAAAAAAAAAA=="}),
                 {},dict(HEADERS,**{MD5:""}),OTHER]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{"ETag":etag},b"")]*len(vectors),"s3") as (worker,server):
                path=Path(temporary)/"body";path.write_bytes(b"A"*13)
                for headers in vectors:
                    response=worker.call({"op":"put_sse_c","key":"object","body_path":str(path),"sse_headers":headers})
                    self.assertTrue(response["ok"],response);self.assertEqual(response["etag"],etag)
                self.assertEqual(len(server.requests),len(vectors))
                for expected,request in zip(vectors,server.requests):
                    headers={key.lower():value for key,value in request[2].items()}
                    self.assertEqual({key:value for key,value in headers.items() if key.startswith("x-amz-server-side-encryption-customer-")},expected)
                    self.assertEqual(request[3],b"A"*13)
                    signed=headers["authorization"].split("SignedHeaders=",1)[1].split(",",1)[0].split(";")
                    for name in expected:self.assertIn(name,signed)

    def test_get_head_observe_service_bytes_headers_and_rejections(self):
        """Read independent encrypted-object response bytes, HEAD length and actual 400 errors; expect genuine SDK outcomes with per-call header isolation."""
        body=b"actual-service-body";error=b"<Error><Code>InvalidRequest</Code><Message>missing key</Message></Error>"
        fixtures=[(200,{"Content-Length":str(len(body)),"x-amz-server-side-encryption-customer-algorithm":"AES256"},body),
                  (200,{"Content-Length":"23"},b""),(400,{"Content-Type":"application/xml"},error),
                  (400,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                output=Path(temporary)/"output"
                result=worker.call({"op":"get_sse_c","key":"object","sse_headers":HEADERS,"output_path":str(output)})
                self.assertTrue(result["ok"],result);self.assertEqual(output.read_bytes(),body)
                self.assertEqual(result["http_headers"][ALGORITHM],"AES256")
                result=worker.call({"op":"head_sse_c","key":"object","sse_headers":OTHER})
                self.assertTrue(result["ok"],result);self.assertEqual(result["size"],23)
                for operation in ("get_sse_c","head_sse_c"):
                    result=worker.call({"op":operation,"key":"object","sse_headers":{},"output_path":str(output)})
                    self.assertFalse(result["ok"],result);self.assertEqual((result["error_kind"],result["status_code"]),("sdk_error",400))
                self.assertEqual([request[0] for request in server.requests],["GET","HEAD","GET","HEAD"])
                for expected,request in zip((HEADERS,OTHER,{},{}),server.requests):
                    self.assertEqual({key.lower():value for key,value in request[2].items() if key.lower().startswith("x-amz-server-side-encryption-customer-")},expected)

    def test_unknown_header_and_invalid_shapes_fail_before_io(self):
        """Reject unrelated or nontext header overrides before HTTP; expect explicit client errors and zero service requests."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for headers in (None,[],{"authorization":"override"},{KEY:3},{MD5:None}):
                    result=worker.call({"op":"head_sse_c","key":"object","sse_headers":headers})
                    self.assertFalse(result["ok"],result);self.assertNotIn("status_code",result)
                self.assertEqual(server.requests,[])
