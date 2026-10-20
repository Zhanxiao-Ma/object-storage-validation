"""Check actual SDK typed storage-class inputs and real transport timeouts."""
import os
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from sdk_fixtures import fixture_worker,FixtureHandler

class DelayedHandler(FixtureHandler):
    """Delay a real local HTTP reply so SDK timeout configuration, rather than a simulated response, determines success."""
    def do_GET(self):
        time.sleep(.3)
        try:super().do_GET()
        except (BrokenPipeError,ConnectionResetError):pass

@unittest.skipUnless(os.environ.get("OSV_RUN_STORAGE_OPTION_TESTS")=="1","requires built real AWS SDK workers")
class StorageOptionsSdkTest(unittest.TestCase):
    """Inspect real typed PUT headers and observe genuine SDK timer failures with bounded local HTTP delays."""
    def languages(self):return os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")

    def test_typed_storage_classes_all_put_paths_and_absent_field(self):
        """Feed standard and vendor class names through ordinary/SSE-C/KMS PUT; expect exact actual headers and no implicit value on the following request."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{"ETag":'"'+hashlib.md5(b"independent").hexdigest()+'"'},b"")]*10,"s3") as (worker,server):
                self.assertIn("put_storage_class",worker.capabilities)
                body=Path(temporary)/"body";body.write_bytes(b"independent")
                for op in ("put","put_sse_c","put_encryption"):
                    for storage in ("STANDARD_IA","DEEP_ARCHIVE","VENDOR-COLD"):
                        request={"op":op,"key":"original","body_path":str(body),"storage_class":storage}
                        if op=="put_sse_c":request["sse_headers"]={"x-amz-server-side-encryption-customer-algorithm":"AES256"}
                        if op=="put_encryption":request["encryption_headers"]={"x-amz-server-side-encryption":"aws:kms"}
                        result=worker.call(request);self.assertTrue(result["ok"],result)
                        self.assertEqual({k.lower():v for k,v in server.requests[-1][2].items()}["x-amz-storage-class"],storage)
                        self.assertEqual(server.requests[-1][3],b"independent")
                self.assertTrue(worker.call({"op":"put","key":"ordinary","body_path":str(body)})["ok"])
                self.assertNotIn("x-amz-storage-class",{k.lower():v for k,v in server.requests[-1][2].items()})

    def test_invalid_storage_fields_emit_zero_http(self):
        """Use null, numbers, empty strings and containers on each actual PUT path; expect SDK bridge rejection before transmission."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(b"independent")
                for op in ("put","put_sse_c","put_encryption"):
                    for value in (None,False,0,"",[],{}):
                        result=worker.call({"op":op,"key":"original","body_path":str(body),"storage_class":value,"encryption_headers":{}})
                        self.assertFalse(result["ok"],result)
                self.assertEqual(server.requests,[])

    def test_request_timeout_uses_actual_http_client(self):
        """Delay genuine replies 300ms; expect configured 100ms request timeout to fail and original source 100000ms settings to read actual bytes successfully."""
        for language in self.languages():
            for timeout,success in (("100",False),("100000",True)):
                with self.subTest(language=language,timeout=timeout),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{},b"original")],"s3",{"OSV_REQUEST_TIMEOUT_MS":timeout,"OSV_CONNECT_TIMEOUT_MS":"100000"},handler=DelayedHandler) as (worker,server):
                    self.assertIn("client_timeouts",worker.capabilities)
                    output=Path(temporary)/"out";result=worker.call({"op":"get","key":"original","output_path":str(output)})
                    self.assertEqual(result["ok"],success,result)
                    if success:self.assertEqual(output.read_bytes(),b"original")

    def test_invalid_timeout_configuration_fails_before_http(self):
        """Start genuine SDK workers with zero/negative/overflow/noninteger transport settings; expect startup rejection and no silently ignored timer."""
        for language in self.languages():
            for field,value in (("OSV_REQUEST_TIMEOUT_MS","0"),("OSV_REQUEST_TIMEOUT_MS","2147483648"),("OSV_CONNECT_TIMEOUT_MS","-1"),("OSV_CONNECT_TIMEOUT_MS","1.5")):
                with self.subTest(language=language,field=field,value=value),self.assertRaises((RuntimeError,ValueError)):
                    with fixture_worker(language,[],"s3",{field:value}):self.fail("invalid timeout was accepted")
