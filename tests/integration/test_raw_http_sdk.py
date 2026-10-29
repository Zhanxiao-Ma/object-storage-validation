"""Optional original Ceph raw-HTTP protocol cases with genuine Java/C++ SDK ownership."""
import base64
from email.parser import BytesParser
from email.policy import default
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from osv.ceph_raw_http import CASES,SourceHttp,normalized_endpoint,post_vector,run_raw_cases
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[2]


class RawHttpEndpointTest(unittest.TestCase):
    """Protect explicit endpoint binding before any raw HTTP request can be sent."""
    def test_normalization_keeps_identity_and_rejects_ambiguous_urls(self):
        """Treat default ports/host case as equivalent, retain path identity, and reject embedded credentials or request query/fragment."""
        self.assertEqual(normalized_endpoint("https://EXAMPLE.test:443/"),normalized_endpoint("https://example.test"))
        self.assertEqual(normalized_endpoint("http://example.test:80/prefix/"),normalized_endpoint("http://EXAMPLE.test/prefix"))
        self.assertNotEqual(normalized_endpoint("http://example.test/prefix"),normalized_endpoint("https://example.test/prefix"))
        for endpoint in ("ftp://example.test","http://a:b@example.test","http://example.test/?secret=x","http://example.test/#fragment","http://example.test:0","http://example.test:65536","not-a-url"):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):normalized_endpoint(endpoint)


