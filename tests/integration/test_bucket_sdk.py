"""Inspect isolated-bucket routing and lifecycle requests in real AWS Java/C++ SDKs."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_BUCKET_TESTS")=="1","enable after building AWS SDK workers")
class BucketSdkTest(unittest.TestCase):
    """Verify explicit bucket routing, default bucket fallback and actual bucket response observations without cloud credentials."""
    def test_bucket_override_routes_every_request_and_keeps_default(self):
        """Use one SDK session with explicit and default buckets; expect exact paths/body/location XML and actual lifecycle response codes."""
        payload=b"scope";default=b"default";name="osv-0123456789abcdef0123456789abcdef"
        etag=lambda data:chr(34)+hashlib.md5(data).hexdigest()+chr(34)
        location=b"<LocationConstraint xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">eu-west-1</LocationConstraint>"
        fixtures=[(200,{},b""),(200,{},b""),(200,{"ETag":etag(payload)},b""),(200,{"ETag":etag(payload)},payload),(200,{"ETag":etag(default)},default),(200,{"Content-Type":"application/xml"},location),(204,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(payload);output=Path(temporary)/"output"
                result=worker.call({"op":"bucket_create_raw","bucket":name,"location":"eu-west-1"});self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200)
                result=worker.call({"op":"bucket_head","bucket":name});self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200)
                self.assertTrue(worker.call({"op":"put","bucket":name,"key":"object","body_path":str(body)})["ok"])
                self.assertTrue(worker.call({"op":"get","bucket":name,"key":"object","output_path":str(output)})["ok"]);self.assertEqual(output.read_bytes(),payload)
                self.assertTrue(worker.call({"op":"get","key":"object","output_path":str(output)})["ok"]);self.assertEqual(output.read_bytes(),default)
                result=worker.call({"op":"bucket_location","bucket":name});self.assertTrue(result["ok"],result);self.assertEqual(result["location"],"eu-west-1")
                result=worker.call({"op":"bucket_delete","bucket":name});self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],204)
                paths=[urlsplit(row[1]).path.rstrip("/") for row in server.requests]
                self.assertEqual(paths,["/"+name,"/"+name,"/"+name+"/object","/"+name+"/object","/fixture-1250000000/object","/"+name,"/"+name])
                self.assertEqual([row[0] for row in server.requests],["PUT","HEAD","PUT","GET","GET","GET","DELETE"])
                document=ET.fromstring(server.requests[0][3]);self.assertEqual(next(child.text for child in document if child.tag.rsplit("}",1)[-1]=="LocationConstraint"),"eu-west-1")
                self.assertEqual(server.requests[2][3],payload)

    def test_bucket_service_errors_preserve_exact_status_and_code(self):
        """Supply real HTTP 404 and 409 errors; expect genuine not_found/BucketNotEmpty results rather than local bucket existence guesses."""
        missing=b"<Error><Code>NoSuchBucket</Code><Message>missing</Message></Error>"
        nonempty=b"<Error><Code>BucketNotEmpty</Code><Message>not empty</Message></Error>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(404,{},b""),(404,{"Content-Type":"application/xml"},missing),(409,{"Content-Type":"application/xml"},nonempty)],"s3") as (worker,server):
                result=worker.call({"op":"bucket_head","bucket":"osv-missing"});self.assertFalse(result["ok"]);self.assertEqual(result["status_code"],404);self.assertEqual(result["error_kind"],"not_found")
                result=worker.call({"op":"bucket_delete","bucket":"osv-missing"});self.assertFalse(result["ok"]);self.assertEqual(result["status_code"],404);self.assertEqual(result["error_code"],"NoSuchBucket")
                result=worker.call({"op":"bucket_delete","bucket":"osv-nonempty"});self.assertFalse(result["ok"]);self.assertEqual(result["status_code"],409);self.assertEqual(result["error_code"],"BucketNotEmpty")
                self.assertEqual(len(server.requests),3)

    def test_scope_preflight_rejects_forbidden_or_existing_bucket(self):
        """Return actual HEAD 403 or 200 to the production scope; expect refusal after one HEAD and no creation/deletion requests."""
        from osv.bucket_scope import BucketScope
        from osv.scenarios import Context
        from osv.suites import Probe
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for status,message in ((403,"cannot establish"),(200,"refusing reuse")):
                with self.subTest(language=language,status=status),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(status,{},b"")],"s3") as (worker,server):
                    directory=Path(temporary)
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    with self.assertRaisesRegex(RuntimeError,message):
                        with BucketScope(Probe(ctx)):self.fail("unverified bucket was accepted")
                    self.assertEqual([row[0] for row in server.requests],["HEAD"])
