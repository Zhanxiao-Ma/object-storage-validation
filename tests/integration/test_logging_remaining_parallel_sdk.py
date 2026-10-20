"""Route predetermined raw HTTP replies by actual request identity, so original source threads may genuinely interleave on one SDK client."""
from collections import defaultdict,deque
from http.server import ThreadingHTTPServer
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs,unquote
import uuid
from xml.etree import ElementTree as ET
from osv.ceph_logging_remaining_parallel import execute
from osv.ceph_logging_remaining_parallel_runtime import AsyncRuntime
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from sdk_fixtures import fixture_worker,FixtureHandler
from test_logging_remaining_extensions_sdk import WORK,PROBE,flushed,journal,name
from test_logging_workloads_sdk import OK,ABSENT,DELETED,EMPTY,listing,log_line


class WideListener(ThreadingHTTPServer):
    """Accept actual concurrent SDK connections without the fixture's default listen backlog becoming an unrelated transport bottleneck."""
    request_queue_size=128


def route(method,path):
    parsed=urlsplit(path);pieces=unquote(parsed.path).strip('/').split('/',1);bucket=pieces[0];key=pieces[1] if len(pieces)>1 else '';query=parse_qs(parsed.query,keep_blank_values=True)
    kind='logging' if 'logging' in query else 'policy' if 'policy' in query else 'v2' if query.get('list-type')==['2'] else 'v1' if method=='GET' and not key else ''
    return method,bucket,key,kind


class RoutedReplies:
    """Store independent per-request reply queues and assertions; this fixture never stores objects, generates log records from observed writes or evaluates permissions."""
    def __init__(self):self.rows=defaultdict(deque);self.lock=threading.Lock();self.failures=[];self.barrier=None;self.barrier_buckets=set();self.overlap=0;self.active=0;self.hold_put=None;self.release_after_configs=None;self.config_seen=0;self.put_started=threading.Event();self.config_released=threading.Event()
    def add(self,method,bucket,key='',kind='',result=OK):self.rows[(method,bucket,key,kind)].append(result)
    def bucket(self,bucket):self.add('HEAD',bucket,result=ABSENT);self.add('PUT',bucket)
    def probe(self,first):self.add('PUT',name(first),kind='logging',result=PROBE)
    def policy(self,bucket):self.add('PUT',bucket,kind='policy',result=DELETED)
    def config(self,bucket,result=OK):self.add('PUT',bucket,kind='logging',result=result)
    def list(self,bucket,keys,kind='v2'):self.add('GET',bucket,kind=kind,result=(200,{},listing(keys)))
    def body(self,bucket,key,text):self.add('GET',bucket,key,result=(200,{},text.encode()))
    def put(self,bucket,key):self.add('PUT',bucket,key)
    def delete(self,bucket,key):self.add('DELETE',bucket,key,result=DELETED)
    def remove_bucket(self,bucket):self.add('DELETE',bucket,result=DELETED);self.add('HEAD',bucket,result=ABSENT)
    def finish(self,bucket,known=(),discovered=()):
        self.add('HEAD',bucket)
        for key in sorted(known):self.delete(bucket,key)
        self.list(bucket,discovered,'v2')
        for key in discovered:self.delete(bucket,key)
        self.remove_bucket(bucket)
    def teardown(self,buckets,known,discovered=None):
        for bucket in buckets:self.config(bucket)
        for bucket in buckets:self.add('DELETE',bucket,kind='policy',result=DELETED)
        for bucket in reversed(buckets):self.finish(bucket,known.get(bucket,()),(discovered or {}).get(bucket,()))


