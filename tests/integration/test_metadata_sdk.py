"""Check metadata transfer through real vendor SDKs using independently specified HTTP messages."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from sdk_fixtures import fixture_worker


@unittest.skipUnless(os.environ.get("OSV_RUN_METADATA_TESTS")=="1","enable after building the selected real SDK workers")
class MetadataSdkTest(unittest.TestCase):
    """Verify explicit metadata operations cannot silently ignore inputs or invent returned fields."""
    def targets(self):
        return [tuple(item.split(":")) for item in os.environ.get("OSV_PAGE_TEST_TARGETS",
            "java:s3,cpp:s3,java:oss,cpp:oss,java:obs,cpp:obs,java:tos,cpp:tos,java:cos,cpp:cos").split(",")]

    def test_put_head_get_preserve_actual_metadata(self):
        """Send literal metadata and replay independent HEAD/GET headers; expect real vendor-prefixed headers, exact metadata maps and body bytes."""
        payload=b"metadata-body-with-\x00-byte";etag='"'+hashlib.md5(payload).hexdigest()+'"'
        metadata={"one":"value-one","second-key":"value-two"};content_type="application/x-osv-test"
        prefixes={"s3":"x-amz-meta-","oss":"x-oss-meta-","obs":"x-obs-meta-","tos":"x-tos-meta-","cos":"x-cos-meta-"}
        for language,backend in self.targets():
            headers={"ETag":etag,"Content-Type":content_type,"Content-Length":str(len(payload)),"Last-Modified":"Tue, 14 Nov 2023 22:13:20 GMT"}
            headers.update({prefixes[backend]+key:value for key,value in metadata.items()})
            fixtures=[(200,{"ETag":etag},b""),(200,headers,b""),(200,headers,payload)]
            with self.subTest(language=language,backend=backend),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,backend) as (worker,server):
                for capability in ("put_metadata","head_metadata","get_metadata"):self.assertIn(capability,worker.info["capabilities"])
                body=Path(temporary)/"body";output=Path(temporary)/"output";body.write_bytes(payload)
                result=worker.call({"op":"put_metadata","key":"object","body_path":str(body),"content_type":content_type,"metadata":metadata})
                self.assertTrue(result["ok"],result)
                self.assertEqual(server.requests[0][3],payload)
                actual={k.lower():v for k,v in server.requests[0][2].items()}
                self.assertEqual(actual["content-type"],content_type)
                for key,value in metadata.items():self.assertEqual(actual[prefixes[backend]+key],value)
                for op in ("head_metadata","get_metadata"):
                    result=worker.call({"op":op,"key":"object","output_path":str(output)})
                    self.assertTrue(result["ok"],result)
                    self.assertEqual(result["metadata"],metadata)
                    self.assertEqual(result["content_type"],content_type)
                    self.assertEqual(result["size"],len(payload))
                    self.assertEqual(result["last_modified_ms"],1700000000000)
                self.assertEqual(output.read_bytes(),payload)
                self.assertEqual([request[0] for request in server.requests],["PUT","HEAD","GET"])
                self.assertEqual(len(server.fixtures),0)
