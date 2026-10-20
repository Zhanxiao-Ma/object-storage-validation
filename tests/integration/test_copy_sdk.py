"""Verify server-side copy request mapping with the real SDKs and independent service responses."""
import json
import os
import unittest
from urllib.parse import unquote,unquote_plus,urlsplit

from sdk_fixtures import fixture_worker


@unittest.skipUnless(os.environ.get("OSV_RUN_COPY_TESTS")=="1","enable after building the selected real SDK workers")
class CopySdkTest(unittest.TestCase):
    """Check source encoding and replacement directives without implementing a fake storage engine."""
    def targets(self):
        return [tuple(item.split(":")) for item in os.environ.get("OSV_PAGE_TEST_TARGETS",
            "java:s3,cpp:s3,java:oss,cpp:oss,java:obs,cpp:obs,java:tos,cpp:tos,java:cos,cpp:cos").split(",")]

    def test_copy_source_encoding_and_metadata_replacement(self):
        """Send reserved and Unicode source keys through SDK copy APIs; expect a single lossless encoding and explicit replacement metadata."""
        source="dir/中文 +%25?#&";destination="dest/中文 +%25?#&"
        prefixes={"s3":"x-amz-","oss":"x-oss-","obs":"x-obs-","tos":"x-tos-","cos":"x-cos-"}
        for language,backend in self.targets():
            raw=(json.dumps({"ETag":"fixture-etag","LastModified":"2023-11-14T22:13:20.000Z"}).encode() if backend=="tos"
                 else b"<CopyObjectResult><ETag>fixture-etag</ETag><LastModified>2023-11-14T22:13:20.000Z</LastModified></CopyObjectResult>")
            fixtures=[(200,{"Content-Type":"application/json" if backend=="tos" else "application/xml","ETag":"fixture-etag"},raw)]*2
            with self.subTest(language=language,backend=backend),fixture_worker(language,fixtures,backend) as (worker,server):
                for operation in ("copy","copy_replace"):
                    self.assertIn(operation,worker.info["capabilities"])
                    request={"op":operation,"key":destination,"source_key":source}
                    if operation=="copy_replace":request.update(content_type="text/osv-replaced",metadata={"new-key":"new-value"})
                    response=worker.call(request)
                    self.assertTrue(response["ok"],response)
                    self.assertEqual(response["etag"].strip('"'),"fixture-etag")
                self.assertEqual(len(server.requests),2)
                prefix=prefixes[backend]
                for method,path,headers,body in server.requests:
                    self.assertEqual(method,"PUT")
                    self.assertTrue(unquote(urlsplit(path).path).endswith("/"+destination),path)
                    actual={k.lower():v for k,v in headers.items()}
                    # TOS Java uses form-style space encoding; literal plus is independently escaped as %2B.
                    decode=unquote_plus if (language,backend)==("java","tos") else unquote
                    decoded=decode(actual[prefix+"copy-source"]).lstrip("/")
                    self.assertEqual(decoded.split("/",1)[1],source)
                    expected_bucket="fixture-1250000000"
                    expected_host=(urlsplit(worker.info["endpoint"]).netloc if language=="java" else expected_bucket+".cos.ap-guangzhou.myqcloud.com") if backend=="cos" else expected_bucket
                    self.assertEqual(decoded.split("/",1)[0],expected_host)
                replaced={k.lower():v for k,v in server.requests[1][2].items()}
                self.assertIn(replaced[prefix+"metadata-directive"].upper(),("REPLACE","REPLACED"))
                self.assertEqual(replaced[prefix+"meta-new-key"],"new-value")
                self.assertEqual(replaced["content-type"],"text/osv-replaced")
                self.assertEqual(len(server.fixtures),0)
