"""Real AWS SDK wire contracts for the explicitly named Ceph logging extension."""
import base64
import hashlib
import hmac
import json
import os
import re
import unittest
from urllib.parse import urlsplit, parse_qsl, quote
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker

LANGUAGES=tuple(os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","))
NS={"s":"http://s3.amazonaws.com/doc/2006-03-01/"}
EMPTY=(200,{},b"")
ERROR=(400, {"Content-Type":"application/xml","x-amz-request-id":"actual-failed-request"}, b"<Error><Code>InvalidArgument</Code><Message>logging rejected</Message><RequestId>actual-failed-request</RequestId></Error>")


def raw(worker,op,**kwargs):
    return worker.call({"op":op,**kwargs})


def rpc(worker,op,**kwargs):
    result=raw(worker,op,**kwargs)
    if not result["ok"]:raise AssertionError(result)
    return result


def signed(test,row):
    """Check AWS SigV4 against actual final HTTP bytes using independent local credentials."""
    method,path,headers,body=row
    headers={k.lower():v for k,v in headers.items()}
    auth=headers["authorization"]
    match=re.fullmatch(r"AWS4-HMAC-SHA256 Credential=local-test/([^,]+), SignedHeaders=([^,]+), Signature=([0-9a-f]+)",auth)
    test.assertIsNotNone(match)
    scope,names,signature=match.groups()
    selected=names.split(";")
    canonical_headers="".join(k+":"+" ".join(headers[k].split())+"\n" for k in selected)
    url=urlsplit(path)
    canonical_query="&".join(quote(k,safe="-_.~")+"="+quote(v,safe="-_.~") for k,v in sorted(parse_qsl(url.query,keep_blank_values=True)))
    payload=headers.get("x-amz-content-sha256",hashlib.sha256(body).hexdigest())
    if payload!="UNSIGNED-PAYLOAD":test.assertEqual(payload,hashlib.sha256(body).hexdigest())
    elif body:test.assertIn("content-md5",selected)
    canonical="\n".join((method,url.path or "/",canonical_query,canonical_headers,names,payload))
    stamp=headers["x-amz-date"]
    message="AWS4-HMAC-SHA256\n"+stamp+"\n"+scope+"\n"+hashlib.sha256(canonical.encode()).hexdigest()
    key=b"AWS4local-test"
    for part in scope.split("/"):key=hmac.new(key,part.encode(),hashlib.sha256).digest()
    test.assertEqual(signature,hmac.new(key,message.encode(),hashlib.sha256).hexdigest())


