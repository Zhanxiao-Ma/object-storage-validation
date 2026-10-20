"""Optional real SDK regressions for Ceph conditional version mutation matrices."""
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from osv.ceph_conditional_versions import CASES,date_header,delete,delete_attribute,delete_many_attribute,write
from osv.bucket_scope import BucketScope
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[2]


class ConditionalVersionTimeTest(unittest.TestCase):
    """Protect UTC HTTP-date conversion and zero/negative boundary semantics with independent literal expectations."""
    def test_utc_seconds_and_epoch_boundaries(self):
        """Preserve zero and pre-epoch milliseconds, truncate fractional seconds, and serialize the source's 2015 bad date in UTC."""
        self.assertEqual(date_header(0),"Thu, 01 Jan 1970 00:00:00 GMT")
        self.assertEqual(date_header(999),"Thu, 01 Jan 1970 00:00:00 GMT")
        self.assertEqual(date_header(1000),"Thu, 01 Jan 1970 00:00:01 GMT")
        self.assertEqual(date_header(-1),"Wed, 31 Dec 1969 23:59:59 GMT")
        self.assertEqual(date_header(1420070400000),"Thu, 01 Jan 2015 00:00:00 GMT")


@unittest.skipUnless(os.environ.get("OSV_RUN_CONDITIONAL_VERSIONS_TESTS")=="1","enable after building real Java/C++ SDK workers")
class ConditionalVersionsWireTest(unittest.TestCase):
    """Inspect genuine signed requests and typed SDK response producers using independent HTTP documents."""
    def test_actual_put_etag_conditions_zero_size_and_error_status(self):
        """Feed an actual SDK PUT ETag into another PUT, retain exact 412/204 diagnostics, and send zero size/time plus explicit version through official signers."""
        from sdk_fixtures import fixture_worker
        failure=b'<Error><Code>PreconditionFailed</Code><Message>independent rejection</Message></Error>'
        fixtures=[(200,{"ETag":'"8b04d5e3775d298e78455efc5ca404d5"',"x-amz-version-id":"actual-put-version"},b""),
            (412,{"Content-Type":"application/xml"},failure),(200,{"ETag":'"a9f0e61a137d86aa9db53465e0801612"'},b""),(204,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}));bucket=BucketScope(probe)
                first=write(bucket,probe,b"first",none="*");self.assertTrue(first["ok"],first)
                self.assertEqual(first["version_id"],"actual-put-version")
                bad=write(bucket,probe,match=first["etag"]);self.assertEqual((bad.get("status_code"),bad.get("error_code")),(412,"PreconditionFailed"))
                good=write(bucket,probe,b"second",match=first["etag"]);self.assertTrue(good["ok"],good)
                removed=delete(bucket,match=good["etag"],mtime=0,size=0,version=first["version_id"]);self.assertTrue(removed["ok"],removed)
                self.assertEqual(removed["status_code"],204);self.assertEqual(len(server.requests),4)
                headers=[{name.lower():value for name,value in request[2].items()} for request in server.requests]
                self.assertEqual(headers[0]["if-none-match"],"*")
                for index in (1,2):self.assertEqual(headers[index]["if-match"],'"8b04d5e3775d298e78455efc5ca404d5"')
                self.assertEqual(headers[3]["if-match"],'"a9f0e61a137d86aa9db53465e0801612"')
                self.assertEqual(headers[3]["x-amz-if-match-size"],"0")
                self.assertEqual(headers[3]["x-amz-if-match-last-modified-time"],"Thu, 01 Jan 1970 00:00:00 GMT")
                self.assertEqual(parse_qs(urlsplit(server.requests[3][1]).query)["versionId"],["actual-put-version"])
                self.assertEqual([request[3] for request in server.requests[:3]],[b"first",b"",b"second"])
                for observed in headers:self.assertIn("if-",observed["authorization"])

    def test_multipart_conditions_preserve_actual_part_etag_and_abort(self):
        """Drive actual multipart success/failure through the production helper; completion consumes SDK part ETags and both upload IDs receive authoritative abort cleanup."""
        from sdk_fixtures import fixture_worker
        xml={"Content-Type":"application/xml"}
        completed_etag='"af5da9f45af7a300e3aded972f8ff687-1"'
        part_etag='"900150983cd24fb0d6963f7d28e17f72"'
        fixtures=[(200,xml,b'<InitiateMultipartUploadResult><UploadId>actual-upload+one</UploadId></InitiateMultipartUploadResult>'),
            (200,{"ETag":part_etag},b""),(200,{**xml,"x-amz-version-id":"actual-completed-version"},b'<CompleteMultipartUploadResult><ETag>"af5da9f45af7a300e3aded972f8ff687-1"</ETag></CompleteMultipartUploadResult>'),
            (404,xml,b'<Error><Code>NoSuchUpload</Code></Error>'),
            (200,xml,b'<InitiateMultipartUploadResult><UploadId>actual-upload+two</UploadId></InitiateMultipartUploadResult>'),
            (200,{"ETag":part_etag},b""),(412,xml,b'<Error><Code>PreconditionFailed</Code></Error>'),(204,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}));bucket=BucketScope(probe)
                completed=write(bucket,probe,multipart=True,none="*");self.assertTrue(completed["ok"],completed)
                self.assertEqual(completed["etag"],completed_etag);self.assertEqual(completed["version_id"],"actual-completed-version")
                failed=write(bucket,probe,multipart=True,match=completed["etag"])
                self.assertEqual((failed.get("status_code"),failed.get("error_code")),(412,"PreconditionFailed"))
                self.assertEqual(len(server.requests),8)
                for index,identifier in ((1,"actual-upload+one"),(2,"actual-upload+one"),(3,"actual-upload+one"),(5,"actual-upload+two"),(6,"actual-upload+two"),(7,"actual-upload+two")):
                    self.assertEqual(parse_qs(urlsplit(server.requests[index][1]).query)["uploadId"],[identifier])
                for index,header,value in ((2,"if-none-match","*"),(6,"if-match",completed_etag)):
                    headers={name.lower():observed for name,observed in server.requests[index][2].items()}
                    self.assertEqual(headers[header],value);self.assertIn(header,headers["authorization"])
                    document=ET.fromstring(server.requests[index][3])
                    self.assertEqual(document.find(".//{*}ETag").text,part_etag.strip('"'))
                    self.assertEqual(document.find(".//{*}PartNumber").text,"1")
                self.assertEqual(server.requests[1][3],b"abc");self.assertEqual(server.requests[5][3],b"abc")
                self.assertEqual(server.requests[3][0],"DELETE");self.assertEqual(server.requests[7][0],"DELETE")
                for index in (0,1,3,4,5,7):
                    headers={name.lower():value for name,value in server.requests[index][2].items()}
                    self.assertNotIn("if-match",headers);self.assertNotIn("if-none-match",headers)

    def test_historical_last_modified_time_consumes_version_head(self):
        """Execute the real version-mtime source case; actual version ID and HEAD date must reach both negative and positive conditional deletes, followed by cleanup."""
        from sdk_fixtures import fixture_worker
        failure=b'<Error><Code>PreconditionFailed</Code><Message>independent rejection</Message></Error>'
        xml={"Content-Type":"application/xml"}
        empty=b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,xml,b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>'),
            (200,{"ETag":'"d41d8cd98f00b204e9800998ecf8427e"',"x-amz-version-id":"actual-version+source"},b""),
            (200,{"Last-Modified":"Fri, 02 Jan 2015 00:00:00 GMT","x-amz-version-id":"actual-version+source"},b""),
            (412,xml,failure),(204,{},b""),(204,{},b""),(200,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary)
                delete_attribute(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})),"mtime","version")
                self.assertEqual(len(server.requests),13)
                for index,expected in ((6,"Thu, 01 Jan 2015 00:00:00 GMT"),(7,"Fri, 02 Jan 2015 00:00:00 GMT"),(8,"Thu, 01 Jan 2015 00:00:00 GMT")):
                    request=server.requests[index];headers={name.lower():value for name,value in request[2].items()}
                    self.assertEqual(headers["x-amz-if-match-last-modified-time"],expected)
                    self.assertEqual(parse_qs(urlsplit(request[1]).query)["versionId"],["actual-version+source"])
                self.assertEqual(server.requests[5][0],"HEAD")
                self.assertEqual(parse_qs(urlsplit(server.requests[5][1]).query)["versionId"],["actual-version+source"])


    def test_invalid_bulk_condition_fields_never_reach_http(self):
        """Reject null, wrong JSON types, floating values, signed-integer overflow and misspelled condition names before HTTP so requested conditions cannot disappear silently."""
        from sdk_fixtures import fixture_worker
        invalid=[{"etag":value} for value in (None,True,0,[],{})]
        invalid+=[{name:value} for name in ("last_modified_ms","size") for value in (None,True,0.5,2**63,-2**63-1,"")]
        invalid+=[{"ETag":"must-not-disappear"},{"LastModifiedTime":1420070400000},{"Size":0}]
        invalid_objects=[None,{},"objects",1,True,[None],[""],[1],[True],[[]],[{}]]
        invalid_objects+=[[{"key":value}] for value in (None,True,1,[],{})]
        invalid_objects+=[[{"key":"obj","version_id":value}] for value in (None,True,0,[],{})]
        invalid_objects+=[[{"key":"first-valid"},{"key":False,"size":0}]]
        requests=[[{"key":"obj",**value}] for value in invalid]+invalid_objects
        fixtures=[(200,{"Content-Type":"application/xml"},b'<DeleteResult><Deleted><Key>obj</Key></Deleted></DeleteResult>')]*len(requests)
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),fixture_worker(language,fixtures,"s3") as (worker,server):
                for value in requests:
                    with self.subTest(value=value):
                        observed=worker.call({"op":"delete_many_versions","objects":value})
                        self.assertFalse(observed["ok"],observed)
                self.assertEqual(server.requests,[])

    def test_bulk_etag_and_zero_size_are_typed_xml_fields(self):
        """Feed the SDK PUT's actual ETag into typed batch XML and retain explicit zero size/epoch time; independent response documents must decode into real Deleted/Errors rows."""
        from sdk_fixtures import fixture_worker
        from osv.ceph_conditional_versions import delete_many
        xml={"Content-Type":"application/xml"}
        fixtures=[(200,{"ETag":'"d41d8cd98f00b204e9800998ecf8427e"'},b""),
            (200,xml,b'<DeleteResult><Deleted><Key>obj</Key></Deleted></DeleteResult>'),
            (200,xml,b'<DeleteResult><Deleted><Key>obj</Key></Deleted></DeleteResult>'),
            (200,xml,b'<DeleteResult><Deleted><Key>obj</Key></Deleted></DeleteResult>'),
            (200,xml,b'<DeleteResult><Error><Key>obj</Key><Code>PreconditionFailed</Code></Error></DeleteResult>')]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                if "delete_many_conditions" not in worker.capabilities:
                    self.assertEqual(language,"java");self.assertEqual(server.requests,[]);continue
                directory=Path(temporary);probe=Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}));bucket=BucketScope(probe)
                source=write(bucket,probe);self.assertTrue(source["ok"],source)
                self.assertEqual(delete_many(bucket,"match",source["etag"])["deleted"][0]["key"],"obj")
                self.assertEqual(delete_many(bucket,"size",0)["deleted"][0]["key"],"obj")
                self.assertEqual(delete_many(bucket,"mtime",0)["deleted"][0]["key"],"obj")
                self.assertEqual(delete_many(bucket,"size",9999)["errors"][0]["code"],"PreconditionFailed")
                self.assertEqual(len(server.requests),5)
                self.assertEqual(ET.fromstring(server.requests[1][3]).find(".//{*}Object/{*}ETag").text,'"d41d8cd98f00b204e9800998ecf8427e"')
                self.assertEqual(ET.fromstring(server.requests[2][3]).find(".//{*}Object/{*}Size").text,"0")
                self.assertEqual(ET.fromstring(server.requests[3][3]).find(".//{*}Object/{*}LastModifiedTime").text,"Thu, 01 Jan 1970 00:00:00 GMT")
                self.assertEqual(ET.fromstring(server.requests[4][3]).find(".//{*}Object/{*}Size").text,"9999")

    def test_bulk_historical_condition_uses_actual_version_and_time(self):
        """Run the real historical bulk-mtime case through supported typed SDK models; the original HEAD date and historical ID must survive a newer current PUT and cleanup."""
        from sdk_fixtures import fixture_worker
        xml={"Content-Type":"application/xml"}
        empty=b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>'
        remaining=b'<ListVersionsResult><IsTruncated>false</IsTruncated><Version><Key>obj</Key><VersionId>new-current+id</VersionId></Version></ListVersionsResult>'
        error=b'<DeleteResult><Error><Key>obj</Key><Code>PreconditionFailed</Code></Error></DeleteResult>'
        success=b'<DeleteResult><Deleted><Key>obj</Key><VersionId>historical+id</VersionId></Deleted></DeleteResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,xml,b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>'),
            (200,{"ETag":'"d41d8cd98f00b204e9800998ecf8427e"',"x-amz-version-id":"historical+id"},b""),
            (200,{"Last-Modified":"Mon, 02 Jan 2017 00:00:00 GMT","x-amz-version-id":"historical+id"},b""),
            (200,{"ETag":'"d41d8cd98f00b204e9800998ecf8427e"',"x-amz-version-id":"new-current+id"},b""),
            (200,xml,error),(200,xml,success),(200,xml,success),(200,{},b""),(200,xml,remaining),(204,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                if "delete_many_conditions" not in worker.capabilities:
                    self.assertEqual(language,"java");self.assertEqual(server.requests,[]);continue
                directory=Path(temporary)
                delete_many_attribute(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})),"mtime","version")
                self.assertEqual(len(server.requests),16)
                for index,expected in ((7,"Thu, 01 Jan 2015 00:00:00 GMT"),(8,"Mon, 02 Jan 2017 00:00:00 GMT"),(9,"Thu, 01 Jan 2015 00:00:00 GMT")):
                    document=ET.fromstring(server.requests[index][3])
                    self.assertEqual(document.find(".//{*}Object/{*}Key").text,"obj")
                    self.assertEqual(document.find(".//{*}Object/{*}VersionId").text,"historical+id")
                    timestamp=document.find(".//{*}Object/{*}LastModifiedTime").text
                    self.assertEqual(timestamp,expected)
                self.assertEqual(server.requests[6][0],"PUT")
                self.assertEqual(parse_qs(urlsplit(server.requests[12][1]).query)["versionId"],["new-current+id"])

