"""Raw independent HTTP vectors consumed by actual Java/C++ SDK logging workloads."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_logging_workloads import CASES,workload,verify
from sdk_fixtures import fixture_worker

EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
VEMPTY=b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>'
OK=(200,{},b'');DELETED=(204,{},b'');ABSENT=(404,{},b'')
NAMES=[uuid.UUID(int=n) for n in (1,2,3)]
SOURCE='osv-'+NAMES[0].hex

def listing(names):return ('<ListBucketResult><IsTruncated>false</IsTruncated>'+''.join('<Contents><Key>'+name+'</Key><Size>100</Size></Contents>' for name in names)+'</ListBucketResult>').encode()
def version_listing(names):return ('<ListVersionsResult><IsTruncated>false</IsTruncated>'+''.join('<Version><Key>'+name+'</Key><VersionId>history-'+str(index)+'</VersionId><IsLatest>true</IsLatest></Version>' for index,name in enumerate(names))+'</ListVersionsResult>').encode()
def log_line(bucket,event,key):return bucket+'-owner '+bucket+' [20/Oct/2026:00:00:00 +0000] 127.0.0.1 requester wire-rid '+event+' '+key+' "GET / HTTP/1.1" 200 - 3 3 1 1 - "actual fixture sdk" - host SigV4 - AuthHeader localhost - - -'
def upload(number):return [(200,{},('<InitiateMultipartUploadResult><UploadId>wire-upload-'+str(number)+'</UploadId></InitiateMultipartUploadResult>').encode())]+[(200,{'ETag':'"wire-part-'+str(index)+'"'},b'') for index in range(1,7)]+[(200,{'x-amz-version-id':'wire-completed-'+str(number)},b'<CompleteMultipartUploadResult><Key>myobject</Key><ETag>"whole"</ETag></CompleteMultipartUploadResult>'),(404,{},b'<Error><Code>NoSuchUpload</Code></Error>')]

def fixture_vector(operation,versioned,another=False,bad_log=None):
    """Specify independent wire inputs and expected final records; this fixture never performs storage, SDK parsing or production case assertions."""
    count=3 if another else 2;destination='osv-'+NAMES[1 if another else 0].hex
    response=[ABSENT,OK]*count
    if versioned:response += [OK,(200,{},b'<VersioningConfiguration><Status>Enabled</Status></VersioningConfiguration>')]
    setup=[DELETED]+[OK]*(2 if another else 1)
    if operation in ('put','mpu'):response += setup
    base=['myobject'+str(index) for index in range(5)]
    if operation in ('mpu','mpu_copy'):
        response += upload(1)
        if versioned:response += upload(2)
        base=['myobject']
    else:response += [(200,{'x-amz-version-id':'wire-put-'+str(index)},b'') for index in range(5*(2 if versioned and operation!='head' else 1))]
    if operation not in ('put','mpu'):response += setup
    if operation not in ('mpu','mpu_copy'):response += [(200,{},listing(base))]
    visible=[]
    if operation in ('get','delete','head'):
        for index in range(5):
            if versioned:
                visible.append('visible-version-'+str(index));response += [(200,{'x-amz-version-id':visible[-1]},b'')]
                response += [(200,{},b'abc') if operation=='get' else DELETED if operation=='delete' else OK]
            if operation=='get':response += [(200,{},b'abc')]
            elif operation=='delete':response += [DELETED]
            elif not versioned:response += [OK]
    elif operation in ('copy','mpu_copy'):response += [(200,{'x-amz-version-id':'copy-wire-'+str(index)},b'<CopyObjectResult><ETag>"copied"</ETag></CopyObjectResult>') for index in range(len(base))]
    elif operation=='multi_delete':
        if versioned:response += [(200,{},version_listing([name for name in base for _ in range(2)]))]
        response += [(200,{},b'<DeleteResult/>')]
    event={'put':'REST.PUT.OBJECT','delete':'REST.DELETE.OBJECT','get':'REST.GET.OBJECT','head':'REST.HEAD.OBJECT','copy':'REST.PUT.OBJECT','multi_delete':'REST.POST.DELETE_MULTI_OBJECT','mpu':'REST.POST.UPLOAD','mpu_copy':'REST.PUT.OBJECT'}[operation]
    out=['copy_of_'+key for key in base] if operation in ('copy','mpu_copy') else base
    record_count=(4 if versioned else 2) if operation=='mpu' else 5 if operation=='copy' else 1 if operation=='mpu_copy' else 10 if versioned else 5
    bucket=destination if operation in ('copy','mpu_copy') else SOURCE
    body='\n'.join(log_line(bucket,event,out[index%len(out)]) for index in range(record_count))
    if operation=='mpu':body+='\n'+'\n'.join(log_line(SOURCE,'REST.PUT.PART','myobject') for _ in range(12 if versioned else 6))
    if bad_log=='count':body=body.split('\n',1)[1]
    key='wrong/record' if bad_log=='prefix' else 'log/record'
    response += [OK,(200,{},listing([key]))]+([] if bad_log=='prefix' else [(200,{},body.encode())])+[OK]*count+[DELETED]*count
    # Actual target log object is discovered, read and then removed, never guessed.
    response += [OK,(200,{},listing([key])),DELETED,DELETED,ABSENT]
    if another:response += [OK]+[DELETED]*5+[(200,{},EMPTY),DELETED,ABSENT]
    known={'dummy',*base}
    if operation in ('copy','mpu_copy') and not another:known.update(out)
    if versioned:response += [OK,(200,{},version_listing(['wire-cleanup-key'])),DELETED,(200,{},VEMPTY),DELETED,ABSENT]
    else:response += [OK]+[DELETED]*len(known)+[(200,{},EMPTY),DELETED,ABSENT]
    return response,visible

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_WORKLOAD_TESTS')=='1','enable actual SDK logging workload fixtures')
class LoggingWorkloadsSdkTest(unittest.TestCase):
    """Exercise source workloads using actual SDK requests and independent raw log objects, versions, ETags and errors."""
    @classmethod
    def setUpClass(cls):
        """Reuse isolated Moto/real SDK commands; expect no real endpoint access."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def execute(self,target,operation,versioned,another=False,bad_log=None):
        fixtures,visible=fixture_vector(operation,versioned,another,bad_log)
        with tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],fixtures,'s3') as (worker,server):
            d=Path(temporary);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'output',{'identities':{'main':{'user_id':'actual-account-user','canonical_id':'different-canonical-owner'}}})
            with patch('osv.bucket_scope.uuid.uuid4',side_effect=NAMES[:3 if another else 2]):
                if bad_log is None:workload(Probe(ctx),operation,versioned,another)
                else:
                    with self.assertRaises(AssertionError):workload(Probe(ctx),operation,versioned,another)
            self.assertFalse(server.fixtures)
            policy=next(json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path)
            self.assertEqual(policy['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'],'actual-account-user')
            if operation=='copy':self.assertEqual(len(policy['Statement']),2)
            if versioned:
                self.assertTrue(any('versionId=history-0' in path and '/wire-cleanup-key' in path for _,path,_,_ in server.requests))
                for ident in visible:self.assertTrue(any('versionId='+ident in path for _,path,_,_ in server.requests))
            if operation in ('mpu','mpu_copy'):
                completed=[body for method,path,headers,body in server.requests if method=='POST' and 'uploadId=wire-upload-' in path]
                self.assertEqual(len(completed),2 if versioned else 1)
                for body in completed:
                    self.assertIn(b'wire-part-1',body);self.assertIn(b'wire-part-6',body)
                self.assertEqual(sum(method=='PUT' and 'partNumber=' in path and len(body)==5*1024*1024 for method,path,headers,body in server.requests),12 if versioned else 6)

    def test_twelve_basic_put_get_head_delete_and_copy_source_variants(self):
        """Run all twelve source variants with real standard logging waits and raw expected records; expect correct current/explicit-version chains, two copy policy statements and actual leftover-version cleanup."""
        variants=[(operation,versioned,False) for operation in ('put','delete','get','head','copy') for versioned in (False,True)]+[('copy',False,True),('copy',True,True)]
        for target in self.targets:
            for operation,versioned,another in variants:
                with self.subTest(language=target['language'],operation=operation,versioned=versioned,another=another):self.execute(target,operation,versioned,another)

    def test_six_original_thirty_mib_multipart_copy_and_multi_delete_variants(self):
        """Run six multipart/copy/batch-delete sources retaining 30MiB and six 5MiB parts; expect actual upload IDs/ETags, original POST/PART record counts, and version-aware batch inputs."""
        for target in self.targets:
            for operation in ('mpu','mpu_copy','multi_delete'):
                for versioned in (False,True):
                    with self.subTest(language=target['language'],operation=operation,versioned=versioned):self.execute(target,operation,versioned)

    def test_wrong_log_prefix_and_missing_event_fail_after_actual_list_get_and_cleanup(self):
        """Return independently malformed final log names/counts through actual SDK list/GET; expect prefix rejection before GET or count rejection after GET, with all owned buckets still shut down and deleted."""
        for target in self.targets:
            for corruption in ('prefix','count'):
                with self.subTest(language=target['language'],corruption=corruption):self.execute(target,'get',False,bad_log=corruption)

    def test_default_eighteen_sources_require_explicit_source_account_metadata(self):
        """Execute complete eighteen-source suite without user_id; expect all not_run before creating buckets, never false source passes from absent logging service."""
        for target in self.targets:
            with self.subTest(language=target['language']),tempfile.TemporaryDirectory() as temporary:
                d=Path(temporary);worker=Worker(target,30)
                try:
                    rows=run_cases(Context(worker,'unused/',d,b'independent',d/'body',d/'out',{}),CASES)
                    self.assertEqual(len(rows),18);self.assertEqual({row['status'] for row in rows},{'not_run'})
                    self.assertEqual([bucket['Name'] for bucket in self.client.list_buckets()['Buckets']],['osv-sdk-integration'])
                finally:worker.close()

class JournalRecordBoundaryTest(unittest.TestCase):
    """Keep source eight-field journal parser boundaries independent from standard SDK's actual unsupported extension gate."""
    def test_original_journal_eight_fields_and_event_count_oracles(self):
        """Provide independent eight-field journal records, short/long schemas and wrong counts; expect exact upstream schema/count acceptance and rejection."""
        body='owner bucket [20/Oct/2026:00:00:00 +0000] REST.PUT.OBJECT key 3 version etag'
        verify(body,'bucket','REST.PUT.OBJECT',['key'],1,'Journal')
        for malformed in (body.rsplit(' ',1)[0],body+' extra'):
            with self.subTest(raw=malformed),self.assertRaises(AssertionError):verify(malformed,'bucket','REST.PUT.OBJECT',['key'],1,'Journal')
        with self.assertRaises(AssertionError):verify(body,'bucket','REST.PUT.OBJECT',['key'],2,'Journal')
