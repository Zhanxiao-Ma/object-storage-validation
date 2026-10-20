"""Real SDK log/header producer-consumer regressions for standard Ceph access records."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.ceph_logging_controls import identity
from osv.worker import Worker
from osv.ceph_logging_records import CASES,mtime,request_id,acl_required,object_names,verify_key,parse,records,field
from datetime import datetime,timedelta
from sdk_fixtures import fixture_worker,FixtureHandler

@unittest.skipUnless(os.environ.get("OSV_RUN_LOGGING_RECORD_TESTS")=="1","enable actual SDK logging record fixtures")
class LoggingRecordsSdkTest(unittest.TestCase):
    """Keep the source's actual waits and consume real SDK headers/log-object bodies without substituting IO logic."""
    @classmethod
    def setUpClass(cls):
        """Reuse local isolated service and real SDK commands; expect no real cloud access."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_put_request_id_reaches_real_log_object_consumer(self):
        """Return an independent x-amz-request-id on real PutObject and matching raw 26-column log body on real GetObject; expect production oracle to compare those actual producer/consumer outputs and clean both buckets."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';names=[uuid.UUID(int=1),uuid.UUID(int=2)];source="osv-"+names[0].hex
        record=("main-id "+source+' [20/Oct/2026:00:00:00 +0000] 127.0.0.1 main-user wire-rid-7 REST.PUT.OBJECT my-test-object "PUT /my-test-object HTTP/1.1" 200 - 0 3 1 1 - "fixture sdk" - host SigV4 - AuthHeader localhost - - -').encode()
        listed=b'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>log/record</Key><Size>400</Size></Contents></ListBucketResult>'
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(200,{},b""),(200,{"x-amz-request-id":"wire-rid-7"},b""),(200,{},b""),(200,{},listed),(200,{},record)]+[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},listed),(204,{},b""),(204,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(204,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"main-id","user_id":"main-user-id"}}})
                with patch("osv.bucket_scope.uuid.uuid4",side_effect=names):request_id(Probe(ctx))
                self.assertEqual(json.loads(server.requests[4][3])["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"],"main-user-id");self.assertEqual(len(server.requests),25);self.assertTrue(server.requests[6][1].endswith("/my-test-object"));self.assertEqual(server.requests[7][3],b"dummy")
                self.assertTrue(server.requests[9][1].endswith("/log/record"));self.assertEqual(server.requests[9][0],"GET")
                self.assertEqual(server.requests[16][0],"DELETE");self.assertTrue(server.requests[16][1].endswith("/log/record"))

    def test_acl_required_transitions_consume_actual_alternate_requests_and_log_objects(self):
        """Exercise public object/bucket ACL then public policy through both real SDKs; expect alternate credentials and the original Yes/- log-field oracles plus full cleanup."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        def listed(*keys):return ('<ListBucketResult><IsTruncated>false</IsTruncated>'+''.join('<Contents><Key>'+k+'</Key><Size>400</Size></Contents>' for k in keys)+'</ListBucketResult>').encode()
        names=[uuid.UUID(int=1),uuid.UUID(int=2)];source='osv-'+names[0].hex
        for target in self.targets:
            for obj in (False,True):
                event='REST.GET.OBJECT' if obj else 'REST.GET.BUCKET';key='my-test-object' if obj else '-'
                def record(value):return ('main-id '+source+' [20/Oct/2026:00:00:00 +0000] 127.0.0.1 alt-user rid '+event+' '+key+' "GET / HTTP/1.1" 200 - 3 3 1 1 - "fixture sdk" - host SigV4 - AuthHeader localhost - - '+value).encode()
                access=(200,{},b'abc' if obj else empty)
                fixtures=[(404,{},b''),(200,{},b''),(404,{},b''),(200,{},b''),(204,{},b''),(200,{},b''),(200,{},b''),(200,{},empty),(200,{},b''),access,(200,{},b''),(200,{},listed('log/one')),(200,{},record('Yes')),(204,{},b''),access,(200,{},b''),(200,{},listed('log/one','log/two')),(200,{},record('-'))]+[(200,{},b'')]*2+[(204,{},b'')]*2+[(200,{},b''),(200,{},listed('log/one','log/two')),(204,{},b''),(204,{},b''),(204,{},b''),(404,{},b''),(200,{},b''),(204,{},b''),(204,{},b''),(200,{},empty),(204,{},b''),(404,{},b'')]
                with self.subTest(language=target['language'],object_acl=obj),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],fixtures,'s3',overrides={'OSV_ALT_ACCESS_KEY':'real-alt','OSV_ALT_SECRET_KEY':'real-alt-secret'}) as (worker,server):
                    d=Path(temporary);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'output',{'identities':{'main':{'canonical_id':'main-id','user_id':'main-user-id'}}})
                    with patch('osv.bucket_scope.uuid.uuid4',side_effect=names):acl_required(Probe(ctx),obj)
                    self.assertEqual(len(server.requests),34)
                    for index in (9,14):self.assertIn('real-alt/',{name.lower():value for name,value in server.requests[index][2].items()}['authorization'])
                    self.assertIn(b's3:GetObject' if obj else b's3:ListBucket',server.requests[13][3]);self.assertTrue(server.requests[17][1].endswith('/log/two'))

    def test_original_simple_partitioned_prefix_vectors_use_actual_sdk_xml(self):
        """Serve independent current-time standard logging keys after the source's real flush wait; expect both prefixes, all five writes, original partition/default-region fields and actual typed key-format XML."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for target in self.targets:
            for partitioned in (False,True):
                if target['language']=='java':continue # Genuine old Java SDK lacks TargetObjectKeyFormat.
                names=[uuid.UUID(int=n) for n in (1,2,3,4)]
                fixtures=[]
                for prefix in ('log/',''):
                    fixtures += [(404,{},b''),(200,{},b''),(404,{},b''),(200,{},b''),(204,{},b''),(200,{},b'')]+[(200,{},b'')]*6+[(200,{},b'NEW_LOG')]+[(200,{},b'')]*2+[(204,{},b'')]*2+[(200,{},b''),(200,{},b'LAST_LOG'),(204,{},b''),(204,{},b''),(404,{},b''),(200,{},b'')]+[(204,{},b'')]*6+[(200,{},empty),(204,{},b''),(404,{},b'')]
                class NamingHandler(FixtureHandler):
                    """Supply independent raw dated listing XML at response time so genuine source sleeps remain intact."""
                    def respond(self):
                        status,headers,payload=self.server.fixtures[0]
                        if payload in (b'NEW_LOG',b'LAST_LOG'):
                            if payload==b'NEW_LOG':
                                count=sum(1 for request in self.server.requests if request[0]=='PUT' and '/myobject0' in request[1]);prefix='log/' if count==1 else '';source='osv-'+names[0 if count==1 else 2].hex;now=datetime.now();stamp=now.strftime('%Y-%m-%d-%H-%M-%S-')+'wire'
                                self.server.last_key=prefix+('main-user-id/default/'+source+'/'+now.strftime('%Y/%m/%d/') if partitioned else '')+stamp
                            payload=('<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>'+self.server.last_key+'</Key><Size>400</Size></Contents></ListBucketResult>').encode();self.server.fixtures[0]=(status,headers,payload)
                        super().respond()
                    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond
                with self.subTest(language=target['language'],partitioned=partitioned),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],fixtures,'s3',handler=NamingHandler) as (worker,server):
                    d=Path(temporary);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'output',{'identities':{'main':{'canonical_id':'main-id','user_id':'main-user-id'}}})
                    with patch('osv.bucket_scope.uuid.uuid4',side_effect=names):object_names(Probe(ctx),partitioned)
                    self.assertEqual(len(server.requests),64);self.assertEqual(sum('/myobject' in path and method=='PUT' for method,path,_,_ in server.requests),10)
                    self.assertIn(b'PartitionedPrefix' if partitioned else b'SimplePrefix',server.requests[5][3])

    def test_logging_mtime_uses_actual_headers_and_original_string_comparisons(self):
        """Supply four real Last-Modified response headers with same/increasing source values; expect original waits, changed prefix and disable/re-enable requests without parsing new datetime semantics."""
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>';body=b'<BucketLoggingStatus/>';stamp=lambda second:{"Last-Modified":"Tue, 20 Oct 2026 00:00:0"+str(second)+" GMT"}
        fixtures=[(404,{},b""),(200,{},b""),(404,{},b""),(200,{},b""),(204,{},b""),(200,{},b""),(200,stamp(1),body),(200,{},b""),(200,stamp(1),body),(204,{},b""),(200,{},b""),(200,stamp(2),body),(200,{},b""),(200,{},b""),(200,stamp(3),body)]+[(200,{},b"")]*2+[(204,{},b"")]*2+[(200,{},b""),(200,{},empty),(204,{},b""),(404,{},b"")]*2
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary,fixture_worker(target["language"],fixtures,"s3") as (worker,server):
                d=Path(temporary);ctx=Context(worker,"unused/",d,b"independent",d/"body",d/"output",{"identities":{"main":{"canonical_id":"main-id","user_id":"main-user-id"}}})
                mtime(Probe(ctx));self.assertEqual(len(server.requests),27)
                self.assertIn("another-log/",json.loads(server.requests[9][3])["Statement"][0]["Resource"])
                for index in (10,13):self.assertIn(b'another-log/',server.requests[index][3])
                self.assertNotIn(b'LoggingEnabled',server.requests[12][3])

    def test_default_logging_record_sources_require_metadata_and_identities(self):
        """Execute all six source entries with default missing identities; expect explicit not_run for every source and zero bucket leaks, never source pass from missing log features."""
        for target in self.targets:
            with self.subTest(language=target["language"]),tempfile.TemporaryDirectory() as temporary:
                d=Path(temporary);worker=Worker(target,30)
                try:
                    rows=run_cases(Context(worker,"unused/",d,b"independent",d/"body",d/"output",{}),CASES)
                    self.assertEqual(len(rows),6);self.assertTrue(all(row["status"]=="not_run" for row in rows));self.assertEqual([b["Name"] for b in self.client.list_buckets()["Buckets"]],["osv-sdk-integration"])
                finally:worker.close()

