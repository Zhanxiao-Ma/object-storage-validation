"""Observe authentic AWS SDK multipart HTTP requests and independent service responses."""
import os
import unittest
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_MULTIPART_TESTS")=="1","enable after building actual AWS SDK workers")
class MultipartSdkTest(unittest.TestCase):
    """Keep SDK serialization, server ETags and service errors observable without local multipart substitution."""
    def test_metadata_completion_and_actual_server_etag(self):
        """Send native SDK metadata/initiate and complete calls; expect original XML part order, unmodified ETag and actual HTTP headers."""
        responses=[(200,{"Content-Type":"application/xml"},b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>object</Key><UploadId>server-id</UploadId></InitiateMultipartUploadResult>"),
                   (200,{"Content-Type":"application/xml","x-amz-request-id":"independent-request"},b'<CompleteMultipartUploadResult><Bucket>fixture</Bucket><Key>object</Key><ETag>"independent-final-9"</ETag></CompleteMultipartUploadResult>')]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,responses,"s3") as (worker,server):
                result=worker.call({"op":"mp_create_metadata","key":"object","content_type":"text/bla","metadata":{"foo":"bar","other":"original"}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["upload_id"],"server-id")
                headers={k.lower():v for k,v in server.requests[0][2].items()}
                self.assertEqual(headers["content-type"],"text/bla");self.assertEqual(headers["x-amz-meta-foo"],"bar");self.assertEqual(headers["x-amz-meta-other"],"original")
                result=worker.call({"op":"mp_complete_observed","key":"object","upload_id":"server-id","parts":[{"part_number":2,"etag":"from-part-two"},{"part_number":1,"etag":"from-part-one"}]})
                self.assertTrue(result["ok"],result);self.assertEqual(result["etag"],'"independent-final-9"');self.assertEqual(result["status_code"],200)
                self.assertEqual(result["http_headers"]["x-amz-request-id"],"independent-request")
                nodes=ET.fromstring(server.requests[1][3])
                self.assertEqual([{n.tag.rsplit("}",1)[-1]:n.text for n in part} for part in nodes],[{"PartNumber":"2","ETag":"from-part-two"},{"PartNumber":"1","ETag":"from-part-one"}])
                self.assertEqual(parse_qs(urlsplit(server.requests[1][1]).query)["uploadId"],["server-id"])

    def test_empty_body_and_missing_upload_errors_reach_sdk(self):
        """Omit the complete body or abort a missing upload; expect one service request each and untouched HTTP/error codes instead of adapter-generated failures."""
        responses=[(400,{"Content-Type":"application/xml"},b"<Error><Code>MalformedXML</Code><Message>empty</Message></Error>"),
                   (404,{"Content-Type":"application/xml"},b"<Error><Code>NoSuchUpload</Code><Message>missing</Message></Error>")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,responses,"s3") as (worker,server):
                for op,status,code in (("mp_complete_empty",400,"MalformedXML"),("mp_abort_raw",404,"NoSuchUpload")):
                    result=worker.call({"op":op,"key":"object","upload_id":"missing-id"})
                    self.assertFalse(result["ok"],result);self.assertEqual(result["status_code"],status);self.assertEqual(result["error_code"],code)
                self.assertEqual(len(server.requests),2);self.assertEqual(server.requests[0][3],b"")
                self.assertEqual([r[0] for r in server.requests],["POST","DELETE"])
