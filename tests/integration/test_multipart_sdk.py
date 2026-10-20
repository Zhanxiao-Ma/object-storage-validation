"""Observe authentic AWS SDK multipart HTTP requests and independent service responses."""
import os
import unittest
from pathlib import Path
import tempfile
from urllib.parse import unquote
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

    def test_copy_uses_opaque_source_key_and_actual_range_header(self):
        """Pass raw whitespace/query/Unicode source keys to actual SDKs; expect correctly encoded source headers, preserved range and server ETags."""
        keys=(" ","?versionId","中文/+%&")
        responses=[(200,{"Content-Type":"application/xml"},b'<CopyPartResult><ETag>"server-copy-etag"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></CopyPartResult>')]*len(keys)
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,responses,"s3") as (worker,server):
                for key in keys:
                    result=worker.call({"op":"mp_copy","key":"destination","source_bucket":"source-bucket","source_key":key,"upload_id":"server-upload","part_number":2,"range_header":"bytes=0-0"})
                    self.assertTrue(result["ok"],result);self.assertEqual(result["etag"],'"server-copy-etag"');self.assertEqual(result["status_code"],200)
                for key,request in zip(keys,server.requests):
                    headers={k.lower():v for k,v in request[2].items()}
                    source=headers["x-amz-copy-source"]
                    self.assertEqual(unquote(source),"source-bucket/"+key)
                    self.assertNotIn("?",source);self.assertNotIn(" ",source)
                    self.assertEqual(headers["x-amz-copy-source-range"],"bytes=0-0")
                    self.assertEqual(parse_qs(urlsplit(request[1]).query),{"partNumber":["2"],"uploadId":["server-upload"]})

    def test_list_preserves_actual_ids_owners_and_pagination(self):
        """Consume an independent multipart-list XML page; expect actual owners/cursors and caller markers serialized once without client-side pagination."""
        document=b'<ListMultipartUploadsResult><Bucket>fixture</Bucket><KeyMarker>input-key</KeyMarker><UploadIdMarker>input-id</UploadIdMarker><NextKeyMarker>server-next</NextKeyMarker><NextUploadIdMarker>server-next-id</NextUploadIdMarker><MaxUploads>1</MaxUploads><IsTruncated>true</IsTruncated><Upload><Key>server/key</Key><UploadId>server-upload</UploadId><Initiated>2026-01-01T00:00:00Z</Initiated><Owner><ID>owner-id</ID><DisplayName>owner-name</DisplayName></Owner><Initiator><ID>initiator-id</ID><DisplayName>initiator-name</DisplayName></Initiator></Upload></ListMultipartUploadsResult>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"Content-Type":"application/xml"},document)],"s3") as (worker,server):
                result=worker.call({"op":"mp_list","key_marker":"input-key","upload_id_marker":"input-id","max_uploads":1,"prefix":"original/","delimiter":"/"})
                self.assertTrue(result["ok"],result);self.assertTrue(result["truncated"])
                self.assertEqual((result["next_key_marker"],result["next_upload_id_marker"]),("server-next","server-next-id"))
                self.assertEqual(len(result["uploads"]),1);item=result["uploads"][0]
                self.assertEqual((item["key"],item["upload_id"],item["initiated_ms"]),("server/key","server-upload",1767225600000))
                self.assertEqual(item["owner"],{"id":"owner-id","display_name":"owner-name"});self.assertEqual(item["initiator"],{"id":"initiator-id","display_name":"initiator-name"})
                query=parse_qs(urlsplit(server.requests[0][1]).query,keep_blank_values=True)
                self.assertEqual(query,{"uploads":["uploads" if language=="cpp" else ""],"key-marker":["input-key"],"upload-id-marker":["input-id"],"max-uploads":["1"],"prefix":["original/"],"delimiter":["/"]})
                self.assertEqual(len(server.requests),1)

    def test_part_get_and_head_preserve_server_counts_etag_and_bytes(self):
        """Send actual PartNumber GET/HEAD requests with independent headers/body; expect original whole-object ETag, part count, length and saved bytes."""
        headers={"ETag":'"whole-object-4"',"x-amz-mp-parts-count":"4","Content-Length":"4"}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,headers,b""),(206,headers,b"abcd")],"s3") as (worker,server):
                output=Path(temporary)/"part"
                for op in ("head_part","get_part"):
                    result=worker.call({"op":op,"key":"object","part_number":2,"output_path":str(output)})
                    self.assertTrue(result["ok"],result);self.assertEqual(result["parts_count"],4);self.assertEqual(result["etag"],'"whole-object-4"');self.assertEqual(result["size"],4)
                self.assertEqual(output.read_bytes(),b"abcd")
                self.assertEqual([r[0] for r in server.requests],["HEAD","GET"])
                for request in server.requests:self.assertEqual(parse_qs(urlsplit(request[1]).query),{"partNumber":["2"]})
