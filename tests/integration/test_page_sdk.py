"""Inspect actual SDK single-page requests, parsed metadata and service errors across every driver."""
import os
import json
import hashlib
import tempfile
from pathlib import Path
import unittest
from urllib.parse import parse_qs,urlsplit

from sdk_fixtures import fixture_worker

PAGE=b"""<ListBucketResult><Name>fixture-1250000000</Name><Prefix>prefix/</Prefix><Delimiter>/</Delimiter><MaxKeys>1</MaxKeys><KeyCount>1</KeyCount><IsTruncated>true</IsTruncated><NextMarker>cursor</NextMarker><NextContinuationToken>cursor</NextContinuationToken><Contents><Key>prefix/one</Key><Size>4</Size><LastModified>2023-11-14T22:13:20.000Z</LastModified><ETag>etag</ETag><StorageClass>STANDARD</StorageClass><Owner><ID>id</ID><DisplayName>name</DisplayName></Owner></Contents><CommonPrefixes><Prefix>prefix/dir/</Prefix></CommonPrefixes></ListBucketResult>"""
TOS_PAGE=json.dumps({"Name":"fixture-1250000000","Prefix":"prefix/","Delimiter":"/","MaxKeys":1,"KeyCount":1,
    "IsTruncated":True,"NextContinuationToken":"cursor","Contents":[{"Key":"prefix/one","Size":4,
    "LastModified":"2023-11-14T22:13:20.000Z","ETag":"etag","StorageClass":"STANDARD"}],
    "CommonPrefixes":[{"Prefix":"prefix/dir/"}]}).encode()
TOS_INVALID=json.dumps({"Code":"InvalidPart","Message":"independent native service error","RequestId":"fixture"}).encode()
INVALID=b"""<Error><Code>InvalidPart</Code><Message>independently supplied server error</Message><RequestId>fixture</RequestId></Error>"""


