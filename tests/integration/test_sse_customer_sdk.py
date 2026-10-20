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

    def test_multipart_headers_parts_and_real_range_are_preserved(self):
        """Use actual SDK multipart requests plus independent part observations; expect signed SSE-C/content type on every stage, exact upload bytes, real PartNumber/Range and service PartsCount."""
        from urllib.parse import parse_qs,urlsplit
        payload=b"real-upload-part";etag='"'+hashlib.md5(payload).hexdigest()+'"'
        create=b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>object</Key><UploadId>actual-upload-id</UploadId></InitiateMultipartUploadResult>'
        complete=b'<CompleteMultipartUploadResult><ETag>"service-whole-etag-4"</ETag></CompleteMultipartUploadResult>'
        fixtures=[(200,{"Content-Type":"application/xml"},create),(200,{"ETag":etag},b""),
                  (200,{"Content-Type":"application/xml"},complete),(200,{"Content-Length":"13","x-amz-mp-parts-count":"4","ETag":'"service-whole-etag-4"'},b""),
                  (206,{"Content-Length":"3","x-amz-mp-parts-count":"4","Content-Range":"bytes 4-6/13"},b"XYZ")]
        headers={**HEADERS,"content-type":"text/plain"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                path=Path(temporary)/"body";path.write_bytes(payload);output=Path(temporary)/"output"
                result=worker.call({"op":"mp_create_sse_c","key":"object","sse_headers":headers,"metadata":{"foo":"bar"}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["upload_id"],"actual-upload-id")
                result=worker.call({"op":"mp_part_sse_c","key":"object","upload_id":"actual-upload-id","part_number":3,"body_path":str(path),"sse_headers":headers})
                self.assertTrue(result["ok"],result);self.assertEqual(result["etag"],etag)
                result=worker.call({"op":"mp_complete_sse_c","key":"object","upload_id":"actual-upload-id","parts":[{"part_number":3,"etag":etag}],"sse_headers":headers})
                self.assertTrue(result["ok"],result);self.assertEqual(result["etag"],'"service-whole-etag-4"')
                result=worker.call({"op":"head_sse_c","key":"object","part_number":3,"sse_headers":headers})
                self.assertTrue(result["ok"],result);self.assertEqual((result["size"],result["parts_count"]),(13,4))
                result=worker.call({"op":"get_sse_c","key":"object","part_number":3,"range_header":"bytes=4-6","output_path":str(output),"sse_headers":headers})
                self.assertTrue(result["ok"],result);self.assertEqual(output.read_bytes(),b"XYZ");self.assertEqual((result["status_code"],result["parts_count"]),(206,4))
                self.assertEqual(server.requests[1][3],payload)
                for request in server.requests:
                    actual={key.lower():value for key,value in request[2].items()}
                    for key,value in headers.items():self.assertEqual(actual[key],value);self.assertIn(key,actual["authorization"])
                for index in (1,3,4):self.assertEqual(parse_qs(urlsplit(server.requests[index][1]).query)["partNumber"],["3"])
                self.assertEqual({key.lower():value for key,value in server.requests[4][2].items()}["range"],"bytes=4-6")
                self.assertEqual({key.lower():value for key,value in server.requests[0][2].items()}["x-amz-meta-foo"],"bar")

    def test_source_bad_chunk_aborts_upload_after_real_service_rejection(self):
        """Execute the real negative source scenario with an independently supplied 400; expect the original wrong key on UploadPart, actual abort and confirmed bucket deletion."""
        from osv.ceph_sse_multipart import multipart
        from osv.scenarios import Context
        from osv.suites import Probe
        create=b'<InitiateMultipartUploadResult><UploadId>owned-encrypted-upload</UploadId></InitiateMultipartUploadResult>'
        error=b'<Error><Code>InvalidRequest</Code><Message>incorrect key</Message></Error>'
        listing=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{"Content-Type":"application/xml"},create),
                  (400,{"Content-Type":"application/xml"},error),(204,{},b""),
                  (200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},listing),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                evidence=multipart(Probe(ctx),"wrong_part_key")
                self.assertTrue(evidence["cleanup_confirmed"])
                self.assertEqual([request[0] for request in server.requests],["HEAD","PUT","POST","PUT","DELETE","HEAD","DELETE","GET","DELETE","HEAD"])
                headers={key.lower():value for key,value in server.requests[3][2].items()}
                for key,value in OTHER.items():self.assertEqual(headers[key],value)
                self.assertEqual(len(server.requests[3][3]),5*1024*1024)
                self.assertIn("uploadId=owned-encrypted-upload",server.requests[4][1])
