"""Exercise official typed copy/multipart models, retaining genuine response producers for later consumers."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit,parse_qs
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_COPY_API_TESTS")=="1","requires built real SDK workers")
class CopyApiSdkTest(unittest.TestCase):
    """Validate Java/C++ public model bindings, including C++ renamed copy-result details and actual DateTime Expires."""
    def test_real_copy_models_dates_and_opaque_multipart_producer_consumers(self):
        """Feed source XML IDs/ETags directly into actual subsequent SDK requests; expect typed dates/storage/encryption headers, opaque query encoding and exact completion part XML."""
        xml={"Content-Type":"application/xml","x-amz-server-side-encryption":"AES256"}
        fixtures=[(200,xml,b'<CopyObjectResult><ETag>"37b51d194a7513e45b56f6524f2d51f2"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></CopyObjectResult>'),(200,xml,b'<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>copied</Key><UploadId>opaque+/=&amp;</UploadId></InitiateMultipartUploadResult>'),(200,xml,b'<CopyPartResult><ETag>"8dd132c2f80b4e9f4f8d4a4d609f9956"</ETag><LastModified>2026-01-01T00:00:00Z</LastModified></CopyPartResult>'),(200,xml,b'<CompleteMultipartUploadResult><Bucket>fixture</Bucket><Key>copied</Key><ETag>"combined-1"</ETag></CompleteMultipartUploadResult>')]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,fixtures,"s3") as (worker,server):
                result=worker.call({"op":"api.CopyObject","input":{"Bucket":"fixture","Key":"copied","CopySource":"source/original","StorageClass":"STANDARD_IA","ServerSideEncryption":"AES256","Expires":1700000000000}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["data"]["CopyObjectResult"]["ETag"],'"37b51d194a7513e45b56f6524f2d51f2"')
                self.assertEqual(result["data"]["CopyObjectResult"]["LastModified"],1767225600000)
                create=worker.call({"op":"api.CreateMultipartUpload","input":{"Bucket":"fixture","Key":"copied","StorageClass":"DEEP_ARCHIVE","ServerSideEncryption":"AES256","Expires":1700000000000}})
                self.assertTrue(create["ok"],create);ident=create["data"]["UploadId"];self.assertEqual(ident,"opaque+/=&")
                part=worker.call({"op":"api.UploadPartCopy","input":{"Bucket":"fixture","Key":"copied","CopySource":"source/original","UploadId":ident,"PartNumber":1,"CopySourceRange":"bytes=0-8388607"}})
                self.assertTrue(part["ok"],part);etag=part["data"]["CopyPartResult"]["ETag"];self.assertEqual(etag,'"8dd132c2f80b4e9f4f8d4a4d609f9956"')
                complete=worker.call({"op":"api.CompleteMultipartUpload","input":{"Bucket":"fixture","Key":"copied","UploadId":ident,"MultipartUpload":{"Parts":[{"PartNumber":1,"ETag":etag}]}}})
                self.assertTrue(complete["ok"],complete);self.assertEqual(complete["data"]["ETag"],'"combined-1"')
                for row,storage in zip(server.requests[:2],("STANDARD_IA","DEEP_ARCHIVE")):
                    headers={k.lower():v for k,v in row[2].items()};self.assertEqual(headers["x-amz-storage-class"],storage);self.assertEqual(headers["x-amz-server-side-encryption"],"AES256");self.assertEqual(headers["expires"],"Tue, 14 Nov 2023 22:13:20 GMT")
                for row in server.requests[2:]:self.assertEqual(parse_qs(urlsplit(row[1]).query)["uploadId"],["opaque+/=&"])
                root=ET.fromstring(server.requests[-1][3]);values={x.tag.rsplit("}",1)[-1]:x.text for x in root.iter() if not list(x)};self.assertEqual(values,{"ETag":'"8dd132c2f80b4e9f4f8d4a4d609f9956"',"PartNumber":"1"})

    def test_scoped_and_ambiguous_copy_destinations_emit_zero_http(self):
        """Supply conflicting scoped or SDK-specific destination aliases; expect rejection before HTTP instead of silently selecting a different bucket/key."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for op in ("api.CopyObject","api.UploadPartCopy"):
                    for request in ({"op":op,"bucket":"owned","input":{"Bucket":"foreign","Key":"key","CopySource":"source/key"}},{"op":op,"input":{"Bucket":"owned","DestinationBucket":"foreign","Key":"key","CopySource":"source/key"}},{"op":op,"input":{"Bucket":"owned","Key":"key","DestinationKey":"other","CopySource":"source/key"}}):
                        result=worker.call(request);self.assertFalse(result["ok"],result)
                self.assertEqual(server.requests,[])
