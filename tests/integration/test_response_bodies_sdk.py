"""Independent bytes enter actual SDK unmarshallers and the migrated source consumers."""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe
from osv.ceph_response_bodies import account_usage,request_id


QUOTAS=b'<Usage><Summary><QuotaMaxBytes>-1</QuotaMaxBytes><QuotaMaxBuckets>1000</QuotaMaxBuckets><QuotaMaxObjCount>-1</QuotaMaxObjCount><QuotaMaxBytesPerBucket>-1</QuotaMaxBytesPerBucket><QuotaMaxObjCountPerBucket>-1</QuotaMaxObjCountPerBucket></Summary></Usage>'


def context(worker,directory):
    d=Path(directory);return Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{}))


@unittest.skipUnless(os.environ.get('OSV_RUN_RESPONSE_BODY_TESTS')=='1','enable actual SDK body capture contracts')
class ResponseBodySdkTest(unittest.TestCase):
    """Observe actual success/error wire data independently from typed SDK metadata."""
    def test_original_usage_source_and_wrong_actual_quota(self):
        """Feed original usage XML and one independent wrong quota; expect all source fields checked and the bare usage query signed by the actual SDK."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for bad in (False,True):
                payload=QUOTAS.replace(b'<QuotaMaxBuckets>1000',b'<QuotaMaxBuckets>999') if bad else QUOTAS
                with self.subTest(language=language,bad=bad),tempfile.TemporaryDirectory() as d,fixture_worker(language,[(200,{'Content-Type':'application/xml'},payload)],'s3') as (worker,server):
                    if bad:
                        with self.assertRaises(AssertionError):account_usage(context(worker,d))
                    else:account_usage(context(worker,d))
                    self.assertEqual([(method,path) for method,path,_,_ in server.requests],[('GET','/?usage' if language=='java' else '/?usage=')])
                    self.assertIn('authorization',{k.lower():v for k,v in server.requests[0][2].items()});self.assertFalse(server.fixtures)

    def test_original_missing_object_source_preserves_raw_error_and_typed_request_id(self):
        """Return actual S3 error XML and independent request-ID header; expect original body-vs-SDK metadata assertion plus actual cleanup and no capture leaking into later calls."""
        body=b'<Error><Code>NoSuchKey</Code><Message>local-test</Message><RequestId>wire-request-id-123</RequestId></Error>'
        docs=[(404,{},b''),(200,{},b''),(404,{'Content-Type':'application/xml','x-amz-request-id':'wire-request-id-123'},body),(200,{},b''),(200,{'Content-Type':'application/xml'},b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'),(200,{},b''),(404,{},b'')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as d,fixture_worker(language,docs,'s3') as (worker,server):
                observed=[];call=worker.call
                def record(q):
                    result=call(q);observed.append((q,result));return result
                worker.call=record
                request_id(context(worker,d))
                raw=observed[2][1];self.assertEqual(base64.b64decode(raw['response_body_b64']),body);self.assertEqual(raw['request_id'],'wire-request-id-123');self.assertEqual(raw['status_code'],404)
                diagnostic=worker.secret_registry.redact(json.dumps(raw));self.assertNotIn(base64.b64encode(body).decode(),diagnostic);self.assertNotIn('local-test',diagnostic)
                self.assertTrue(all('response_body_b64' not in r and 'request_id' not in r for q,r in observed if not q.get('capture_response_body')))
                self.assertFalse(server.fixtures)

    def test_metadata_retains_official_sdk_body_preference_when_header_differs(self):
        """Provide different XML and HTTP-header IDs; expect genuine SDK metadata body preference, the unchanged source equality assertion and full cleanup."""
        error=b'<Error><Code>NoSuchKey</Code><Message>missing</Message><RequestId>body-request-id</RequestId></Error>'
        docs=[(404,{},b''),(200,{},b''),(404,{'Content-Type':'application/xml','x-amz-request-id':'sdk-header-request-id'},error),(200,{},b''),(200,{'Content-Type':'application/xml'},b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'),(200,{},b''),(404,{},b'')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as d,fixture_worker(language,docs,'s3') as (worker,server):
                observed=[];call=worker.call
                def record(q):
                    result=call(q);observed.append((q,result));return result
                worker.call=record
                request_id(context(worker,d))
                result=observed[2][1]
                self.assertEqual(result['request_id'],'body-request-id')
                self.assertNotEqual(result['request_id'],'sdk-header-request-id')
                self.assertFalse(server.fixtures)

    def test_captured_success_bytes_still_reach_real_sdk_file_consumer(self):
        """Send independent binary HTTP content through the real SDK and capture interceptor; expect unchanged bytes both in the genuine SDK output file and bounded raw evidence."""
        payload=bytes(range(256))+b"independent-http-body"
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[(200,{'x-amz-request-id':'actual-success-id'},payload)],'s3') as (worker,server):
                output=Path(directory)/"actual-sdk-output"
                result=worker.call({'op':'get','key':'source-bytes','output_path':str(output),'capture_response_body':True})
                self.assertTrue(result['ok'],result);self.assertEqual(output.read_bytes(),payload)
                self.assertEqual(base64.b64decode(result['response_body_b64']),payload)
                self.assertEqual(result['request_id'],'actual-success-id');self.assertFalse(server.fixtures)

    def test_bounded_capture_and_invalid_flags_do_not_corrupt_or_leak_other_responses(self):
        """Capture a real oversized response then make an ordinary SDK call; expect explicit capture limit diagnostics, strict flags without HTTP, and complete per-RPC reset."""
        oversized=b'<ListAllMyBucketsResult>'+b' '*(512*1024)+b'</ListAllMyBucketsResult>'
        docs=[(200,{'Content-Type':'application/xml'},oversized),(200,{'Content-Type':'application/xml'},b'<ListAllMyBucketsResult/>')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),fixture_worker(language,docs,'s3') as (worker,server):
                for invalid in (None,1,0,'true',[],{}):
                    result=worker.call({'op':'api.ListBuckets','input':{},'capture_response_body':invalid});self.assertFalse(result['ok'])
                result=worker.call({'op':'concurrent_requests','capture_response_body':True,'requests':[{'op':'api.ListBuckets','input':{}}]});self.assertFalse(result['ok']);self.assertEqual(server.requests,[])
                nested=worker.call({'op':'concurrent_requests','requests':[{'op':'api.ListBuckets','input':{},'capture_response_body':True}]});self.assertFalse(nested['ok']);self.assertEqual(server.requests,[])
                result=worker.call({'op':'api.ListBuckets','input':{},'capture_response_body':True});self.assertIn('512 KiB',result.get('response_body_capture_error',''));self.assertNotIn('response_body_b64',result)
                clean=worker.call({'op':'api.ListBuckets','input':{}});self.assertTrue(clean['ok'],clean);self.assertNotIn('response_body_b64',clean);self.assertNotIn('response_body_capture_error',clean);self.assertEqual(len(server.requests),2)
