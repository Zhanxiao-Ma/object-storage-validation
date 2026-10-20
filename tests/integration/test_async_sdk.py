"""Exercise actual shared-client SDK futures and JSONL consumers over independent HTTP."""
import base64
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from sdk_fixtures import fixture_worker, FixtureHandler

LANGUAGES=tuple(os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','))


class PausedHandler(FixtureHandler):
    """Supply independent per-path bytes and barriers, without implementing object storage."""
    def respond(self):
        length=int(self.headers.get('Content-Length','0'));body=self.rfile.read(length)
        with self.server.guard:
            self.server.requests.append((self.command,self.path,dict(self.headers),body))
            if len([r for r in self.server.requests if '/paused-' in r[1]])>=2:self.server.entered.set()
        if '/paused-' in self.path:self.server.release.wait(5)
        failed='/paused-error' in self.path
        payload=(b'<Error><Code>AccessDenied</Code><Message>independent rejection</Message><RequestId>error-body-id</RequestId></Error>' if failed else b'<BucketLoggingStatus><FlushedLoggingObject>actual-success</FlushedLoggingObject></BucketLoggingStatus>')
        self.send_response(403 if failed else 200)
        self.send_header('Content-Type','application/xml');self.send_header('x-amz-request-id','error-body-id' if failed else 'success-id')
        self.send_header('Content-Length',str(len(payload)));self.end_headers()
        if self.command!='HEAD':self.wfile.write(payload)
    do_GET=do_HEAD=do_POST=respond


def initialize(server):
    server.socket.listen(128)  # Admit the explicit 64 real connections without fixture backlog loss.
    server.guard=threading.Lock();server.entered=threading.Event();server.release=threading.Event()


def collect(worker,job):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        response=worker.call({'op':'async_poll','job_id':job})
        if not response['ok']:raise AssertionError(response)
        if response['done']:return response['result']
        time.sleep(.005)
    raise AssertionError('actual async SDK job did not finish')