@unittest.skipUnless(os.environ.get("OSV_RUN_PAGE_TESTS")=="1","enable after building selected real SDK workers")
class PageSdkTest(unittest.TestCase):
    """Keep malformed client input distinct from an actual service rejection and verify single-page wire parameters."""
    def targets(self):
        selected=os.environ.get("OSV_PAGE_TEST_TARGETS","java:s3,cpp:s3,java:oss,cpp:oss,java:obs,cpp:obs,java:tos,cpp:tos,java:cos,cpp:cos")
        return [tuple(item.split(":")) for item in selected.split(",")]

    def test_single_page_preserves_parameters_and_real_metadata(self):
        """Replay one raw XML page per SDK; expect exact request options, no auto-pagination and preserved size/time/cursor."""
        for language,backend in self.targets():
            with self.subTest(language=language,backend=backend),fixture_worker(language,[(200,{"Content-Type":"application/json" if backend=="tos" else "application/xml"},TOS_PAGE if backend=="tos" else PAGE)],backend) as (worker,server):
                result=worker.call({"op":"list_page","prefix":"prefix/","delimiter":"/","marker":"prior","max_keys":1})
                self.assertTrue(result["ok"],result)
                self.assertEqual(result["keys"],["prefix/one"])
                self.assertEqual(result["objects"][0]["size"],4)
                self.assertEqual(result["objects"][0]["last_modified_ms"],1700000000000)
                self.assertEqual(result["common_prefixes"],["prefix/dir/"])
                self.assertTrue(result["truncated"])
                self.assertEqual(result["next_token"],"cursor")
                self.assertEqual(len(server.requests),1)
                query=parse_qs(urlsplit(server.requests[0][1]).query)
                self.assertEqual(query["prefix"],["prefix/"])
                self.assertEqual(query["delimiter"],["/"])
                self.assertEqual(query["max-keys"],["1"])
                self.assertEqual(query.get("start-after",query.get("marker")),["prior"])
                self.assertEqual(len(server.fixtures),0)
                authorization={key.lower():value for key,value in server.requests[0][2].items()}.get("authorization","")
                expected={"s3":("AWS4-HMAC-SHA256 ",),"oss":("OSS ","OSS4-HMAC-SHA256 "),
                          "obs":("OBS ",),"tos":("TOS4-HMAC-SHA256 ",),"cos":("q-sign-algorithm=",)}
                self.assertTrue(authorization.startswith(expected[backend]),authorization)

    def test_invalid_etag_reaches_service_not_just_client_validation(self):
        """Send a well-typed incorrect ETag through each real SDK; expect an HTTP request and preserved InvalidPart code."""
        for language,backend in self.targets():
            with self.subTest(language=language,backend=backend),fixture_worker(language,[(400,{"Content-Type":"application/json" if backend=="tos" else "application/xml"},TOS_INVALID if backend=="tos" else INVALID)],backend) as (worker,server):
                result=worker.call({"op":"mp_complete","key":"object","upload_id":"upload","parts":[{"part_number":1,"etag":"ffffffffffffffffffffffffffffffff"}]})
                self.assertFalse(result["ok"],result)
                self.assertEqual(result["error_kind"],"sdk_error")
                self.assertEqual(result.get("error_code"),"InvalidPart")
                if "status_code" in result:self.assertEqual(result["status_code"],400)
                self.assertEqual(len(server.requests),1)
                self.assertEqual(server.requests[0][0],"POST")
                self.assertIn(b"ffffffffffffffffffffffffffffffff",server.requests[0][3])
                self.assertEqual(len(server.fixtures),0)

    def test_tos_cpp_rejects_sdk_endpoint_constraints_without_crashing(self):
        """Provide IP, explicit-port or S3 endpoints to real TOS C++; expect clean initialization diagnostics before any HTTP request."""
        if ("cpp","tos") not in self.targets():self.skipTest("requires TOS C++ worker")
        for endpoint in ("http://127.0.0.1", "http://localhost:9999", "https://tos-s3-cn-beijing.volces.com"):
            with self.subTest(endpoint=endpoint),self.assertRaisesRegex(RuntimeError,r"TOS native C\+\+ SDK requires"):
                with fixture_worker("cpp",[],"tos",{"OSV_ENDPOINT":endpoint}):
                    self.fail("invalid endpoint accepted")

    def test_obs_java_rejects_implicit_s3_authentication_fallback(self):
        """Use IP or CNAME native endpoints; expect a diagnostic instead of a mislabeled AWS V2 comparison."""
        if ("java","obs") not in self.targets():self.skipTest("requires OBS Java worker")
        for overrides in ({"OSV_ENDPOINT":"http://127.0.0.1"},{"OSV_CNAME":"true"}):
            with self.subTest(overrides=overrides),self.assertRaisesRegex(RuntimeError,"SDK changed authentication"):
                with fixture_worker("java",[],"obs",overrides):
                    self.fail("implicit S3 authentication fallback accepted")

    def test_aws_whitespace_delimiter_parser_discrepancy_is_observable(self):
        """Supply literal whitespace XML independently of Moto; expect Java preservation and the pinned C++ SDK's empty value."""
        for language,backend in self.targets():
            if backend!="s3":continue
            for delimiter in (" ","\n"):
                page=PAGE.replace(b"<Delimiter>/</Delimiter>",("<Delimiter>"+delimiter+"</Delimiter>").encode())
                with self.subTest(language=language,delimiter=repr(delimiter)),fixture_worker(language,[(200,{"Content-Type":"application/xml"},page)],backend) as (worker,server):
                    response=worker.call({"op":"list_page","prefix":"prefix/","delimiter":delimiter,"max_keys":1})
                    self.assertTrue(response["ok"],response)
                    self.assertEqual(response["delimiter"],delimiter if language=="java" else "")
                    self.assertEqual(parse_qs(urlsplit(server.requests[0][1]).query)["delimiter"],[delimiter])
                    self.assertEqual(len(server.fixtures),0)

    def test_first_last_and_single_byte_ranges_have_exact_request_headers(self):
        """Request first/last/interior single bytes through real SDKs; expect explicit Range including bytes=0-0 and exact response bytes."""
        payload=b"edge";etag='"'+hashlib.md5(payload).hexdigest()+'"'
        vectors=[(0,1),(3,1),(1,1)]
        fixtures=[(206,{"Content-Type":"application/octet-stream","ETag":etag,"Content-Range":f"bytes {offset}-{offset}/{len(payload)}"},payload[offset:offset+1]) for offset,length in vectors]
        for language,backend in self.targets():
            with self.subTest(language=language,backend=backend),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,backend) as (worker,server):
                output=Path(temporary)/"output"
                for index,(offset,length) in enumerate(vectors):
                    result=worker.call({"op":"get","key":"object","offset":offset,"length":length,"output_path":str(output)})
                    self.assertTrue(result["ok"],result)
                    self.assertEqual(output.read_bytes(),payload[offset:offset+1])
                    headers={key.lower():value for key,value in server.requests[index][2].items()}
                    self.assertEqual(headers.get("range"),f"bytes={offset}-{offset}")
                self.assertEqual(len(server.requests),3)

    def test_actual_streams_range_metadata_and_delete(self):
        """Send and consume independent bytes through every real SDK; expect exact body/range, genuine HEAD metadata and DELETE requests."""
        payload=b"independent-real-sdk-stream";etag='"'+hashlib.md5(payload).hexdigest()+'"'
        fixtures=[(200,{"ETag":etag},b""),(200,{"ETag":etag,"Content-Type":"application/octet-stream"},payload),
                  (206,{"ETag":etag,"Content-Range":f"bytes 3-9/{len(payload)}","Content-Type":"application/octet-stream"},payload[3:10]),
                  (200,{"ETag":etag,"Content-Length":str(len(payload)),"Last-Modified":"Tue, 14 Nov 2023 22:13:20 GMT"},b""),(204,{},b"")]
        for language,backend in self.targets():
            with self.subTest(language=language,backend=backend),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,backend) as (worker,server):
                body=Path(temporary)/"body";output=Path(temporary)/"output";body.write_bytes(payload)
                result=worker.call({"op":"put","key":"object","body_path":str(body)})
                self.assertTrue(result["ok"],result)
                result=worker.call({"op":"get","key":"object","output_path":str(output)})
                self.assertTrue(result["ok"],result);self.assertEqual(output.read_bytes(),payload)
                result=worker.call({"op":"get","key":"object","offset":3,"length":7,"output_path":str(output)})
                self.assertTrue(result["ok"],result);self.assertEqual(output.read_bytes(),payload[3:10])
                result=worker.call({"op":"head","key":"object"});self.assertTrue(result["ok"],result)
                self.assertEqual(result["size"],len(payload));self.assertEqual(result["last_modified_ms"],1700000000000)
                self.assertTrue(worker.call({"op":"delete","key":"object"})["ok"])
                self.assertEqual(server.requests[0][3],payload,{key:value for key,value in server.requests[0][2].items() if key.lower() in ("content-length","content-encoding","transfer-encoding","x-amz-decoded-content-length","x-amz-content-sha256")})
                self.assertEqual([row[0] for row in server.requests],["PUT","GET","GET","HEAD","DELETE"])
                self.assertEqual({k.lower():v for k,v in server.requests[2][2].items()}["range"],"bytes=3-9")
                self.assertEqual(len(server.fixtures),0)