@unittest.skipUnless(os.environ.get("OSV_RUN_CONDITIONAL_VERSIONS_TESTS")=="1","enable after building real SDK workers")
class ConditionalVersionsSdkTest(unittest.TestCase):
    """Keep actual local service incompatibilities visible while exercising production SDKs and confirming all owned resources are cleaned."""
    @classmethod
    def setUpClass(cls):
        """Use a serial local Moto service with fixed dummy credentials; cloud certification and timing are outside this regression."""
        import boto3
        from local_moto import LocalMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server=LocalMotoServer();cls.server.start();cls.addClassCleanup(cls.server.stop)
        host,port=cls.server.get_host_and_port()
        cls.environment=patch.dict(os.environ,{"OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"",
            "OSV_ENDPOINT":f"http://{host}:{port}","AWS_EC2_METADATA_DISABLED":"true","OSV_REGION":"us-east-1","OSV_BUCKET":"osv-conditional-versions",
            "OSV_PATH_STYLE":"true","OSV_RETRIES":"0","OSV_ALT_ACCESS_KEY":"","OSV_ALT_SECRET_KEY":"","OSV_TENANT_ACCESS_KEY":"","OSV_TENANT_SECRET_KEY":""})
        cls.environment.start();cls.addClassCleanup(cls.environment.stop)
        cls.client=boto3.client("s3",endpoint_url=f"http://{host}:{port}",region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-conditional-versions")
        cls.commands={"java":["java","-Xmx256m","-jar",str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"),"s3"],"cpp":[str(ROOT/"drivers/cpp/build/osv-cpp-s3")]}

    def test_source_condition_matrices_keep_actual_outcomes_and_cleanup(self):
        """Run all 25 original source matrices; retain genuine unsupported per-object SDK conditions and exact local diagnostics with no remaining test buckets or uploads."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},30)
                try:
                    directory=Path(temporary)
                    rows=run_cases(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}),CASES)
                    self.assertEqual(len(rows),25)
                    missing={item.name for item in CASES if "delete_many_conditions" in item.operations} if language=="java" else set()
                    self.assertEqual({row["case"] for row in rows if row["status"]=="not_run"},missing,rows)
                    self.assertEqual({row["case"] for row in rows if row["status"]=="fail"},{item.name for item in CASES}-missing,rows)
                    self.assertFalse(any(row["status"]=="pass" for row in rows))
                    for row in rows:
                        if row["status"]=="not_run":self.assertEqual(row["missing_operations"],["delete_many_conditions"])
                        else:self.assertNotEqual(row.get("error"),"'last_modified_ms'",row)
                    self.assertEqual({item["Name"] for item in self.client.list_buckets()["Buckets"]},{"osv-conditional-versions"})
                finally:worker.close()
