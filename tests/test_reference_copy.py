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

    def test_conditional_status_and_tag_count_follow_real_stored_object(self):
        """Create actual bytes/tags through the production worker; expect exact 412/304 statuses and HEAD tag count changing from absent to 2 back to absent."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker(config(directory)["targets"][0],5)
            try:
                body=directory/"body";body.write_bytes(b"bar")
                self.assertTrue(worker.call({"op":"put","key":"test","body_path":str(body)})["ok"])
                output=directory/"output"
                for condition,status,code in (({"if_match":'"ABCORZ"'},412,"PreconditionFailed"),({"if_unmodified_since_ms":0},412,"PreconditionFailed"),({"if_none_match":"*"},304,"NotModified"),({"if_modified_since_ms":9223372036854775807},304,"NotModified")):
                    row=worker.call({"op":"get_http","key":"test","output_path":str(output),**condition})
                    self.assertFalse(row["ok"],row);self.assertEqual(row["status_code"],status);self.assertEqual(row["error_code"],code)
                self.assertNotIn("x-amz-tagging-count",worker.call({"op":"head_http","key":"test"})["http_headers"])
                self.assertTrue(worker.call({"op":"tags_put","key":"test","tags":[{"key":"a","value":"1"},{"key":"b","value":"2"}]})["ok"])
                self.assertEqual(worker.call({"op":"head_http","key":"test"})["http_headers"]["x-amz-tagging-count"],"2")
                self.assertTrue(worker.call({"op":"tags_delete","key":"test"})["ok"])
                self.assertNotIn("x-amz-tagging-count",worker.call({"op":"head_http","key":"test"})["http_headers"])
            finally:worker.close()

    def test_empty_cursor_and_upload_key_binding(self):
        """Create real reference upload state; expect wrong-key abort to preserve its ID, correct abort to remove it, and empty cursor to differ from omission."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker(config(directory)["targets"][0],5)
            try:
                absent=worker.call({"op":"list_v2_observed","prefix":""})
                self.assertTrue(absent["ok"],absent)
                empty=worker.call({"op":"list_v2_observed","prefix":"","token":""})
                self.assertEqual((empty["ok"],empty["status_code"],empty["error_code"]),(False,400,"InvalidArgument"))
                created=worker.call({"op":"mp_create_metadata","key":"source"})
                self.assertTrue(created["ok"],created);ident=created["upload_id"]
                wrong=worker.call({"op":"mp_abort_raw","key":"other","upload_id":ident})
                self.assertEqual((wrong["ok"],wrong["status_code"],wrong["error_code"]),(False,404,"NoSuchUpload"))
                listing=worker.call({"op":"mp_list","prefix":""})
                self.assertEqual([(x["key"],x["upload_id"]) for x in listing["uploads"]],[("source",ident)])
                self.assertTrue(worker.call({"op":"mp_abort_raw","key":"source","upload_id":ident})["ok"])
                self.assertEqual(worker.call({"op":"mp_list","prefix":""})["uploads"],[])
            finally:worker.close()
