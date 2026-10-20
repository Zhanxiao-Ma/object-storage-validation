"""Observe successful HTTP responses through genuine SDK response APIs."""
import hashlib
import os
import socket
import tempfile
import unittest
from pathlib import Path
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get("OSV_RUN_HTTP_TESTS")=="1","enable after building real SDK workers")
class HttpSdkTest(unittest.TestCase):
    """Prevent Range status/header inference by asserting independently supplied HTTP responses."""
    def test_range_headers_and_actual_status(self):
        """Exercise closed, open and suffix ranges plus full GET; expect actual status, content-range and exact wire requests."""
        payload=b"testcontent"
        vectors=[("bytes=4-7",b"cont","bytes 4-7/11",206),("bytes=4-",b"content","bytes 4-10/11",206),("bytes=-7",b"content","bytes 4-10/11",206),(None,payload,None,200),(None,b"",None,200)]
        fixtures=[]
        for value,body,content_range,status in vectors:
            headers={"Content-Type":"application/octet-stream","ETag":chr(34)+hashlib.md5(payload if value else body).hexdigest()+chr(34),"Last-Modified":"Tue, 14 Nov 2023 22:13:20 GMT"}
            if content_range:headers["Content-Range"]=content_range
            fixtures.append((status,headers,body))
        targets=os.environ.get("OSV_PAGE_TEST_TARGETS","java:s3,cpp:s3,java:oss,cpp:oss,java:obs,cpp:obs,java:tos,cpp:tos,java:cos,cpp:cos")
        for target in targets.split(","):
            language,backend=target.split(":")
            with self.subTest(target=target),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,backend) as (worker,server):
                if target in ("cpp:oss","cpp:obs","java:cos"):
                    self.assertNotIn("get_http",worker.capabilities)
                    self.assertEqual(server.requests,[])
                    continue
                self.assertIn("get_http",worker.capabilities)
                output=Path(temporary)/"output"
                for index,(value,body,content_range,status) in enumerate(vectors):
                    request={"op":"get_http","key":"object","output_path":str(output)}
                    if value:request["range_header"]=value
                    result=worker.call(request)
                    self.assertTrue(result["ok"],result)
                    self.assertEqual(output.read_bytes(),body)
                    self.assertEqual(result["status_code"],status)
                    self.assertEqual(result["http_headers"].get("content-range"),content_range)
                    headers={k.lower():v for k,v in server.requests[index][2].items()}
                    self.assertEqual(headers.get("range"),value)
                self.assertEqual(len(server.requests),5)

    def test_aws_status_is_observed_instead_of_inferred_from_range(self):
        """Return HTTP 200 to a Range request through real AWS SDKs; expect 200 to remain visible for strict Ceph rejection."""
        selected=os.environ.get("OSV_PAGE_TEST_TARGETS","java:s3,cpp:s3").split(",")
        for target in selected:
            if target not in ("java:s3","cpp:s3"):continue
            language,backend=target.split(":")
            with self.subTest(target=target),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{"Content-Range":"bytes 4-7/11"},b"cont")],backend) as (worker,server):
                output=Path(temporary)/"output"
                result=worker.call({"op":"get_http","key":"object","range_header":"bytes=4-7","output_path":str(output)})
                self.assertTrue(result["ok"],result)
                self.assertEqual(result["status_code"],200)
                self.assertEqual(output.read_bytes(),b"cont")

    def test_tos_empty_get_checks_real_zero_length_and_output_io(self):
        """Consume the real TOS null-stream empty response; expect a zero-byte file, while a directory destination must still fail."""
        selected=os.environ.get("OSV_PAGE_TEST_TARGETS","cpp:tos").split(",")
        if "cpp:tos" not in selected:self.skipTest("requires TOS C++ worker")
        with tempfile.TemporaryDirectory() as temporary,fixture_worker("cpp",[(200,{},b""),(200,{},b"")],"tos") as (worker,server):
            output=Path(temporary)/"output"
            result=worker.call({"op":"get_http","key":"object","output_path":str(output)})
            self.assertTrue(result["ok"],result);self.assertEqual(result["status_code"],200);self.assertEqual(output.read_bytes(),b"")
            result=worker.call({"op":"get","key":"object","output_path":temporary})
            self.assertFalse(result["ok"],result);self.assertIn("output_path",result["error"])
            self.assertEqual(len(server.requests),2)

    def test_transport_failure_is_not_replaced_by_http_observer_error(self):
        """Connect the real AWS C++ SDK to a closed local port; expect its SDK transport error without a fabricated HTTP status or observer exception."""
        selected=os.environ.get("OSV_PAGE_TEST_TARGETS","cpp:s3").split(",")
        if "cpp:s3" not in selected:self.skipTest("requires AWS C++ worker")
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1",0));port=reserved.getsockname()[1]
        with tempfile.TemporaryDirectory() as temporary,fixture_worker("cpp",[],"s3",{"OSV_ENDPOINT":f"http://127.0.0.1:{port}"}) as (worker,server):
            result=worker.call({"op":"get_http","key":"object","output_path":str(Path(temporary)/"output")})
            self.assertFalse(result["ok"],result);self.assertEqual(result["error_kind"],"sdk_error")
            self.assertNotIn("SDK did not expose",result["error"])
            self.assertEqual(server.requests,[])
