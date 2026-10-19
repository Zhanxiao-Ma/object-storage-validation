"""Native OBS C SDK calls from the real C++ worker, checked with fixed HTTP fixtures."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from sdk_fixtures import fixture_worker

PAYLOAD = b"independent-obs-c-sdk-payload"
BUCKET = "fixture-1250000000"


@unittest.skipUnless(os.environ.get("OSV_RUN_OBS_TESTS") == "1", "enable after linking the official OBS C SDK")
class ObsSdkContractTest(unittest.TestCase):
    """Verify C++-to-C SDK request mapping and callback results without replacing the native SDK."""

    def test_native_auth_upload_download_ranges_and_metadata(self):
        """Send real payloads and fixed full/partial responses; expect OBS auth, STS header, exact bytes and Range bounds."""
        fixtures = [(200,{"ETag":'"etag"'},b""), (200,{},PAYLOAD),
                    (206,{"Content-Range":f"bytes 0-0/{len(PAYLOAD)}"},PAYLOAD[:1]),
                    (206,{"Content-Range":f"bytes {len(PAYLOAD)-1}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"},PAYLOAD[-1:]),
                    (206,{"Content-Range":f"bytes 3-6/{len(PAYLOAD)}"},PAYLOAD[3:7]),
                    (200,{"Content-Length":str(len(PAYLOAD))},b"")]
        with tempfile.TemporaryDirectory() as temporary, fixture_worker("cpp", fixtures, "obs", {"OSV_SESSION_TOKEN":"fixture-token"}) as (worker, server):
            body, output = Path(temporary)/"body.bin", Path(temporary)/"output.bin"
            body.write_bytes(PAYLOAD)
            self.assertEqual(worker.info["sdk_language"], "c")
            self.assertEqual(worker.info["language"], "cpp")
            self.assertEqual(worker.info["auth_mode"], "OBS_OBS_TYPE")
            self.assertEqual(worker.info["sdk_version"], "3.26.6")
            for request in ({"op":"put", "key":"object", "body_path":str(body)},
                            {"op":"get", "key":"object", "output_path":str(output)}):
                result = worker.call(request)
                self.assertTrue(result["ok"], result)
            self.assertEqual(output.read_bytes(), PAYLOAD)
            for offset, length in ((0,1),(len(PAYLOAD)-1,1),(3,4)):
                result = worker.call({"op":"get", "key":"object", "offset":offset, "length":length, "output_path":str(output)})
                self.assertTrue(result["ok"], result)
                self.assertEqual(output.read_bytes(), PAYLOAD[offset:offset+length])
            self.assertEqual(worker.call({"op":"head", "key":"object"})["size"], len(PAYLOAD))
            self.assertEqual(server.requests[0][0:2], ("PUT", "/object"))
            self.assertEqual(server.requests[0][3], PAYLOAD)
            headers = {key.lower():value for key,value in server.requests[0][2].items()}
            self.assertTrue(headers["authorization"].startswith("OBS local-test:"), headers)
            self.assertIn("obs-sdk-c-"+worker.info["sdk_version"], headers["user-agent"])
            self.assertEqual(headers["x-obs-security-token"], "fixture-token")
            ranges = [{k.lower():v for k,v in request[2].items()}["range"] for request in server.requests[2:5]]
            self.assertEqual(ranges, ["bytes=0-0", f"bytes={len(PAYLOAD)-1}-{len(PAYLOAD)-1}", "bytes=3-6"])
            self.assertEqual(len(server.fixtures), 0)

    def test_zero_byte_object_and_missing_vs_forbidden(self):
        """Use real empty callbacks and HEAD error responses; expect zero size, 404 not_found and 403 sdk_error."""
        fixtures = [(200,{},b""),(200,{},b""),(200,{},b""),(204,{},b""),(404,{},b""),(403,{},b"")]
        with tempfile.TemporaryDirectory() as temporary, fixture_worker("cpp", fixtures, "obs") as (worker, server):
            body, output = Path(temporary)/"body.bin", Path(temporary)/"output.bin"
            body.write_bytes(b"")
            self.assertTrue(worker.call({"op":"put","key":"empty","body_path":str(body)})["ok"])
            self.assertTrue(worker.call({"op":"get","key":"empty","output_path":str(output)})["ok"])
            self.assertEqual(output.read_bytes(), b"")
            self.assertEqual(worker.call({"op":"head","key":"empty"})["size"], 0)
            self.assertTrue(worker.call({"op":"delete","key":"empty"})["ok"])
            missing = worker.call({"op":"head","key":"missing"})
            denied = worker.call({"op":"head","key":"forbidden"})
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["error_kind"], "not_found")
            self.assertFalse(denied["ok"])
            self.assertEqual(denied["error_kind"], "sdk_error")
            self.assertEqual(server.requests[0][3], b"")
            self.assertEqual(len(server.fixtures), 0)

    def test_multipart_callbacks_preserve_content_and_ids(self):
        """Drive real OBS multipart APIs with raw XML; expect upload ID, part body, ETag and completion XML to remain intact."""
        initial = b'<InitiateMultipartUploadResult><Bucket>fixture-1250000000</Bucket><Key>object</Key><UploadId>obs-upload</UploadId></InitiateMultipartUploadResult>'
        complete = b'<CompleteMultipartUploadResult><Location>http://fixture/object</Location><Bucket>fixture-1250000000</Bucket><Key>object</Key><ETag>etag</ETag></CompleteMultipartUploadResult>'
        fixtures = [(200,{},initial),(200,{"ETag":'"part-etag"'},b""),(200,{},complete),(204,{},b"")]
        with tempfile.TemporaryDirectory() as temporary, fixture_worker("cpp", fixtures, "obs") as (worker, server):
            body = Path(temporary)/"part.bin"
            body.write_bytes(PAYLOAD)
            upload = worker.call({"op":"mp_create","key":"object"})
            self.assertTrue(upload["ok"], upload)
            self.assertEqual(upload["upload_id"], "obs-upload")
            part = worker.call({"op":"mp_part","key":"object","upload_id":"obs-upload","part_number":2,"body_path":str(body)})
            self.assertTrue(part["ok"], part)
            self.assertEqual(part["etag"].strip('"'), "part-etag")
            result = worker.call({"op":"mp_complete","key":"object","upload_id":"obs-upload","parts":[{"part_number":2,"etag":part["etag"]}]})
            self.assertTrue(result["ok"], result)
            self.assertTrue(worker.call({"op":"mp_abort","key":"object","upload_id":"obs-upload"})["ok"])
            self.assertEqual(server.requests[1][3], PAYLOAD)
            self.assertEqual(parse_qs(urlsplit(server.requests[1][1]).query), {"partNumber":["2"],"uploadId":["obs-upload"]})
            self.assertIn(b"<PartNumber>2</PartNumber>", server.requests[2][3])
            self.assertIn(b"part-etag", server.requests[2][3])
            self.assertEqual(parse_qs(urlsplit(server.requests[3][1]).query)["uploadId"], ["obs-upload"])
            self.assertEqual(len(server.fixtures), 0)

    def test_listing_reads_all_pages_and_rejects_repeated_marker(self):
        """Feed independent pages to the real C parser; expect both keys, then failure for a repeated continuation marker."""
        first = b'<ListBucketResult><Name>fixture-1250000000</Name><Prefix>prefix/</Prefix><NextMarker>next</NextMarker><MaxKeys>1</MaxKeys><IsTruncated>true</IsTruncated><Contents><Key>prefix/one</Key><LastModified>2026-01-01T00:00:00Z</LastModified><ETag>etag</ETag><Size>1</Size><StorageClass>STANDARD</StorageClass></Contents></ListBucketResult>'
        second = first.replace(b'<IsTruncated>true',b'<IsTruncated>false').replace(b'prefix/one',b'prefix/two')
        with fixture_worker("cpp", [(200,{},first),(200,{},second),(200,{},first),(200,{},first)], "obs") as (worker, server):
            result = worker.call({"op":"list","prefix":"prefix/"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["keys"], ["prefix/one","prefix/two"])
            self.assertEqual(parse_qs(urlsplit(server.requests[1][1]).query)["marker"], ["next"])
            result = worker.call({"op":"list","prefix":"prefix/"})
            self.assertFalse(result["ok"], result)
            self.assertIn("pagination marker", result["error"])
            self.assertEqual(len(server.fixtures), 0)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires a real failing output device")
    def test_output_failure_is_not_reported_as_success(self):
        """Stream a real SDK response to /dev/full; expect failure from callback/flush and a usable worker afterward."""
        with fixture_worker("cpp", [(200,{},b"x"*65536)], "obs") as (worker, server):
            result = worker.call({"op":"get","key":"object","output_path":"/dev/full"})
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error_kind"], "worker_error")
            self.assertTrue(worker.call({"op":"info"})["ok"])
            self.assertEqual(len(server.fixtures), 0)

    def test_unavailable_retry_setting_is_rejected(self):
        """Request disabled retries from a SDK with no such public switch; expect initialization failure, not misleading metadata."""
        with self.assertRaises((RuntimeError, ValueError)):
            with fixture_worker("cpp", [], "obs", {"OSV_RETRIES":"0"}):
                self.fail("unsupported retry configuration accepted")

    def test_path_style_cannot_be_mislabeled_as_native_obs(self):
        """Request Path Style, which forces S3 in this SDK; expect rejection before any request can become native evidence."""
        with self.assertRaises((RuntimeError, ValueError)):
            with fixture_worker("cpp", [], "obs", {"OSV_PATH_STYLE":"true"}):
                self.fail("S3-only addressing accepted as OBS native")


if __name__ == "__main__":
    unittest.main()
