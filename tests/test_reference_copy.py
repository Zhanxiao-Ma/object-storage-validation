"""Drive actual reference-worker producers/consumers across bucket boundaries."""
from pathlib import Path
import tempfile
import unittest
from osv.worker import Worker
from test_harness import config

class ReferenceCopyTest(unittest.TestCase):
    """Verify source-bucket resolution with real files and worker messages, never reimplementing the copy operation in the test."""
    def test_cross_bucket_key_collision_metadata_and_missing_sources(self):
        """Write different same-key objects in two buckets and copy/replace through production; expect source bytes/metadata, unchanged source, same-bucket self-copy HTTP 400, optional replacement ContentType default and real 404 for absent sources."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker(config(directory)["targets"][0],5)
            try:
                def call(op,bucket="a",**options):
                    result=worker.call({"op":op,"bucket":bucket,**options});self.assertTrue(result["ok"],result);return result
                for bucket in ("a","b"):call("bucket_create_raw",bucket)
                body=directory/"body";output=directory/"output"
                for bucket,data in (("a",b"source-value"),("b",b"destination-decoy")):
                    body.write_bytes(data);call("put_metadata",bucket,key="same",body_path=str(body),metadata={"origin":bucket},content_type="text/"+bucket)
                call("copy","b",key="same",source_bucket="a",source_key="same")
                result=call("get_metadata","b",key="same",output_path=str(output));self.assertEqual(output.read_bytes(),b"source-value");self.assertEqual(result["metadata"],{"origin":"a"});self.assertEqual(result["content_type"],"text/a")
                call("copy_replace","b",key="new",source_bucket="a",source_key="same",metadata={"replaced":"yes"},content_type="text/replaced")
                result=call("get_metadata","b",key="new",output_path=str(output));self.assertEqual(output.read_bytes(),b"source-value");self.assertEqual(result["metadata"],{"replaced":"yes"})
                result=call("get_metadata","a",key="same",output_path=str(output));self.assertEqual(result["metadata"],{"origin":"a"});self.assertEqual(output.read_bytes(),b"source-value")
                for options,code in (({"source_bucket":"absent","source_key":"same"},"NoSuchBucket"),({"source_bucket":"a","source_key":"missing"},None),({"source_bucket":"b","source_key":"same"},"InvalidRequest")):
                    result=worker.call({"op":"copy","bucket":"b","key":"same",**options});self.assertFalse(result["ok"],result)
                    if code:self.assertEqual(result["error_code"],code)
                    else:self.assertEqual(result["status_code"],404)
                    if code=="InvalidRequest":self.assertEqual(result["status_code"],400)
                call("copy_replace","a",key="same",source_key="same",metadata={"foo":"bar"})
                result=call("get_metadata","a",key="same",output_path=str(output))
                self.assertEqual(output.read_bytes(),b"source-value");self.assertEqual(result["metadata"],{"foo":"bar"})
                self.assertEqual(result["content_type"],"application/octet-stream")
                call("copy","b",key="default-source",source_key="same")
                call("get","b",key="default-source",output_path=str(output));self.assertEqual(output.read_bytes(),b"source-value")
            finally:worker.close()
