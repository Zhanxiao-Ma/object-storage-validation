"""Validate actual SDK object SSE-S3/KMS request and response headers."""
import os
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker
from osv.ceph_encryption_objects import ALGORITHM,KMS_KEY

@unittest.skipUnless(os.environ.get("OSV_RUN_ENCRYPTION_TESTS")=="1","enable after building actual Java/C++ AWS SDK workers")
class EncryptionObjectsSdkTest(unittest.TestCase):
    """Use independent HTTP fixtures to verify real signed encryption fields, source omissions and service observations."""
    def test_put_headers_missing_key_and_defaults_are_exact_wire_inputs(self):
        """Send explicit S3/KMS and original incomplete headers; expect exactly requested fields in SignedHeaders, original body bytes and no leakage into default requests."""
        vectors=[{ALGORITHM:"AES256"},{ALGORITHM:"aws:kms",KMS_KEY:"original-key-id"},{ALGORITHM:"aws:kms"},{KMS_KEY:"testkey-2"},{}]
        error=b'<Error><Code>InvalidArgument</Code><Message>independent service rejection</Message></Error>'
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(400,{"Content-Type":"application/xml"},error)]*len(vectors),"s3") as (worker,server):
                path=Path(temporary)/"body";path.write_bytes(b"A"*13)
                for headers in vectors:
                    result=worker.call({"op":"put_encryption","key":"testobj","encryption_headers":headers,"body_path":str(path)})
                    self.assertFalse(result["ok"],result);self.assertEqual((result["status_code"],result["error_code"]),(400,"InvalidArgument"))
                for expected,request in zip(vectors,server.requests):
                    actual={key.lower():value for key,value in request[2].items()}
                    self.assertEqual({key:value for key,value in actual.items() if key.startswith("x-amz-server-side-encryption")},expected)
                    self.assertEqual(request[3],b"A"*13)
                    for name in expected:self.assertIn(name,actual["authorization"])

    def test_get_head_use_actual_service_encryption_not_request_echo(self):
        """Request a different KMS ID from the independent response; expect actual bytes, algorithm/key headers and HEAD length, then no header leakage into a default GET."""
        headers={ALGORITHM:"aws:kms",KMS_KEY:"server-key-id"}
        fixtures=[(200,{**headers,"Content-Length":"4"},b"real"),(200,{**headers,"Content-Length":"17"},b""),(200,{ALGORITHM:"AES256","Content-Length":"3"},b"new")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                path=Path(temporary)/"output"
                result=worker.call({"op":"get_encryption","key":"testobj","encryption_headers":{ALGORITHM:"aws:kms",KMS_KEY:"request-key-id"},"output_path":str(path)})
                self.assertTrue(result["ok"],result);self.assertEqual(path.read_bytes(),b"real");self.assertEqual(result["http_headers"][KMS_KEY],"server-key-id")
                result=worker.call({"op":"head_encryption","key":"testobj","encryption_headers":{ALGORITHM:"AES256"}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["size"],17);self.assertEqual(result["http_headers"][ALGORITHM],"aws:kms")
                result=worker.call({"op":"get_encryption","key":"testobj","encryption_headers":{},"output_path":str(path)})
                self.assertTrue(result["ok"],result);self.assertEqual(path.read_bytes(),b"new");self.assertEqual(result["http_headers"][ALGORITHM],"AES256")
                for expected,request in zip(({ALGORITHM:"aws:kms",KMS_KEY:"request-key-id"},{ALGORITHM:"AES256"},{}),server.requests):
                    self.assertEqual({key.lower():value for key,value in request[2].items() if key.lower().startswith("x-amz-server-side-encryption")},expected)

    def test_ambiguous_override_maps_do_not_reach_http(self):
        """Supply two conflicting header maps; expect a client error before network IO rather than silently selecting one map."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                result=worker.call({"op":"head_encryption","key":"testobj","sse_headers":{},"encryption_headers":{ALGORITHM:"AES256"}})
                self.assertFalse(result["ok"],result);self.assertNotIn("status_code",result);self.assertEqual(server.requests,[])