@unittest.skipUnless(os.environ.get("OSV_RUN_RAW_HTTP_TESTS")=="1","enable after building actual SDK workers and installing requests")
class RawHttpWireTest(unittest.TestCase):
    """Inspect source multipart bytes and genuine SDK readback without emulating object storage or signatures."""
    def test_original_multipart_order_filenames_hmac_and_sdk_readback(self):
        """Run the real authenticated source case; require scalar fields without filenames and only file with a filename, exact JSON policy bytes, HMAC-SHA1 and SDK GET producer bytes with cleanup."""
        from sdk_fixtures import fixture_worker
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(204,{},b""),(200,{"Content-Type":"text/plain","ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b"bar"),
            (200,{},b""),(204,{},b""),(200,{"Content-Type":"application/xml"},empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);workload={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"}}
                evidence=post_vector(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)),"authenticated")
                self.assertEqual(evidence["request_transport"],"raw_http");self.assertFalse(evidence["sdk_performance_sample"])
                self.assertEqual(len(server.requests),9)
                post=server.requests[2];self.assertEqual(post[0],"POST")
                headers={key.lower():value for key,value in post[2].items()}
                self.assertNotIn("authorization",headers)
                document=BytesParser(policy=default).parsebytes(("Content-Type: "+headers["content-type"]+"\r\nMIME-Version: 1.0\r\n\r\n").encode()+post[3])
                parts=list(document.iter_parts());names=[part.get_param("name",header="content-disposition") for part in parts]
                self.assertEqual(names,["key","AWSAccessKeyId","acl","signature","policy","Content-Type","file"])
                self.assertEqual([part.get_filename() for part in parts],[None]*6+["file"])
                fields={name:part.get_payload(decode=True) for name,part in zip(names,parts)}
                self.assertEqual(fields["key"],b"foo.txt");self.assertEqual(fields["file"],b"bar")
                self.assertEqual(fields["AWSAccessKeyId"],b"local-test");self.assertEqual(fields["acl"],b"private")
                self.assertEqual(fields["signature"],base64.b64encode(hmac.new(b"local-test",fields["policy"],hashlib.sha1).digest()))
                policy=json.loads(base64.b64decode(fields["policy"]))
                bucket=post[1].strip("/")
                self.assertEqual(policy["conditions"],[{"bucket":bucket},["starts-with","$key","foo"],{"acl":"private"},["starts-with","$Content-Type","text/plain"],["content-length-range",0,1024]])
                self.assertEqual(server.requests[3][0],"GET");self.assertTrue(server.requests[3][1].endswith("/foo.txt"))
                self.assertIn("authorization",{name.lower():value for name,value in server.requests[3][2].items()})

    def test_all_post_vectors_consume_independent_http_and_sdk_outputs(self):
        """Exercise every original POST success/error branch against explicit independent responses, checking source field mutations and real SDK body/metadata/tag readbacks without a storage emulator."""
        from sdk_fixtures import fixture_worker
        import uuid
        from osv.ceph_raw_http import POSTS
        # Source status expectations are independent literals; no production status selector is reused.
        expected={"anonymous":204,"authenticated":204,"no_content_type":204,"bad_access_key":403,"success":201,"invalid_success":204,
            "large":204,"filename":204,"ignored":204,"case_insensitive":204,"escaped":204,"redirect":200,"invalid_signature":403,"invalid_access_key":403,
            "invalid_date":400,"no_key":400,"missing_signature":400,"missing_bucket_condition":403,"metadata":204,"missing_metadata":403,
            "conditions_case":400,"expiration_case":400,"expired":403,"wrong_bucket":403,"invalid_metadata":403,"no_expiration":400,"no_conditions":400,
            "above_maximum":400,"missing_length":400,"negative_length":400,"below_minimum":400,"chunk_bug":204,"empty_conditions":400,"tags_anonymous":204,"tags_authenticated":204,"checksum":204}
        reads={"anonymous","authenticated","no_content_type","large","filename","escaped","redirect","metadata","tags_anonymous","tags_authenticated","checksum"}
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        xml={"Content-Type":"application/xml"};fixtures=[];identifiers=[];planned=[];next_id=1
        for mode,_ in POSTS:
            if mode=="case_insensitive":continue  # Nonstandard policy operators are gated before HTTP.
            identifier=uuid.UUID(int=next_id);next_id+=1;identifiers.append(identifier);name="osv-"+identifier.hex
            fixtures.extend([(404,{},b""),(200,{},b"")])
            if mode=="wrong_bucket":identifiers.append(uuid.UUID(int=next_id));next_id+=1;fixtures.extend([(404,{},b""),(200,{},b"")])
            if mode=="redirect":
                location="/"+name+"?bucket="+name+"&key=foo.txt&etag=%2237b51d194a7513e45b56f6524f2d51f2%22"
                fixtures.extend([(303,{"Location":location},b""),(200,{},b"")])
            else:fixtures.append((expected[mode],xml if mode=="success" else {},b'<PostResponse><Key>foo.txt</Key></PostResponse>' if mode=="success" else b""))
            if mode in reads:
                content=b"x"*(2*1024*1024) if mode=="checksum" else b"foo"*(1024*1024) if mode=="large" else b"bar"
                headers={"Content-Type":"text/plain","ETag":'"'+hashlib.md5(content).hexdigest()+'"'}
                if mode=="metadata":headers["x-amz-meta-foo"]="barclamp"
                fixtures.append((200,headers,content))
            if mode=="tags_anonymous":fixtures.append((200,xml,b'<Tagging><TagSet><Tag><Key>0</Key><Value>0</Value></Tag><Tag><Key>1</Key><Value>1</Value></Tag></TagSet></Tagging>'))
            if mode=="checksum":fixtures.append((400,{},b""))
            fixtures.extend([(200,{},b""),(204,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")])
            if mode=="wrong_bucket":fixtures.extend([(200,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")])
            planned.append((mode,name))
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary);workload={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"}}
                with patch("osv.bucket_scope.uuid.uuid4",side_effect=identifiers):
                    for mode,name in planned:
                        with self.subTest(mode=mode):
                            start=len(server.requests)
                            result=post_vector(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)),mode)
                            self.assertFalse(result["sdk_performance_sample"])
                            requests=[request for request in server.requests[start:] if request[0]=="POST"]
                            self.assertEqual(len(requests),2 if mode=="checksum" else 1)
                            request=requests[0];headers={key.lower():value for key,value in request[2].items()}
                            document=BytesParser(policy=default).parsebytes(("Content-Type: "+headers["content-type"]+"\r\n\r\n").encode()+request[3])
                            parts=list(document.iter_parts());fields={part.get_param("name",header="content-disposition"):part.get_payload(decode=True) for part in parts}
                            policy_name="pOLICy" if mode=="case_insensitive" else "policy"
                            policy=json.loads(base64.b64decode(fields[policy_name])) if policy_name in fields else None
                            if mode=="no_key":self.assertNotIn("key",fields)
                            if mode=="missing_signature":self.assertNotIn("signature",fields)
                            if mode=="no_content_type":self.assertNotIn("Content-Type",fields)
                            if mode=="conditions_case":self.assertIn("CONDITIONS",policy);self.assertNotIn("conditions",policy)
                            if mode=="expiration_case":self.assertIn("EXPIRATION",policy);self.assertNotIn("expiration",policy)
                            if mode=="no_expiration":self.assertNotIn("expiration",policy)
                            if mode=="no_conditions":self.assertNotIn("conditions",policy)
                            if mode=="empty_conditions":self.assertEqual(policy["conditions"],[{}])
                            if mode in {"missing_length","negative_length","below_minimum","above_maximum","chunk_bug"}:
                                length=next(condition for condition in policy["conditions"] if isinstance(condition,list) and condition[0]=="content-length-range")
                                self.assertEqual(length,{"missing_length":["content-length-range",0],"negative_length":["content-length-range",-1,0],"below_minimum":["content-length-range",512,1000],"above_maximum":["content-length-range",0,0],"chunk_bug":["content-length-range",4194304,12582912]}[mode])
                            if mode in {"filename","wrong_bucket"}:self.assertEqual(next(part.get_filename() for part in parts if part.get_param("name",header="content-disposition")=="file"),"foo.txt")
                            if mode=="metadata":self.assertEqual(fields["x-amz-meta-foo"],b"barclamp")
                            if mode=="ignored":self.assertEqual(fields["x-ignore-foo"],b"bar")
                            if mode=="bad_access_key":self.assertEqual(fields["AWSAccessKeyId"],b"foo")
                            if mode=="invalid_access_key":self.assertEqual(fields["AWSAccessKeyId"],b"tset-lacol")
                self.assertEqual(len(server.fixtures),0)

    def test_complete_original_cors_matrix_consumes_independent_http_headers(self):
        """Exercise every original CORS GET/PUT/OPTIONS branch using explicit independent response headers/statuses and real SDK CORS configuration/cleanup."""
        from sdk_fixtures import fixture_worker
        from osv.ceph_raw_http import cors
        from sdk_fixtures import FixtureHandler
        class OptionsHandler(FixtureHandler):
            """Replay explicit OPTIONS documents as well as ordinary SDK HTTP methods; no CORS service behavior is implemented."""
            do_OPTIONS=FixtureHandler.respond
        origin_responses=[(200,None,None),(200,"foo.suffix","GET"),(200,None,None),(200,None,None),
            (200,"startend","GET"),(200,"start1end","GET"),(200,"start12end","GET"),(200,None,None),
            (200,"prefix","GET"),(200,"prefix.suffix","GET"),(200,None,None),(404,"foo.suffix","GET"),
            (403,"foo.suffix","GET"),(403,None,None),(403,None,None),(403,None,None),(403,"foo.put","PUT"),(404,"foo.suffix","GET"),
            (400,None,None),(400,None,None),(400,None,None),(200,"foo.suffix","GET"),(403,None,None),(403,None,None),
            (200,"startend","GET"),(200,"start1end","GET"),(200,"start12end","GET"),(403,None,None),(200,"prefix","GET"),
            (200,"prefix.suffix","GET"),(403,None,None),(403,None,None),(200,"foo.put","PUT")]
        xml={"Content-Type":"application/xml"};empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[]
        for values in (origin_responses,[(200,None,None),(200,"*","GET")],[(403,None,None)]):
            fixtures.extend([(404,{},b""),(200,{},b""),(404,xml,b'<Error><Code>NoSuchCORSConfiguration</Code></Error>'),(200,{},b"")])
            for status,origin,method in values:
                headers={}
                if origin is not None:headers["Access-Control-Allow-Origin"]=origin
                if method is not None:headers["Access-Control-Allow-Methods"]=method
                fixtures.append((status,headers,b""))
            fixtures.extend([(200,{},b""),(204,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")])
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",handler=OptionsHandler) as (worker,server):
                directory=Path(temporary);workload={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"}}
                for mode,total in (("origins",33),("wildcard",2),("header",1)):
                    start=len(server.requests)
                    evidence=cors(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)),mode)
                    self.assertFalse(evidence["sdk_performance_sample"])
                    raw=server.requests[start+4:start+4+total];self.assertEqual(len(raw),total)
                    if mode=="origins":
                        self.assertEqual([item[0] for item in raw],["GET"]*12+["PUT"]*5+["GET"]+["OPTIONS"]*15)
                        self.assertEqual({key.lower():value for key,value in raw[12][2].items()}["access-control-request-method"],"GET")
                    self.assertEqual(len(server.requests)-start,total+9)
                    self.assertTrue(all("authorization" not in {key.lower() for key in item[2]} for item in raw))
                self.assertEqual(len(server.fixtures),0)

    def test_raw_interim_socket_status_and_sdk_acl_transition(self):
        """Replay independent 403/100 interim statuses without consuming a request body; preserve the declared 123 bytes, anonymous socket headers and actual SDK ACL transition/cleanup."""
        from sdk_fixtures import fixture_worker,FixtureHandler
        from osv.ceph_raw_http import continue_interim
        from collections import deque
        class InterimHandler(FixtureHandler):
            """Return explicit interim protocol documents without a storage/auth model or body emulation."""
            def handle_expect_100(self):
                if self.headers.get("Content-Length")=="123":
                    self.server.interim_requests.append((self.command,self.path,dict(self.headers)))
                    status=self.server.interim_statuses.popleft()
                    self.send_response_only(status);self.send_header("Content-Length","0");self.end_headers()
                    self.close_connection=True
                    return False
                return super().handle_expect_100()
        xml={"Content-Type":"application/xml"};empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(200,{},b""),(200,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3",handler=InterimHandler) as (worker,server):
                server.interim_statuses=deque([403,100]);server.interim_requests=[]
                directory=Path(temporary);workload={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path"}}
                result=continue_interim(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload)))
                self.assertEqual(result["request_transport"],"raw_http");self.assertEqual(len(server.requests),7)
                self.assertEqual(len(server.interim_requests),2);self.assertEqual(len(server.interim_statuses),0)
                for method,path,values in server.interim_requests:
                    headers={name.lower():value for name,value in values.items()}
                    self.assertEqual(method,"PUT");self.assertTrue(path.endswith("/testobj"))
                    self.assertEqual(headers["content-length"],"123");self.assertEqual(headers["expect"],"100-continue")
                    self.assertEqual(headers["accept-encoding"],"identity");self.assertNotIn("authorization",headers)
                headers={name.lower():value for name,value in server.requests[2][2].items()}
                self.assertEqual(headers["x-amz-acl"],"public-read-write");self.assertIn("authorization",headers)

    def test_sdk_expect_error_then_success_preserves_body_and_headers(self):
        """Send genuine SDK Expect requests through the source retry case; retain the missing-bucket 404 followed by valid literal bar and authoritative bucket cleanup."""
        from sdk_fixtures import fixture_worker
        from osv.ceph_raw_http import continue_retry
        xml={"Content-Type":"application/xml"};empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(404,xml,b'<Error><Code>NoSuchBucket</Code></Error>'),
            (200,{"ETag":'"37b51d194a7513e45b56f6524f2d51f2"'},b""),(200,{},b""),(204,{},b""),(200,xml,empty),(204,{},b""),(404,{},b"")]
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,"s3") as (worker,server):
                directory=Path(temporary)
                result=continue_retry(Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})))
                self.assertEqual(result["request_transport"],"sdk_http");self.assertEqual(len(server.requests),10)
                for index in (3,4):
                    request=server.requests[index];headers={name.lower():value for name,value in request[2].items()}
                    self.assertEqual(request[0],"PUT");self.assertEqual(request[3],b"bar")
                    self.assertEqual(headers["expect"],"100-continue");self.assertIn("authorization",headers)
                self.assertIn("-but-doesnt-exist/",server.requests[3][1]);self.assertNotIn("-but-doesnt-exist/",server.requests[4][1])

    def test_configuration_binding_missing_secret_and_target_env_override(self):
        """Reject absent configuration, mismatched endpoint and missing credential references before HTTP; explicit target env must supply the raw form credentials."""
        from sdk_fixtures import fixture_worker
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                directory=Path(temporary)
                def probe(workload):return Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload))
                with self.assertRaises(CaseNotRun):SourceHttp(probe({}))
                with self.assertRaises(CaseNotRun):SourceHttp(probe({"ceph_http":{"endpoint":"http://another.invalid","addressing_style":"path"}}))
                configured={"ceph_http":{"endpoint":worker.info["endpoint"],"addressing_style":"path","access_key_env":"OSV_TEST_RAW_AK","secret_key_env":"OSV_TEST_RAW_SK"}}
                with patch.dict(os.environ,{"OSV_TEST_RAW_AK":"","OSV_TEST_RAW_SK":""}):
                    with self.assertRaises(CaseNotRun):SourceHttp(probe(configured))
                    worker.target["env"]={**worker.target.get("env",{}),"OSV_TEST_RAW_AK":"target-key","OSV_TEST_RAW_SK":"target-secret"}
                    bound=SourceHttp(probe(configured));self.assertEqual(bound.access,"target-key");self.assertEqual(bound.secret,"target-secret")
                self.assertEqual(server.requests,[])