class RoutedHandler(FixtureHandler):
    """Replay raw independently supplied replies after recording genuine SDK bytes; allow a real HTTP barrier to prove requests overlap instead of serializing a thread-shaped test."""
    def respond(self):
        body=self.chunks(self.rfile) if 'chunked' in self.headers.get('Transfer-Encoding','').lower() else self.rfile.read(int(self.headers.get('Content-Length','0')))
        decoded=self.headers.get('x-amz-decoded-content-length');signed_stream=self.headers.get('x-amz-content-sha256','').startswith('STREAMING-')
        if ('aws-chunked' in self.headers.get('Content-Encoding','') or signed_stream) and (decoded is None or len(body)!=int(decoded)):body=self.chunks(io.BytesIO(body))
        plan=self.server.plan;key=route(self.command,self.path)
        with plan.lock:
            self.server.requests.append((self.command,self.path,dict(self.headers),body));queue=plan.rows[key]
            if queue:status,headers,payload=queue.popleft()
            else:plan.failures.append(('unexpected request',key));status,headers,payload=500,{},b'<Error><Code>UnexpectedFixtureRequest</Code></Error>'
        held=plan.barrier is not None and key[0]=='POST' and key[3]=='logging' and key[1] in plan.barrier_buckets
        if held:
            with plan.lock:plan.active+=1;plan.overlap=max(plan.overlap,plan.active)
            try:plan.barrier.wait(10)
            except threading.BrokenBarrierError:
                with plan.lock:plan.failures.append(('actual SDK calls did not overlap',key))
            finally:
                with plan.lock:plan.active-=1
        if plan.hold_put is not None:
            bucket,object_key=plan.hold_put
            if key==('PUT',bucket,object_key,''):
                plan.put_started.set()
                if not plan.config_released.wait(10):
                    with plan.lock:plan.failures.append(('configuration did not overlap actual PUT',key))
            elif key==('PUT',bucket,'','logging'):
                with plan.lock:plan.config_seen+=1;seen=plan.config_seen
                if seen==plan.release_after_configs:
                    if not plan.put_started.wait(10):
                        with plan.lock:plan.failures.append(('configuration arrived without a live held PUT',key))
                    plan.config_released.set()
        self.send_response(status)
        for k,value in headers.items():self.send_header(k,value)
        self.send_header('Content-Length',str(len(payload)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(payload)
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond


def lines(items):
    rows=list(items);return '\n'.join(rows)+'\n' if rows else ''


def records(kind,buckets,keys,duplicate_last=False):
    line=log_line if kind=='Standard' else journal
    return lines(line(bucket,event,key) for bucket in buckets for event in ('REST.PUT.OBJECT','REST.DELETE.OBJECT') for repeat in range(2 if duplicate_last and bucket==buckets[-1] else 1) for key in keys)


def flush_plan(kind,shared,counts,barrier=False):
    p=RoutedReplies();counter=2;all_buckets=[];known={};discovered={}
    for count in counts:
        p.probe(counter);target=name(counter+2);buckets=[name(counter+3+i) for i in range(5)];counter+=8
        p.bucket(target)
        for bucket in buckets:p.bucket(bucket)
        p.policy(target)
        for bucket in buckets:p.config(bucket)
        keys=['myobject'+str(i) for i in range(count)]
        for bucket in buckets:
            for key in keys:p.put(bucket,key);p.delete(bucket,key)
        p.list(target,[]);out=['log/shared'] if shared else [bucket+'/records' for bucket in buckets]
        for bucket,key in zip(buckets,out):p.add('POST',bucket,kind='logging',result=flushed(key))
        p.list(target,out)
        for i,key in enumerate(out):p.body(target,key,records(kind,buckets if shared else [buckets[i]],keys))
        all_buckets += [target,*buckets];known.update({bucket:keys for bucket in buckets});discovered[target]=out
        if barrier and not shared and count==counts[0]:p.barrier=threading.Barrier(5);p.barrier_buckets=set(buckets)
    p.teardown(all_buckets,known,discovered);return p


def cleanup_plan(action,kind,shared,partial=False):
    p=RoutedReplies();p.probe(2);target=name(4);buckets=[name(i) for i in range(5,8 if partial else 10)];p.bucket(target)
    for bucket in buckets:p.bucket(bucket)
    p.policy(target)
    for bucket in buckets:p.config(bucket)
    keys=['myobject'+str(i) for i in range(3 if partial else 10)]
    for bucket in buckets:
        for key in keys:p.put(bucket,key);p.delete(bucket,key)
    p.list(target,[])
    changed=buckets[:-1] if partial else buckets
    for bucket in changed:
        if action=='deletion':p.remove_bucket(bucket)
        else:p.config(bucket)
    if partial:
        for key in keys:p.put(buckets[-1],key);p.delete(buckets[-1],key)
        p.probe(8);p.add('POST',buckets[-1],kind='logging',result=flushed('log/final'))
        out=['log/partial-'+str(i) for i in range(3)]
    else:
        out=['log/cleanup'] if shared else [bucket+'/cleanup' for bucket in buckets]
    p.list(target,out)
    for i,key in enumerate(out):
        selected=buckets if not partial and shared else [buckets[i]]
        p.body(target,key,records(kind,selected,keys,partial and i==len(buckets)-1))
    survivors=[buckets[-1]] if partial and action=='deletion' else [] if action=='deletion' else buckets
    p.teardown([target,*survivors],{bucket:keys for bucket in survivors},{target:out})
    return p


def configuration_plan(kind,update,barrier=False,missing_first_records=False):
    p=RoutedReplies();p.probe(2);target=name(4);source=name(5);p.bucket(target);p.bucket(source);p.policy(target);p.config(source)
    if update=='prefix':p.policy(target)
    p.config(source)
    keys=['myobject'+str(i) for i in range(100)]
    for key in keys:p.put(source,key)
    if update=='prefix':p.policy(target)
    out=['log/empty','log/update','log/concurrent'] if update=='roll_time' else ['log/empty','newlog1/update','newlog2/concurrent']
    p.config(source,flushed(out[1]));p.probe(6);p.probe(8);p.add('POST',source,kind='logging',result=flushed(out[2]))
    if update=='prefix' and kind=='Journal':p.list(source,keys)
    p.list(target,out);line=log_line if kind=='Standard' else journal
    for i,key in enumerate(out):
        selected=[] if i==0 or missing_first_records else keys[(i-1)*50:i*50]
        p.body(target,key,lines(line(source,'REST.PUT.OBJECT',obj) for obj in selected));p.delete(target,key)
    another=['anotherobject'+str(i) for i in range(100)]
    for key in another:p.put(source,key)
    p.probe(10);final='log/final' if update=='roll_time' else 'newlog2/final';p.add('POST',source,kind='logging',result=flushed(final));p.list(target,[final]);p.body(target,final,lines(line(source,'REST.PUT.OBJECT',key) for key in another))
    p.teardown([target,source],{source:keys+another},{target:[final]})
    if barrier:p.hold_put=(source,'myobject0');p.release_after_configs=3
    return p


def put_flush_plan():
    p=RoutedReplies();p.probe(2);target=name(4);source=name(5);p.bucket(target);p.bucket(source);p.policy(target);p.config(source)
    keys=['myobject'+str(i) for i in range(300)];out=['log/'+str(i).zfill(3) for i in range(31)]
    for key in keys:p.put(source,key)
    for key in out:p.add('POST',source,kind='logging',result=flushed(key))
    p.probe(6);p.list(target,out)
    for i,key in enumerate(out):p.body(target,key,lines(journal(source,'REST.PUT.OBJECT',obj) for obj in keys[i*10:(i+1)*10]))
    p.teardown([target,source],{source:keys},{target:out});return p

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_REMAINING_PARALLEL_TESTS')=='1','enable actual shared-SDK asynchronous logging fixtures')
class LoggingRemainingParallelSdkTest(unittest.TestCase):
    """Exercise original thread bodies using actual same-client requests and independent reply queues, preserving workload size and source oracle defects."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def run_source(self,language,suffix,plan,expected_failure=False):
        with tempfile.TemporaryDirectory() as directory,patch('sdk_fixtures.ThreadingHTTPServer',WideListener),fixture_worker(language,[],'s3',{'OSV_CONNECTIONS':'100'},handler=RoutedHandler) as (worker,server):
            server.plan=plan;d=Path(directory);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'out',WORK)
            with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,1000)]):
                if expected_failure:
                    with self.assertRaises(AssertionError):execute(Probe(ctx),suffix)
                else:self.assertEqual(execute(Probe(ctx),suffix)['protocol_adapter'],'ceph_logging_wire_extensions')
            self.assertEqual(plan.failures,[])
            self.assertEqual({key:len(values) for key,values in plan.rows.items() if values},{})
            for method,path,headers,body in server.requests:
                if method=='PUT' and route(method,path)[3]=='logging' and body:ET.fromstring(body)
            if plan.barrier is not None:self.assertGreaterEqual(plan.overlap,5)
            if plan.hold_put is not None:self.assertTrue(plan.put_started.is_set());self.assertTrue(plan.config_released.is_set())
            return list(server.requests)

    def test_four_concurrent_flush_sources_keep_all_original_rounds(self):
        """Run original four 5-bucket parallel flush sources with 10/100/0-key rounds unchanged; expect all actual POST keys/log counts and a five-way raw HTTP barrier proving real overlap."""
        for language in self.languages():
            for suffix,kind,shared,counts in [('concurrent_flush_j','Journal',False,[10,100,0]),('concurrent_flush_s','Standard',False,[10,100]),('concurrent_flush_j_single','Journal',True,[10,100,0]),('concurrent_flush_s_single','Standard',True,[10,100])]:
                with self.subTest(language=language,source=suffix):self.run_source(language,suffix,flush_plan(kind,shared,counts,barrier=not shared))

    def test_twelve_concurrent_cleanup_sources_preserve_record_and_file_bounds(self):
        """Run all original Journal/Standard × shared/separate prefix × delete/disable/update thread groups with five buckets and ten PUT+DELETE pairs; expect original 1..5 file bound and exact records, with owned cleanup."""
        for language in self.languages():
            for kind,letter in [('Journal','j'),('Standard','s')]:
                for shared in (False,True):
                    for action,stem in [('deletion','cleanup_bucket_concurrent_deletion'),('disabling','cleanup_concurrent_disabling'),('updating','cleanup_concurrent_updating')]:
                        suffix=stem+'_'+letter+('_single' if shared else '')
                        with self.subTest(language=language,source=suffix):self.run_source(language,suffix,cleanup_plan(action,kind,shared))

    def test_six_concurrent_partial_sources_preserve_original_ignored_verifications(self):
        """Run original three-source partial concurrent transitions and repeated final-source writes; expect actual final flush/file bounds while leaving unasserted final record booleans unchanged."""
        for language in self.languages():
            for kind,letter in [('Standard','s'),('Journal','j')]:
                for action in ('updating','disabling','deletion'):
                    suffix='part_cleanup_concurrent_'+action+'_'+letter
                    with self.subTest(language=language,source=suffix):self.run_source(language,suffix,cleanup_plan(action,kind,True,partial=True))

    def test_four_dynamic_configuration_sources_update_while_actual_put_is_pending(self):
        """Hold a real SDK PUT until the original main thread changes configuration; expect actual overlap, all 100+100 original writes and all source-specific key/count assertions across four variants."""
        for language in self.languages():
            for kind,letter in [('Standard','s'),('Journal','j')]:
                for update,word in [('roll_time','roll'),('prefix','pfx')]:
                    suffix='conf_concurrent_updating_'+word+'_'+letter
                    with self.subTest(language=language,source=suffix):self.run_source(language,suffix,configuration_plan(kind,update,barrier=True))

    def test_original_standard_concurrent_prefix_first_batch_is_not_asserted(self):
        """Return empty first-batch logs only in the original Standard concurrent-prefix variant; expect its deliberately omitted first assertion to remain omitted, with the second 100-record batch still asserted."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'conf_concurrent_updating_pfx_s',configuration_plan('Standard','prefix',missing_first_records=True))

    def test_three_hundred_put_threads_and_thirty_flush_threads_share_one_sdk_client(self):
        """Run all original 300 PUT and30 async flush threads plus final synchronous flush with actual pool100; expect 300 distinct original random bodies,31 POST requests, sorted31 log files and exact300 Journal records."""
        for language in self.languages():
            with self.subTest(language=language):
                rows=self.run_source(language,'put_and_flush',put_flush_plan())
                puts=[(route(method,path)[2],body) for method,path,headers,body in rows if method=='PUT' and route(method,path)[2].startswith('myobject')]
                self.assertEqual({key for key,body in puts},{'myobject'+str(i) for i in range(300)})
                self.assertEqual(len(puts),300);self.assertEqual(len({body for key,body in puts}),300)
                for key,body in puts:self.assertTrue(body.isalpha() and body.islower());self.assertTrue(10<=len(body)<=1024)
                self.assertEqual(sum(method=='POST' and route(method,path)[3]=='logging' for method,path,headers,body in rows),31)

    def test_original_pool100_requirement_is_checked_before_any_resource_creation(self):
        """Offer actual pool16 to the original explicitly pool100 workload; expect not_run before bucket/IAM mutations rather than silently dropping source client configuration."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[],'s3',{'OSV_CONNECTIONS':'16'}) as (worker,server):
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',WORK)
                with self.assertRaises(CaseNotRun):execute(Probe(ctx),'put_and_flush')
                self.assertEqual(server.requests,[])

    def test_original_three_hundred_workload_rejects_old_or_missing_admission_handshake_before_http(self):
        """Feed raw legacy/missing driver capacity metadata into the real source consumer; expect not_run before any real SDK HTTP rather than reducing the original330-task producer workload."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[],'s3',{'OSV_CONNECTIONS':'100'}) as (worker,server):
                self.assertEqual(worker.info['async_jobs_limit'],512)
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',WORK)
                for limit in (64,329,None):
                    with patch.dict(worker.info,{'async_jobs_limit':limit}),self.assertRaises(CaseNotRun):execute(Probe(ctx),'put_and_flush')
                saved=worker.info.pop('async_jobs_limit')
                try:
                    with self.assertRaises(CaseNotRun):execute(Probe(ctx),'put_and_flush')
                finally:worker.info['async_jobs_limit']=saved
                self.assertEqual(server.requests,[])

    def test_parallel_source_body_values_reach_matching_actual_sdk_requests(self):
        """Submit eight independently keyed literal bodies concurrently through actual scope producer→SDK consumer; expect exact key/body pairing and no shared temporary-file overwrite, followed by owned cleanup."""
        for language in self.languages():
            plan=RoutedReplies();bucket=name(2);plan.bucket(bucket);expected={'key-'+str(i):('independent-parallel-body-'+str(i)).encode() for i in range(8)}
            for key in expected:plan.put(bucket,key)
            plan.teardown([bucket],{bucket:list(expected)})
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[],'s3',{'OSV_CONNECTIONS':'100'},handler=RoutedHandler) as (worker,server):
                server.plan=plan;d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',WORK);errors=[]
                with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,20)]),AsyncRuntime(Probe(ctx)) as runtime:
                    runtime.main.create_bucket(Bucket=runtime.new_bucket())
                    def put(key,body):
                        try:runtime.main.put_object(Bucket=bucket,Key=key,Body=body)
                        except Exception as error:errors.append(error)
                    threads=[threading.Thread(target=put,args=(key,body)) for key,body in expected.items()]
                    for thread in threads:thread.start()
                    for thread in threads:thread.join()
                self.assertEqual(errors,[]);self.assertEqual(plan.failures,[])
                actual={route(method,path)[2]:body for method,path,headers,body in server.requests if method=='PUT' and route(method,path)[2]}
                self.assertEqual(actual,expected);self.assertFalse(any(values for values in plan.rows.values()))
