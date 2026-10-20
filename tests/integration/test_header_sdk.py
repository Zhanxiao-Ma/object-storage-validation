"""Observe actual AWS PUT and HEAD HTTP metadata through real Java/C++ SDKs."""
import os
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_HEADER_TESTS")=="1","enable after building AWS SDK workers")
class HeaderSdkTest(unittest.TestCase):
    """Assert signed request headers and independently supplied successful response fields, including empty HEAD/PUT bodies."""
    def test_cache_expiry_and_etag_are_real_wire_fields(self):
        """Send cache/expiry via real SDK PUT, then consume a real HEAD response; expect exact metadata/status and no inferred ETag."""
        cache="public, max-age=14400";expiry="Tue, 14 Nov 2023 22:13:20 GMT";etag=chr(34)+"37b51d194a7513e45b56f6524f2d51f2"+chr(34)
        fixtures=[(200,{"ETag":etag},b""),(200,{"Content-Length":"3","ETag":etag,"Cache-Control":cache,"Expires":expiry},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(b"bar")
                result=worker.call({"op":"put_http","key":"object","body_path":str(body),"cache_control":cache,"expires_ms":1700000000000})
                self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200);self.assertEqual(result["etag"],etag)
                headers={k.lower():v for k,v in server.requests[0][2].items()}
                self.assertEqual(headers.get("cache-control"),cache);self.assertEqual(headers.get("expires"),expiry)
                self.assertEqual(server.requests[0][3],b"bar")
                result=worker.call({"op":"head_http","key":"object"})
                self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200);self.assertEqual(result["size"],3)
                self.assertEqual(result["http_headers"].get("cache-control"),cache);self.assertEqual(result["http_headers"].get("expires"),expiry)
                self.assertEqual([r[0] for r in server.requests],["PUT","HEAD"])
