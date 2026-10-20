"""Run translated object-attribute cases and typed SDK models against independent HTTP evidence."""
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from sdk_fixtures import fixture_worker,FixtureHandler
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_checksum_attributes import CASES,ordinary

class ChecksumFixtureHandler(FixtureHandler):
    """Retain actual HTTP/AWS chunk trailers so SDK trailer conversion is measured rather than assumed to use ordinary headers."""
    def respond(self):
        self.server.attribute_header_values=getattr(self.server,"attribute_header_values",[])+[self.headers.get_all("x-amz-object-attributes",[])]
        super().respond()

    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

    def chunks(self,stream):
        pieces=[]
        while True:
            line=stream.readline(4096)
            if not line:raise ValueError("missing chunk size")
            size=int(line.split(b";",1)[0].strip(),16)
            if size==0:
                trailers={}
                while True:
                    line=stream.readline(4096)
                    if line in (b"\r\n",b"\n",b""):break
                    name,value=line.decode("ascii").split(":",1);trailers[name.lower()]=value.strip()
                self.server.checksum_trailers=getattr(self.server,"checksum_trailers",[])+[trailers]
                return b"".join(pieces)
            if size<0 or size>16*1024*1024:raise ValueError("fixture chunk too large")
            data=stream.read(size)
            if len(data)!=size or stream.read(2)!=b"\r\n":raise ValueError("invalid chunk framing")
            pieces.append(data)