@unittest.skipUnless(os.environ.get("OSV_RUN_CEPH_LOGGING_WIRE_TESTS" )=="1","enable actual SDK integration contracts explicitly")
class CephLoggingSdkTests(unittest.TestCase):
    def test_extended_xml_is_signed_and_scoped(self):
        """Purpose: send all extension fields through official SDKs; expect escaped Ceph XML, valid final-body MD5/signature, and clean following stock request."""
        enabled={"TargetBucket":"log-target","TargetPrefix":"prefix<&é/","LoggingType":"Journal","ObjectRollTime":17,"RecordsBatchSize":23,
                 "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"a<&é/"},{"Name":"suffix","Value":".csv"}]}},
                 "TargetObjectKeyFormat":{"PartitionedPrefix":{"PartitionDateSource":"EventTime"}}}
        response=b"<BucketLoggingStatus><FlushedLoggingObject>real&amp;object</FlushedLoggingObject></BucketLoggingStatus>"
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[(200,{},response),EMPTY],backend="s3") as (worker,server):
                result=rpc(worker,"api.ceph.PutBucketLogging",bucket="logging-source",input={"BucketLoggingStatus":{"LoggingEnabled":enabled}},capture_response_body=True)
                self.assertEqual(result["status_code"],200)
                self.assertEqual(base64.b64decode(result["response_body_b64"]),response)
                row=server.requests[0];self.assertEqual(row[0],"PUT");self.assertEqual(parse_qsl(urlsplit(row[1]).query,keep_blank_values=True),[("logging","")])
                headers={k.lower():v for k,v in row[2].items()}
                self.assertEqual(headers["content-md5"],base64.b64encode(hashlib.md5(row[3]).digest()).decode())
                if "content-length" in headers:self.assertEqual(int(headers["content-length"]),len(row[3]))
                self.assertLessEqual(row[3].count(b"<?xml"),1)
                if language=="cpp":self.assertEqual(row[3].count(b"<?xml"),1)
                root=ET.fromstring(row[3]);self.assertEqual(root.tag,"{http://s3.amazonaws.com/doc/2006-03-01/}BucketLoggingStatus")
                e=root.find("s:LoggingEnabled",NS);self.assertIsNotNone(e)
                self.assertEqual(e.findtext("s:TargetBucket",namespaces=NS),"log-target")
                self.assertEqual(e.findtext("s:TargetPrefix",namespaces=NS),"prefix<&é/")
                for k,v in (("LoggingType","Journal"),("ObjectRollTime","17"),("RecordsBatchSize","23")):self.assertEqual(e.findtext("s:"+k,namespaces=NS),v)
                rules=e.findall("s:Filter/s:S3Key/s:FilterRule",NS)
                self.assertEqual([(r.findtext("s:Name",namespaces=NS),r.findtext("s:Value",namespaces=NS)) for r in rules],[("prefix","a<&é/"),("suffix",".csv")])
                self.assertIsNone(e.find("s:Filter/s:Key",NS))
                self.assertEqual(e.findtext("s:TargetObjectKeyFormat/s:PartitionedPrefix/s:PartitionDateSource",namespaces=NS),"EventTime")
                self.assertEqual(len(e.findall("s:TargetObjectKeyFormat",NS)),1)
                signed(self,row)
                rpc(worker,"api.PutBucketLogging",bucket="logging-source",input={"BucketLoggingStatus":{}})
                self.assertEqual(server.requests[1][0],"PUT")
                self.assertNotIn(b"LoggingType",server.requests[1][3]);signed(self,server.requests[1])

    def test_get_post_actual_body_and_failure_restores_method(self):
        """Purpose: preserve actual response XML and failures; expect POST signed as POST, real 204/400, and subsequent cleanup GET still GET."""
        response=b"<BucketLoggingStatus><LoggingEnabled><LoggingType>Journal</LoggingType><ObjectRollTime>0</ObjectRollTime></LoggingEnabled></BucketLoggingStatus>"
        flush=b"<BucketLoggingStatus><FlushedLoggingObject>a&amp;b</FlushedLoggingObject></BucketLoggingStatus>"
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[(200,{},response),(200,{},flush),(204,{},b""),ERROR,(200,{},b"<BucketLoggingStatus/>")],backend="s3") as (worker,server):
                for op,expected,status in (("Get",response,200),("Post",flush,200),("Post",b"",204)):
                    result=rpc(worker,"api.ceph."+op+"BucketLogging",bucket="logging-source",input={},capture_response_body=True)
                    self.assertEqual(result["status_code"],status);self.assertEqual(base64.b64decode(result["response_body_b64"]),expected)
                error=raw(worker,"api.ceph.PostBucketLogging",bucket="logging-source",input={},capture_response_body=True)
                self.assertFalse(error["ok"]);self.assertEqual(error["status_code"],400);self.assertEqual(error["error_code"],"InvalidArgument")
                self.assertEqual(base64.b64decode(error["response_body_b64"]),ERROR[2])
                rpc(worker,"api.GetBucketLogging",bucket="logging-source",input={})
                self.assertEqual([r[0] for r in server.requests],["GET","POST","POST","POST","GET"])
                for row in server.requests:self.assertEqual(row[3],b"");signed(self,row)

    def test_validation_rejects_unknown_and_wrong_types_before_http(self):
        """Purpose: reject unmodeled input locally without dropping fields; expect zero HTTP and no extension leakage into next valid operation."""
        invalid=[{"Surprise":1},{"LoggingType":None},{"ObjectRollTime":True},{"ObjectRollTime":1.5},{"ObjectRollTime":2**63},
                 {"Filter":{"Key":{"FilterRules":{}}}},{"Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":1}]}}},
                 {"TargetObjectKeyFormat":{"SimplePrefix":{"bad":1}}},{"Filter":{"WrongKey":{}}}]
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[(200,{},b"<BucketLoggingStatus/>")],backend="s3") as (worker,server):
                for extra in invalid:
                    result=raw(worker,"api.ceph.PutBucketLogging",bucket="logging-source",input={"BucketLoggingStatus":{"LoggingEnabled":{"TargetBucket":"target",**extra}}})
                    self.assertFalse(result["ok"],extra);self.assertEqual(server.requests,[])
                rpc(worker,"api.ceph.GetBucketLogging",bucket="logging-source",input={});self.assertEqual(server.requests[0][0],"GET")


if __name__=="__main__":unittest.main()