@unittest.skipUnless(os.environ.get("OSV_RUN_ASYNC_SDK_TESTS" )=="1","enable actual SDK integration contracts explicitly")
class AsyncSdkTests(unittest.TestCase):
    def test_nested_async_batch_and_other_client_jobs_reject_before_http(self):
        """Purpose: prevent batch threads from racing the async job map or selecting another service's client; expect whole-batch validation and zero HTTP for nested control/signer/stream jobs."""
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[],backend='s3',handler=PausedHandler) as (worker,server):
                initialize(server)
                for operation in ('async_start','async_poll'):
                    result=worker.call({'op':'concurrent_requests','requests':[{'op':'bucket_head','bucket':'must-not-be-issued'},{'op':operation,'job_id':'missing','request':{'op':'bucket_head'}}]})
                    self.assertFalse(result['ok'],result)
                    self.assertEqual(server.requests,[])
                for operation in ('presign','atomic_stream','api.sns.ListTopics','api.s3control.GetPublicAccessBlock'):
                    result=worker.call({'op':'async_start','request':{'op':operation,'input':{}}})
                    self.assertFalse(result['ok'],result)
                    self.assertEqual(server.requests,[])

    def test_python_source_threads_consume_their_actual_sdk_result(self):
        """Purpose: interleave real producer calls via the common Python consumer; expect each source thread to receive its own HTTP result/body rather than another thread's JSONL reply."""
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[],backend='s3',overrides={'OSV_CONNECTIONS':'100'},handler=PausedHandler) as (worker,server):
                initialize(server)
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futures=[pool.submit(worker.async_call,{'op':'api.ceph.PostBucketLogging','bucket':('paused-error-' if i%2 else 'paused-success-')+str(i),'input':{},'capture_response_body':True}) for i in range(8)]
                    try:self.assertTrue(server.entered.wait(2))
                    finally:server.release.set()
                    for i,future in enumerate(futures):
                        result=future.result(timeout=10)
                        self.assertEqual(result['ok'],not bool(i%2),result)
                        self.assertEqual(result['status_code'],403 if i%2 else 200)
                        self.assertIn(b'error-body-id' if i%2 else b'actual-success',base64.b64decode(result['response_body_b64']))
                self.assertEqual(len(server.requests),8)

    def test_shared_client_calls_overlap_and_keep_actual_bodies_and_errors(self):
        """Purpose: pause two genuine SDK calls simultaneously; expect responsive polling, isolated actual success/error bodies, and consumable single-use results."""
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[],backend='s3',overrides={'OSV_CONNECTIONS':'100'},handler=PausedHandler) as (worker,server):
                initialize(server);self.assertIn('async_requests',worker.capabilities)
                self.assertEqual(worker.info['connections_per_worker'],'100')
                jobs=[]
                try:
                    for bucket in ('paused-success','paused-error'):
                        response=worker.call({'op':'async_start','request':{'op':'api.ceph.PostBucketLogging','bucket':bucket,'input':{},'capture_response_body':True}})
                        self.assertTrue(response['ok'],response);jobs.append(response['job_id'])
                    self.assertTrue(server.entered.wait(2),'SDK operations were serialized instead of overlapping')
                    for job in jobs:
                        pending=worker.call({'op':'async_poll','job_id':job});self.assertTrue(pending['ok']);self.assertFalse(pending['done'])
                    ordinary=worker.call({'op':'bucket_head','bucket':'ordinary'})
                    self.assertTrue(ordinary['ok'],ordinary);self.assertNotIn('response_body_b64',ordinary)
                finally:server.release.set()
                success,error=[collect(worker,job) for job in jobs]
                self.assertTrue(success['ok']);self.assertEqual(success['status_code'],200)
                self.assertIn(b'actual-success',base64.b64decode(success['response_body_b64']))
                self.assertFalse(error['ok']);self.assertEqual(error['status_code'],403);self.assertEqual(error['error_code'],'AccessDenied')
                self.assertIn(b'error-body-id',base64.b64decode(error['response_body_b64']))
                self.assertEqual(error['request_id'],'error-body-id')
                for result in (success,error):self.assertGreaterEqual(result['elapsed_ns'],0)
                for job in jobs:self.assertFalse(worker.call({'op':'async_poll','job_id':job})['ok'])
                self.assertEqual([r[0] for r in server.requests if '/paused-' in r[1]],['POST','POST'])

    def test_invalid_inputs_and_outstanding_job_limit(self):
        """Purpose: bound outstanding SDK tasks and reject invalid controls; expect zero invalid-request HTTP, exactly 512 admitted jobs, and all genuine results drainable."""
        for language in LANGUAGES:
            with self.subTest(language=language),fixture_worker(language,[],backend='s3',overrides={'OSV_CONNECTIONS':'100'},handler=PausedHandler) as (worker,server):
                initialize(server)
                for request in (None,{}, {'op':'async_start'},{'op':'info'},{'op':'select'},{'op':'bucket_head','identity':'main'},{'op':'bucket_head','capture_response_body':1}):
                    self.assertFalse(worker.call({'op':'async_start','request':request})['ok'])
                for job in (None,1,'missing'):
                    self.assertFalse(worker.call({'op':'async_poll','job_id':job})['ok'])
                self.assertEqual(server.requests,[])
                self.assertEqual(worker.info['async_jobs_limit'],512)
                jobs=[]
                try:
                    for i in range(512):
                        result=worker.call({'op':'async_start','request':{'op':'bucket_head','bucket':'capacity-'+str(i)}})
                        self.assertTrue(result['ok'],result);jobs.append(result['job_id'])
                        deadline=time.monotonic()+5
                        while len(server.requests)<i+1 and time.monotonic()<deadline:time.sleep(.001)
                        self.assertGreaterEqual(len(server.requests),i+1)
                    self.assertFalse(worker.call({'op':'async_start','request':{'op':'bucket_head','bucket':'not-admitted'}})['ok'])
                finally:server.release.set()
                self.assertTrue(collect(worker,jobs.pop(0))['ok'])
                replacement=worker.call({'op':'async_start','request':{'op':'bucket_head','bucket':'capacity-reused'}})
                self.assertTrue(replacement['ok'],replacement);jobs.append(replacement['job_id'])
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results=list(pool.map(lambda job:collect(worker,job),jobs))
                self.assertTrue(all(r['ok'] for r in results),results)
                self.assertEqual(len(server.requests),513)
                self.assertFalse(any('not-admitted' in row[1] for row in server.requests))


if __name__=='__main__':unittest.main()