@unittest.skipUnless(os.environ.get("OSV_RUN_CHECKSUM_ATTRIBUTE_TESTS")=="1","enable after building actual Java/C++ SDK workers")
class ChecksumAttributesSdkTest(unittest.TestCase):
    """Validate literal requests and native decoded fields without replacing any SDK or production scenario logic."""
    def test_attribute_sdk_models_preserve_nested_parts_and_input_markers(self):
        """Request the original pagination/attribute fields; expect real typed parts, checksum, version and marker values from independent XML/headers."""
        payload=b"<GetObjectAttributesOutput><ETag>independent-etag</ETag><Checksum><ChecksumSHA256>independent-checksum</ChecksumSHA256></Checksum><ObjectParts><PartsCount>6</PartsCount><PartNumberMarker>3</PartNumberMarker><NextPartNumberMarker>4</NextPartNumberMarker><MaxParts>1</MaxParts><IsTruncated>true</IsTruncated><Part><PartNumber>4</PartNumber><Size>5242880</Size><ChecksumSHA256>independent-part</ChecksumSHA256></Part></ObjectParts><StorageClass>STANDARD</StorageClass><ObjectSize>31457280</ObjectSize></GetObjectAttributesOutput>"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"x-amz-version-id":"observed-version","Content-Type":"application/xml"},payload)],"s3",handler=ChecksumFixtureHandler) as (worker,server):
                result=worker.call({"op":"api.GetObjectAttributes","input":{"Bucket":"fixture","Key":"folder/obj","ObjectAttributes":["ETag","Checksum","ObjectParts","StorageClass","ObjectSize"],"MaxParts":1,"PartNumberMarker":3,"VersionId":"requested-version"}})
                self.assertTrue(result["ok"],result);data=result["data"]
                self.assertEqual(data["ETag"],"independent-etag");self.assertEqual(data["Checksum"],{"ChecksumSHA256":"independent-checksum"})
                self.assertEqual(data["VersionId"],"observed-version");self.assertNotIn("DeleteMarker",data)
                self.assertEqual(data["ObjectSize"],31457280);self.assertEqual(data["StorageClass"],"STANDARD")
                self.assertEqual(data["ObjectParts"],{"TotalPartsCount":6,"PartNumberMarker":3,"NextPartNumberMarker":4,"MaxParts":1,"IsTruncated":True,"Parts":[{"PartNumber":4,"Size":5242880,"ChecksumSHA256":"independent-part"}]})
                request=server.requests[0];headers={k.lower():v for k,v in request[2].items()}
                self.assertEqual(request[0],"GET");self.assertEqual({value for line in server.attribute_header_values[0] for value in line.split(",")},{"ETag","Checksum","ObjectParts","StorageClass","ObjectSize"})
                query=parse_qs(urlsplit(request[1]).query,keep_blank_values=True)
                self.assertEqual(query["versionId"],["requested-version"]);self.assertEqual(query["attributes"],["" if language=="java" else "attributes"])
                self.assertEqual(headers["x-amz-max-parts"],"1");self.assertEqual(headers["x-amz-part-number-marker"],"3")

    def test_original_ordinary_case_consumes_real_put_etag_and_preserves_absence(self):
        """Run the real translated ordinary case; expect observed PUT ETag consumed by attributes assertions and actual bucket cleanup after absent optional fields."""
        payload=b"<GetObjectAttributesOutput><ETag>acbd18db4cc2f85cedef654fccc4a4d8</ETag><StorageClass>STANDARD</StorageClass><ObjectSize>3</ObjectSize></GetObjectAttributesOutput>"
        empty=b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>"
        fixtures=[(404,{},b""),(200,{},b""),(200,{"ETag":'"acbd18db4cc2f85cedef654fccc4a4d8"'},b""),(200,{"Content-Type":"application/xml"},payload),(200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                ordinary(Probe(ctx))
                self.assertEqual(len(server.requests),9);self.assertEqual(server.requests[2][3],b"foo")
                self.assertEqual(server.requests[-2][0],"DELETE");self.assertEqual(server.requests[-1][0],"HEAD")

    def test_checksum_typed_members_and_optional_responses(self):
        """Send explicit typed SHA256 through PUT/HEAD/multipart; expect literal checksum headers/XML, true decoded checksum fields and absent ordinary HEAD checksum."""
        checksum="arcu6553sHVAiX4MjW0j7I7vD4w6R+Gz9Ok0Q9lTa+0="
        init=b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>obj</Key><UploadId>upload-real</UploadId><ChecksumAlgorithm>SHA256</ChecksumAlgorithm></InitiateMultipartUploadResult>"
        completed=b"<CompleteMultipartUploadResult><ETag>observed-multipart-etag</ETag><ChecksumSHA256>observed-composite=-1</ChecksumSHA256></CompleteMultipartUploadResult>"
        fixtures=[(200,{"ETag":'"put-etag"',"x-amz-checksum-sha256":checksum},b""),(200,{},b""),(200,{"x-amz-checksum-sha256":checksum},b""),(200,{"Content-Type":"application/xml","x-amz-checksum-algorithm":"SHA256"},init),(200,{"ETag":'"part-etag"',"x-amz-checksum-sha256":"observed-part="},b""),(200,{"Content-Type":"application/xml"},completed)]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",handler=ChecksumFixtureHandler) as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(b"AAA")
                put=worker.call({"op":"put_http","key":"obj","body_path":str(body),"checksum":{"ChecksumAlgorithm":"SHA256","ChecksumSHA256":checksum}})
                self.assertTrue(put["ok"],put);self.assertEqual(put["checksum"],{"ChecksumSHA256":checksum})
                plain=worker.call({"op":"head_http","key":"obj"});self.assertTrue(plain["ok"],plain);self.assertEqual(plain["checksum"],{})
                head=worker.call({"op":"head_http","key":"obj","checksum_mode":"ENABLED"});self.assertTrue(head["ok"],head);self.assertEqual(head["checksum"],{"ChecksumSHA256":checksum})
                created=worker.call({"op":"mp_create_metadata","key":"obj","checksum":{"ChecksumAlgorithm":"SHA256"}})
                self.assertTrue(created["ok"],created);self.assertEqual(created["checksum"],{"ChecksumAlgorithm":"SHA256"})
                part=worker.call({"op":"mp_part","key":"obj","upload_id":created["upload_id"],"part_number":1,"body_path":str(body),"checksum":{"ChecksumAlgorithm":"SHA256","ChecksumSHA256":checksum}})
                self.assertTrue(part["ok"],part);self.assertEqual(part["checksum"],{"ChecksumSHA256":"observed-part="})
                result=worker.call({"op":"mp_complete_observed","key":"obj","upload_id":created["upload_id"],"parts":[{"part_number":1,"etag":part["etag"],"ChecksumSHA256":part["checksum"]["ChecksumSHA256"]}],"checksum":{"ChecksumSHA256":"request-composite=-1"}})
                self.assertTrue(result["ok"],result);self.assertEqual(result["checksum"],{"ChecksumSHA256":"observed-composite=-1"})
                self.assertEqual(len(server.requests),6)
                headers=[{k.lower():v for k,v in request[2].items()} for request in server.requests]
                wire_checksums=[h["x-amz-checksum-sha256"] for h in (headers[0],headers[4]) if "x-amz-checksum-sha256" in h]
                wire_checksums += [trailer["x-amz-checksum-sha256"] for trailer in getattr(server,"checksum_trailers",[]) if "x-amz-checksum-sha256" in trailer]
                self.assertEqual(wire_checksums,[checksum,checksum])
                self.assertEqual(headers[0]["x-amz-sdk-checksum-algorithm"],"SHA256")
                self.assertEqual(server.requests[0][3],b"AAA");self.assertEqual(server.requests[4][3],b"AAA")
                self.assertNotIn("x-amz-checksum-mode",headers[1]);self.assertEqual(headers[2]["x-amz-checksum-mode"],"ENABLED")
                self.assertEqual(headers[3]["x-amz-checksum-algorithm"],"SHA256")
                self.assertEqual(headers[5]["x-amz-checksum-sha256"],"request-composite=-1")
                from xml.etree import ElementTree as ET
                document=ET.fromstring(server.requests[5][3]);part_node=document.find("{*}Part")
                self.assertEqual(part_node.find("{*}ETag").text,part["etag"])
                self.assertEqual(part_node.find("{*}ChecksumSHA256").text,"observed-part=")
                self.assertEqual(part_node.find("{*}PartNumber").text,"1")

    def test_invalid_or_unsupported_checksum_input_fails_before_http(self):
        """Pass malformed maps/types/unknown fields and truly absent typed models; expect explicit SDK errors, zero HTTP and accurate capability flags."""
        invalid=(["SHA256"],{"UnknownChecksum":"x"},{"ChecksumSHA256":None},{"ChecksumSHA256":23},{"ChecksumSHA256":False})
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                body=Path(temporary)/"body";body.write_bytes(b"AAA")
                for checksum in invalid:
                    result=worker.call({"op":"put_http","key":"obj","body_path":str(body),"checksum":checksum})
                    self.assertFalse(result["ok"],result)
                caps=worker.info["capabilities"]
                for name in ("sha256","sha1","crc32","crc32c"):self.assertIn("checksum_"+name,caps)
                if language=="java":
                    self.assertNotIn("checksum_type",caps);self.assertNotIn("checksum_crc64nvme",caps)
                    for checksum in ({"ChecksumType":"COMPOSITE"},{"ChecksumCRC64NVME":"x"}):
                        result=worker.call({"op":"mp_create_metadata","key":"obj","checksum":checksum});self.assertFalse(result["ok"],result)
                self.assertEqual(server.requests,[])

    def test_original_checksum_and_attribute_cases_keep_local_diagnostics_and_cleanup(self):
        """Execute all original translated cases through genuine SDK workers; expect unsupported typed fields to be not_run, local service differences retained and only the default bucket surviving."""
        from test_s3_sdk import SdkIntegrationTest
        SdkIntegrationTest.setUpClass.__func__(type(self))
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary)
                worker=Worker(target,30)
                try:
                    ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                    results=run_cases(ctx,CASES)
                    self.assertEqual(len(results),17)
                    expected_not_run={"ceph_checksum_object_crc64nvme"}|{"ceph_checksum_helper_"+name for name in ("sha256","crc64nvme","crc32","crc32c","sha1")} if target["language"]=="java" else set()
                    expected_pass=set() if target["language"]=="java" else {"ceph_attributes_object","ceph_attributes_versioned","ceph_checksum_attributes_sha256"}
                    all_names={item.name for item in CASES}
                    self.assertEqual({row["case"] for row in results if row["status"]=="not_run"},expected_not_run,results)
                    self.assertEqual({row["case"] for row in results if row["status"]=="pass"},expected_pass,results)
                    self.assertEqual({row["case"] for row in results if row["status"]=="fail"},all_names-expected_not_run-expected_pass,results)
                    for row in results:
                        if row["status"]=="not_run":self.assertEqual(row["reason"],"unsupported_operations")
                        if row["status"]=="fail":self.assertTrue(row["error"])
                finally:worker.close()
                self.assertEqual([row["Name"] for row in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])

    def test_empty_checksum_response_header_is_distinct_from_absence(self):
        """Receive empty then absent checksum headers; expect native SDK empty-field behavior retained (Java null, C++ empty string), raw header evidence and no stale response state."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,[(200,{"x-amz-checksum-sha256":""},b""),(200,{},b"")],"s3") as (worker,server):
                first=worker.call({"op":"head_http","key":"obj","checksum_mode":"ENABLED"})
                self.assertTrue(first["ok"],first);self.assertEqual(first["http_headers"]["x-amz-checksum-sha256"],"")
                self.assertEqual(first["checksum"],{} if language=="java" else {"ChecksumSHA256":""})
                second=worker.call({"op":"head_http","key":"obj"})
                self.assertTrue(second["ok"],second);self.assertEqual(second["checksum"],{})
                self.assertEqual(len(server.requests),2)
