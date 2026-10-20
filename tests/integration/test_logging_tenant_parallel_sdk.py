"""Real SDK seven-namespace vectors and two genuine fifty-way shared-client phases."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.ceph_logging_tenants import CASES as TENANT_CASES,tenant_standard,VECTORS
from osv.ceph_logging_parallel import CASES as PARALLEL_CASES,concurrent_put
from sdk_fixtures import fixture_worker,FixtureHandler
from test_logging_workloads_sdk import listing,log_line
OK=(200,{},b'');DEL=(204,{},b'');NO=(404,{},b'');EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'


def tenant_vectors():
    names=[uuid.UUID(int=index) for index in range(1,14)];available=iter(names);sequence=[];expected=[]
    for source_identity,target_identity,same,address,valid in VECTORS:
        source='osv-'+next(available).hex;target=source if same else 'osv-'+next(available).hex
        expected.append((source,target,source_identity,target_identity,address,valid));sequence += [NO,OK]*2+[DEL,OK if valid else (404,{},b'<Error><Code>NoSuchKey</Code></Error>')]
        if valid:
            body='\n'.join(log_line(source,'REST.PUT.OBJECT','myobject'+str(index)) for index in range(5)).encode()
            sequence += [OK]*5+[(200,{},listing(['myobject'+str(index) for index in range(5)])),OK,(200,{},listing(['log/record'])),(200,{},body)]
        sequence += [OK]*2+[DEL]*2+[OK,(200,{},listing(['log/record']) if valid else EMPTY)]+([DEL] if valid else [])+[DEL,NO]+[OK]+[DEL]*(6 if valid else 0)+[(200,{},EMPTY),DEL,NO]
    return sequence,names,expected


def parallel_vectors():
    names=[uuid.UUID(int=1),uuid.UUID(int=2)];source='osv-'+names[0].hex;objects=['myobject'+str(index) for index in range(50)]
    bodies=[('\n'.join(log_line(source,'REST.PUT.OBJECT',name) for name in objects[start:start+25])+'\n').encode() for start in (0,25)]
    sequence=[NO,OK]*2+[DEL,OK]+[OK]*50+[(200,{},listing(objects))]+[OK]*50+[(200,{},listing(['log/first','log/second'])),(200,{},bodies[0]),(200,{},bodies[1])]+[OK]*2+[DEL]*2
    sequence += [OK,(200,{},listing(['log/first','log/second'])),DEL,DEL,DEL,NO]+[OK]+[DEL]*51+[(200,{},EMPTY),DEL,NO]
    return sequence,names

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_TENANT_PARALLEL_TESTS')=='1','enable real SDK tenant/concurrency logging sources')
class TenantParallelSdkTest(unittest.TestCase):
    """Verify actual credential namespaces, returned log bodies and overlapping native SDK calls without replacing storage or SDK behavior."""
    def test_all_seven_namespace_vectors_keep_real_tenant_signatures_and_original_targets(self):
        """Run all seven original vectors on both SDKs including same bare names in different namespaces; expect five real full logs, two exact NoSuchKey denials and owner-consistent shutdown/cleanup."""
        for language in ('java','cpp'):
            sequence,names,expected=tenant_vectors()
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,sequence,'s3',{'OSV_TENANT_ACCESS_KEY':'genuine-tenant','OSV_TENANT_SECRET_KEY':'genuine-tenant-secret'}) as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'tenant_name':'source-tenant','identities':{'main':{'user_id':'main-user'},'tenant':{'user_id':'tenant-user','canonical_id':'different-owner'}}})
                started=time.monotonic()
                with patch('osv.bucket_scope.uuid.uuid4',side_effect=names):result=tenant_standard(Probe(ctx))
                self.assertGreaterEqual(time.monotonic()-started,27.4);self.assertEqual(result,{'source_vectors':7,'successful_log_vectors':5,'negative_no_such_key_vectors':2});self.assertFalse(server.fixtures);self.assertEqual(len(server.requests),206)
                creations=[(path,{k.lower():v for k,v in headers.items()}['authorization']) for method,path,headers,body in server.requests if method=='PUT' and b'CreateBucketConfiguration' in body]
                self.assertEqual(len(creations),14)
                for index,(source,target,source_identity,target_identity,address,valid) in enumerate(expected):
                    for offset,owner in ((0,source_identity),(1,target_identity)):
                        self.assertIn('Credential='+('genuine-tenant' if owner=='tenant' else 'local-test')+'/',creations[index*2+offset][1])
                configured=[(body,headers) for method,path,headers,body in server.requests if method=='PUT' and 'logging' in path and b'LoggingEnabled' in body]
                self.assertEqual(len(configured),7)
                for (body,headers),(source,target,source_identity,target_identity,address,valid) in zip(configured,expected):
                    value=('source-tenant:' if address=='qualified' else ':' if address=='default' else '')+target;self.assertIn(value.encode(),body)
                    self.assertIn('Credential='+('genuine-tenant' if source_identity=='tenant' else 'local-test')+'/',{k.lower():v for k,v in headers.items()}['authorization'])
                policies=[json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path]
                self.assertEqual(policies[3]['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'],'tenant-user');self.assertIn('arn:aws:s3::source-tenant:',policies[3]['Statement'][0]['Resource'])

    def test_two_fifty_request_phases_overlap_on_one_actual_fifty_connection_client(self):
        """Hold each independent PUT response until all fifty real SDK requests arrive in each phase; expect pool50, genuine fifty-way overlap twice, fifty original event records across two log objects and cleanup of all fifty keys."""
        class BarrierHandler(FixtureHandler):
            """Delay raw fixture responses until all fifty callers arrive; do not implement storage or SDK scheduling."""
            def respond(self):
                phase=0 if self.command=='PUT' and '/myobject' in self.path else 1 if self.command=='PUT' and self.path.split('?')[0].endswith('/dummy') else None
                if phase is not None:
                    with self.server.phase_lock:
                        self.server.phase_counts[phase]+=1
                        if self.server.phase_counts[phase]==50:self.server.phase_events[phase].set()
                    if not self.server.phase_events[phase].wait(8):self.server.phase_failures.append(phase)
                super().respond()
            do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond
        for language in ('java','cpp'):
            sequence,names=parallel_vectors()
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,patch('sdk_fixtures.ThreadingHTTPServer.request_queue_size',128),fixture_worker(language,sequence,'s3',handler=BarrierHandler) as (worker,server):
                server.phase_lock=threading.Lock();server.phase_counts=[0,0];server.phase_events=[threading.Event(),threading.Event()];server.phase_failures=[]
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'source-user'}}});started=time.monotonic()
                with patch('osv.bucket_scope.uuid.uuid4',side_effect=names):result=concurrent_put(Probe(ctx))
                self.assertGreaterEqual(time.monotonic()-started,5.4);self.assertFalse(server.fixtures);self.assertEqual(server.phase_counts,[50,50]);self.assertEqual(server.phase_failures,[])
                self.assertEqual(result,{'workload_sdk_clients':1,'connections':50,'concurrent_requests_per_phase':50,'cleanup_confirmed':True});self.assertEqual(sum(method=='DELETE' and '/myobject' in path for method,path,headers,body in server.requests),50)
                bodies=[body for method,path,headers,body in server.requests if method=='PUT' and '/myobject' in path];self.assertEqual(len(bodies),50);self.assertEqual(len(set(bodies)),50);self.assertTrue(all(10<=len(body)<=1024 and body.islower() for body in bodies));self.assertEqual(list(d.glob('*.parallel-*')),[])

    def test_both_source_defaults_and_tenant_prerequisites_stop_before_io(self):
        """Run both source entries without account metadata and direct tenant source with missing real tenant credentials; expect not_run before any HTTP or worker clone, never fallback to main."""
        for language in ('java','cpp'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{})
                rows=run_cases(ctx,TENANT_CASES+PARALLEL_CASES);self.assertEqual(len(rows),2);self.assertEqual({row['status'] for row in rows},{'not_run'})
                ctx.workload.update({'tenant_name':'source-tenant','identities':{'main':{'user_id':'main'},'tenant':{'user_id':'tenant'}}})
                with self.assertRaises(CaseNotRun) as missing:tenant_standard(Probe(ctx))
                self.assertIn('identity_tenant',missing.exception.requirements);self.assertEqual(server.requests,[])