@unittest.skipUnless(os.environ.get("OSV_RUN_RAW_HTTP_TESTS")=="1","enable after building actual SDK workers and requests")
class RawHttpSdkTest(unittest.TestCase):
    """Preserve source protocol outcomes against a local isolated process while the genuine SDK owns every bucket and readback."""
    @classmethod
    def setUpClass(cls):
        """Start the common isolated local service with explicit dummy credentials and a configured raw HTTP endpoint matching each SDK."""
        import boto3
        from local_moto import LocalMotoServer
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        cls.server=LocalMotoServer();cls.server.start();cls.addClassCleanup(cls.server.stop)
        host,port=cls.server.get_host_and_port();cls.endpoint=f"http://{host}:{port}"
        cls.environment=patch.dict(os.environ,{"OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"","OSV_ENDPOINT":cls.endpoint,
            "OSV_REGION":"us-east-1","OSV_BUCKET":"osv-raw-http","OSV_PATH_STYLE":"true","OSV_RETRIES":"0","OSV_ALT_ACCESS_KEY":"","OSV_ALT_SECRET_KEY":""})
        cls.environment.start();cls.addClassCleanup(cls.environment.stop)
        cls.client=boto3.client("s3",endpoint_url=cls.endpoint,region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-raw-http")
        cls.commands={"java":["java","-Xmx256m","-jar",str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"),"s3"],"cpp":[str(ROOT/"drivers/cpp/build/osv-cpp-s3")]}

    def test_original_raw_http_cases_keep_service_differences_and_cleanup(self):
        """Execute all 41 source cases with actual HTTP/forms and SDK ownership; expect 11 valid cases to pass, 29 local service differences and one nonstandard contract not-run and verify no case leaves a bucket behind."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},30)
                try:
                    directory=Path(temporary);workload={"ceph_http":{"endpoint":self.endpoint,"addressing_style":"path"}}
                    rows=run_raw_cases(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",workload))
                    self.assertEqual(len(rows),41)
                    self.assertEqual([row["case"] for row in rows if row["status"]=="pass"],["ceph_raw_post_"+n for n in ("anonymous","authenticated","no_content_type","large","filename","ignored","escaped","metadata","chunk_bug","tags_authenticated")]+["ceph_raw_continue_retry"])
                    self.assertEqual(sum(row["status"]=="fail" for row in rows),29)
                    self.assertEqual([row["case"] for row in rows if row["status"]=="not_run"],["ceph_raw_post_case_insensitive"])
                    self.assertTrue(all(row["evidence"]["sdk_performance_sample"] is False for row in rows))
                    self.assertEqual({bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]},{"osv-raw-http"})
                finally:worker.close()

    def test_absent_raw_configuration_is_precise_not_run_and_sdk_retry_still_executes(self):
        """With no explicit HTTP destination, 40 protocol cases must perform zero HTTP and report their prerequisite; the independent SDK retry case remains executable and cleans its owned bucket."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                worker=Worker({"command":self.commands[language],"language":language,"api":"s3"},30)
                try:
                    directory=Path(temporary)
                    rows=run_raw_cases(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))
                    self.assertEqual([row["case"] for row in rows if row["status"]=="pass"],["ceph_raw_continue_retry"])
                    self.assertEqual(sum(row["status"]=="not_run" for row in rows),40)
                    self.assertTrue(all(row["missing_requirements"] in (["workload.ceph_http"],["nonstandard policy operator contract"]) for row in rows if row["status"]=="not_run"))
                    self.assertEqual({bucket["Name"] for bucket in self.client.list_buckets()["Buckets"]},{"osv-raw-http"})
                finally:worker.close()
