"""Validate real conditional GET request mapping through every SDK using independent HTTP responses."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from sdk_fixtures import fixture_worker


@unittest.skipUnless(os.environ.get("OSV_RUN_CONDITION_TESTS")=="1","enable after building selected real SDK workers")
class ConditionalSdkTest(unittest.TestCase):
    """Exercise SDK conditions on the wire instead of replacing a request builder or storage client."""
    def targets(self):
        return [tuple(item.split(":")) for item in os.environ.get("OSV_PAGE_TEST_TARGETS",
            "java:s3,cpp:s3,java:oss,cpp:oss,java:obs,cpp:obs,java:tos,cpp:tos,java:cos,cpp:cos").split(",")]

    def test_conditional_headers_dates_and_response_bytes(self):
        """Send all four literal conditions through each real SDK; expect exact HTTP headers, second precision and body bytes."""
        payload=b"independent-conditional-response";etag='"'+hashlib.md5(payload).hexdigest()+'"'
        for language,backend in self.targets():
            with self.subTest(language=language,backend=backend),tempfile.TemporaryDirectory() as temporary,fixture_worker(
                    language,[(200,{"Content-Type":"application/octet-stream","ETag":etag},payload)],backend) as (worker,server):
                self.assertIn("get_conditional",worker.info["capabilities"])
                output=Path(temporary)/"output"
                result=worker.call({"op":"get_conditional","key":"object","output_path":str(output),
                    "if_match":'"existing-tag"',"if_none_match":'"different-tag"',
                    "if_modified_since_ms":1700000000123,"if_unmodified_since_ms":1700000005999})
                self.assertTrue(result["ok"],result)
                self.assertEqual(output.read_bytes(),payload)
                self.assertEqual(len(server.requests),1)
                headers={k.lower():v for k,v in server.requests[0][2].items()}
                self.assertEqual(headers["if-match"],'"existing-tag"')
                self.assertEqual(headers["if-none-match"],'"different-tag"')
                # The OBS C SDK spells the zero-offset timezone UTC; retain its actual wire spelling.
                zone="UTC" if (language,backend)==("cpp","obs") else "GMT"
                self.assertEqual(headers["if-modified-since"],"Tue, 14 Nov 2023 22:13:20 "+zone)
                self.assertEqual(headers["if-unmodified-since"],"Tue, 14 Nov 2023 22:13:25 "+zone)

    def test_service_precondition_rejection_is_preserved(self):
        """Return an independent 412 response; expect actual SDK rejection with PreconditionFailed rather than local validation failure."""
        import json
        for language,backend in self.targets():
            body=(json.dumps({"Code":"PreconditionFailed","Message":"independent rejected condition","RequestId":"fixture"}).encode()
                  if backend=="tos" else b"<Error><Code>PreconditionFailed</Code><Message>independent rejected condition</Message><RequestId>fixture</RequestId></Error>")
            with self.subTest(language=language,backend=backend),tempfile.TemporaryDirectory() as temporary,fixture_worker(
                    language,[(412,{"Content-Type":"application/json" if backend=="tos" else "application/xml"},body)],backend) as (worker,server):
                result=worker.call({"op":"get_conditional","key":"object","if_match":'"incorrect"',"output_path":str(Path(temporary)/"output")})
                self.assertFalse(result["ok"],result)
                self.assertEqual(result["error_kind"],"sdk_error")
                self.assertEqual(result["error_code"],"PreconditionFailed")
                if "status_code" in result:self.assertEqual(result["status_code"],412)
                self.assertEqual(len(server.requests),1)