class LoggingRecordParserTest(unittest.TestCase):
    """Use independent raw records and expected fields to cover the strict upstream parser boundary."""
    def test_source_user_id_is_explicit_and_never_falls_back_to_canonical_owner(self):
        """Provide a canonical ACL owner with missing/empty source user ID and distinct valid IDs; expect explicit missing-prerequisite results or exactly user_id, preserving different account-ID meanings."""
        with tempfile.TemporaryDirectory() as temporary:
            d=Path(temporary)
            for metadata in ({'canonical_id':'canonical-owner'},{'canonical_id':'canonical-owner','user_id':''}):
                with self.subTest(metadata=metadata),self.assertRaises(CaseNotRun):identity(Probe(Context(None,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':metadata}})))
            self.assertEqual(identity(Probe(Context(None,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'canonical_id':'canonical-owner','user_id':'source-user'}}}))),'source-user')

    def test_key_oracle_rejects_wrong_prefix_region_date_and_time_boundaries(self):
        """Pass independent simple/partitioned names and corrupt each source field separately; expect valid names accepted and wrong prefix, account, region, bucket, date, invalid calendar, and >5-second timestamps rejected."""
        now=datetime(2026,10,20,12,30,0);simple='2026-10-20-12-30-00-id';partition='acct/default/bucket/2026/10/20/'+simple
        verify_key(simple,False,'',now,'acct','bucket');verify_key('log/'+simple,False,'log/',now,'acct','bucket');verify_key('log/'+partition,True,'log/',now,'acct','bucket')
        for key,part,prefix in [('wrong/'+simple,False,'log/'),(partition.replace('acct/','other/',1),True,''),(partition.replace('/default/','/region/',1),True,''),(partition.replace('/bucket/','/other/',1),True,''),(partition.replace('/2026/10/20/','/2026/10/19/'),True,''),(simple.replace('-10-20-','-13-20-'),False,''),(simple.replace('-30-00-','-30-06-'),False,'')]:
            with self.subTest(key=key),self.assertRaises(AssertionError):verify_key(key,part,prefix,now,'acct','bucket')

    def test_original_twenty_six_fields_and_malformed_records(self):
        """Provide valid quoted 26-column records and too-short/long/broken-quote inputs; expect exact semantic fields and explicit parser failures without reproducing parsing logic."""
        line='owner bucket [20/Oct/2026:00:00:00 +0000] 127.0.0.1 requester rid REST.GET.OBJECT key "GET /key HTTP/1.1" 200 - 3 3 1 1 - "fixture sdk" - host SigV4 - AuthHeader localhost - - Yes'
        actual=parse(line);self.assertEqual(actual["RequestID"],"rid");self.assertEqual(actual["RequestURI"],"GET /key HTTP/1.1");self.assertEqual(actual["ACLRequired"],"Yes");self.assertEqual(actual["RequestDateTime"],"20/Oct/2026:00:00:00 +0000")
        records(line,"bucket","REST.GET.OBJECT",["key"],1);field(line,"bucket","REST.GET.OBJECT","key","ACLRequired","Yes")
        for bad in (line.rsplit(" ",1)[0],line+" extra",line+' "unterminated'):
            with self.subTest(raw=bad),self.assertRaises((AssertionError,ValueError)):parse(bad)
        with self.assertRaises(AssertionError):records(line,"bucket","REST.GET.OBJECT",["missing"],1)
        with self.assertRaises(AssertionError):field(line,"bucket","REST.GET.OBJECT","key","ACLRequired","-")
