"""Real COS SDK adapter contract tests against fixed HTTP fixtures, not a COS service emulator."""
from collections import deque
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from osv.worker import Worker

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = b"independent-sdk-fixture-bytes"
ETAG = '"'+hashlib.md5(PAYLOAD).hexdigest()+'"'


class FixtureHandler(BaseHTTPRequestHandler):
    """Record real requests and replay supplied raw responses without implementing storage behavior."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        status, headers, payload = self.server.fixtures.popleft()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_PUT = do_HEAD = do_POST = do_DELETE = respond


@contextmanager
def fixture_worker(language, fixtures):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.fixtures = deque(fixtures)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = (["java", "-Xmx256m", "-jar", str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"), "cos"]
               if language == "java" else [str(ROOT/"drivers/cpp/build/osv-cpp-cos")])
    environment = {"OSV_ACCESS_KEY":"local-test", "OSV_SECRET_KEY":"local-test", "OSV_SESSION_TOKEN":"",
        "OSV_ENDPOINT":f"http://127.0.0.1:{server.server_port}", "OSV_BUCKET":"fixture-1250000000",
        "OSV_REGION":"ap-guangzhou", "OSV_RETRIES":"0", "OSV_CONNECTIONS":"2"}
    try:
        with patch.dict(os.environ, environment):
            worker = Worker({"command":command, "language":language, "api":"native"}, 15)
            try:
                yield worker, server
            finally:
                worker.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def languages():
    selected = os.environ.get("OSV_COS_TEST_LANGUAGES", "java,cpp").split(",")
    if not selected or any(name not in ("java", "cpp") for name in selected):
        raise ValueError("OSV_COS_TEST_LANGUAGES must select java and/or cpp")
    return selected


@unittest.skipUnless(os.environ.get("OSV_RUN_COS_TESTS") == "1", "enable explicitly after building the COS SDK drivers")
class CosSdkContractTest(unittest.TestCase):
    """Check production adapters through real official SDKs with independently specified wire expectations."""

    def test_real_put_range_head_delete_and_error_classification(self):
        """Replay fixed HTTP results; expect exact PUT bytes, Range header/body and distinct 404 versus 403 errors."""
        fixtures = [(200, {"ETag":ETAG}, b""),
                    (200, {"ETag":ETAG}, PAYLOAD),
                    (206, {"Content-Range":f"bytes 3-6/{len(PAYLOAD)}", "ETag":ETAG}, PAYLOAD[3:7]),
                    (200, {"Content-Length":str(len(PAYLOAD)), "ETag":ETAG}, b""),
                    (204, {}, b""), (404, {}, b""), (403, {}, b"")]
        for language in languages():
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                body, output = directory/"body.bin", directory/"result.bin"
                body.write_bytes(PAYLOAD)
                with fixture_worker(language, fixtures) as (worker, server):
                    self.assertEqual(worker.info["backend"], "cos")
                    self.assertTrue(worker.info["sdk_version"])
                    self.assertTrue(worker.call({"op":"put", "key":"object", "body_path":str(body)})["ok"])
                    full = worker.call({"op":"get", "key":"object", "output_path":str(output)})
                    self.assertTrue(full["ok"], full)
                    self.assertEqual(output.read_bytes(), PAYLOAD)
                    response = worker.call({"op":"get", "key":"object", "offset":3, "length":4, "output_path":str(output)})
                    self.assertTrue(response["ok"], response)
                    self.assertEqual(output.read_bytes(), PAYLOAD[3:7])
                    self.assertEqual(worker.call({"op":"head", "key":"object"})["size"], len(PAYLOAD))
                    self.assertTrue(worker.call({"op":"delete", "key":"object"})["ok"])
                    missing = worker.call({"op":"head", "key":"missing"})
                    denied = worker.call({"op":"head", "key":"denied"})
                    self.assertFalse(missing["ok"])
                    self.assertEqual(missing["error_kind"], "not_found")
                    self.assertFalse(denied["ok"])
                    self.assertEqual(denied["error_kind"], "sdk_error")
                    self.assertEqual(len(server.fixtures), 0)
                    requests = server.requests
                    self.assertEqual(requests[0][0:2], ("PUT", "/object"))
                    self.assertEqual(requests[0][3], PAYLOAD)
                    headers = {key.lower():value for key,value in requests[2][2].items()}
                    self.assertEqual(headers["range"], "bytes=3-6")
                    self.assertIn("q-sign-algorithm=sha1", {k.lower():v for k,v in requests[0][2].items()}["authorization"])
                    self.assertEqual([r[0] for r in requests], ["PUT","GET","GET","HEAD","DELETE","HEAD","HEAD"])

    def test_zero_length_object_uses_real_empty_streams(self):
        """Send and receive empty payloads through each real SDK; expect zero bytes and HEAD size zero."""
        empty_etag = '"'+hashlib.md5(b"").hexdigest()+'"'
        fixtures = [(200, {"ETag":empty_etag}, b""), (200, {"ETag":empty_etag}, b""), (200, {"Content-Length":"0"}, b"")]
        for language in languages():
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                body, output = Path(temporary)/"empty.bin", Path(temporary)/"output.bin"
                body.write_bytes(b"")
                with fixture_worker(language, fixtures) as (worker, server):
                    result = worker.call({"op":"put", "key":"empty", "body_path":str(body)})
                    self.assertTrue(result["ok"], result)
                    result = worker.call({"op":"get", "key":"empty", "output_path":str(output)})
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(output.read_bytes(), b"")
                    self.assertEqual(worker.call({"op":"head", "key":"empty"})["size"], 0)
                    self.assertEqual(server.requests[0][3], b"")
                    self.assertEqual(len(server.fixtures), 0)

    def test_cpp_full_get_keeps_sdk_integrity_check(self):
        """Return a wrong whole-object ETag to the real C++ SDK; expect SDK failure while Range-only disabling stays scoped."""
        if "cpp" not in languages():
            self.skipTest("C++ COS SDK not selected")
        fixtures = [(200, {"ETag":'"00000000000000000000000000000000"'}, PAYLOAD)]
        with tempfile.TemporaryDirectory() as temporary, fixture_worker("cpp", fixtures) as (worker, server):
            result = worker.call({"op":"get", "key":"object", "output_path":str(Path(temporary)/"body.bin")})
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error_kind"], "sdk_error")
            self.assertIn("etag", result["error"].lower())
            self.assertEqual(len(server.fixtures), 0)

    def test_real_multipart_requests_preserve_upload_id_part_and_etag(self):
        """Feed fixed multipart XML to real SDKs; expect preserved upload ID, part number, content and completion ETag."""
        initial = b'<InitiateMultipartUploadResult><Bucket>fixture-1250000000</Bucket><Key>object</Key><UploadId>fixture-upload</UploadId></InitiateMultipartUploadResult>'
        complete = ('<CompleteMultipartUploadResult><Location>http://fixture/object</Location><Bucket>fixture-1250000000</Bucket><Key>object</Key><ETag>'+ETAG+'</ETag></CompleteMultipartUploadResult>').encode()
        fixtures = [(200, {}, initial), (200, {"ETag":ETAG}, b""), (200, {}, complete), (204, {}, b"")]
        for language in languages():
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temporary:
                body = Path(temporary)/"part.bin"
                body.write_bytes(PAYLOAD)
                with fixture_worker(language, fixtures) as (worker, server):
                    upload = worker.call({"op":"mp_create", "key":"object"})
                    self.assertTrue(upload["ok"], upload)
                    self.assertEqual(upload["upload_id"], "fixture-upload")
                    part = worker.call({"op":"mp_part", "key":"object", "upload_id":upload["upload_id"], "part_number":2, "body_path":str(body)})
                    self.assertTrue(part["ok"], part)
                    self.assertEqual(part["etag"].strip('"'), ETAG.strip('"'))
                    response = worker.call({"op":"mp_complete", "key":"object", "upload_id":upload["upload_id"], "parts":[{"part_number":2,"etag":part["etag"]}]})
                    self.assertTrue(response["ok"], response)
                    self.assertTrue(worker.call({"op":"mp_abort", "key":"object", "upload_id":upload["upload_id"]})["ok"])
                    self.assertEqual(len(server.fixtures), 0)
                    requests = server.requests
                    self.assertIn("uploads", parse_qs(urlsplit(requests[0][1]).query, keep_blank_values=True))
                    self.assertEqual(parse_qs(urlsplit(requests[1][1]).query), {"partNumber":["2"], "uploadId":["fixture-upload"]})
                    self.assertEqual(requests[1][3], PAYLOAD)
                    self.assertIn(b"<PartNumber>2</PartNumber>", requests[2][3])
                    self.assertIn(ETAG.strip('"').encode(), requests[2][3])
                    self.assertEqual(parse_qs(urlsplit(requests[3][1]).query)["uploadId"], ["fixture-upload"])

    def test_real_listing_follows_next_marker(self):
        """Supply two independently specified XML pages; expect both keys and the actual continuation marker request."""
        first = b'<ListBucketResult><Name>fixture-1250000000</Name><Prefix>prefix/</Prefix><Marker></Marker><NextMarker>next-page</NextMarker><MaxKeys>1</MaxKeys><IsTruncated>true</IsTruncated><Contents><Key>prefix/one</Key><LastModified>2026-01-01T00:00:00Z</LastModified><ETag>etag</ETag><Size>1</Size><StorageClass>STANDARD</StorageClass></Contents></ListBucketResult>'
        second = first.replace(b'<IsTruncated>true', b'<IsTruncated>false').replace(b'prefix/one', b'prefix/two')
        for language in languages():
            with self.subTest(language=language), fixture_worker(language, [(200,{},first),(200,{},second)]) as (worker, server):
                result = worker.call({"op":"list", "prefix":"prefix/"})
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["keys"], ["prefix/one", "prefix/two"])
                self.assertEqual(parse_qs(urlsplit(server.requests[1][1]).query)["marker"], ["next-page"])
                self.assertEqual(len(server.fixtures), 0)

    def test_repeated_pagination_marker_is_rejected(self):
        """Return a repeated continuation marker through actual SDK XML parsing; expect failure instead of an endless list."""
        page = b'<ListBucketResult><Name>fixture-1250000000</Name><Prefix>prefix/</Prefix><NextMarker>same</NextMarker><MaxKeys>1</MaxKeys><IsTruncated>true</IsTruncated></ListBucketResult>'
        for language in languages():
            with self.subTest(language=language), fixture_worker(language, [(200,{},page),(200,{},page)]) as (worker, server):
                result = worker.call({"op":"list", "prefix":"prefix/"})
                self.assertFalse(result["ok"], result)
                self.assertIn("pagination marker", result["error"])
                self.assertEqual(len(server.fixtures), 0)


if __name__ == "__main__":
    unittest.main()
