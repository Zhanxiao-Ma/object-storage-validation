"""Independent HTTP documents validate original extension queries through real signed SDK requests."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from xml.sax.saxutils import escape
from osv.ceph_query_edges import head_usage,unordered,invalid_maxkeys,response_headers,list_buckets_paginated
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun


@unittest.skipUnless(os.environ.get('OSV_RUN_QUERY_EDGES_TESTS')=='1','requires actual SDK workers')
class QueryEdgesWireTest(unittest.TestCase):
    """Feed original request inputs through official SDK serializers/signers into independent HTTP response oracles."""
    def probe(self,worker,directory):
        return Probe(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',{}))
    def test_usage_headers_and_private_response_overrides(self):
        """Keep read-stats query scope, original count/quota headers, private empty body and exact six response parameters; independent headers drive all source assertions."""
        from sdk_fixtures import fixture_worker
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        fixtures=[]
        cleanup=[(200,{},b''),(200,{},b''),(200,{'Content-Type':'application/xml'},empty),(204,{},b''),(404,{},b'')]
        # Usage: owned bucket, foo/3 bytes, actual headers, then cleanup.
        fixtures.extend([(404,{},b''),(200,{},b''),(200,{},b''),(200,{'x-rgw-object-count':'1','x-rgw-bytes-used':'3','x-rgw-quota-max-buckets':'1000'},b'')]+cleanup)
        # Extended: empty counts, original foo/bar/baz writes, 3/9 headers.
        fixtures.extend([(404,{},b''),(200,{},b''),(200,{'x-rgw-object-count':'0','x-rgw-bytes-used':'0'},b'')]+[(200,{},b'')]*3+[(200,{'x-rgw-object-count':'3','x-rgw-bytes-used':'9'},b'')]+cleanup[:1]+[(204,{},b'')]*3+cleanup[2:])
        headers={'Content-Type':'foo/bar','Content-Disposition':'bla','Content-Language':'esperanto','Content-Encoding':'aaa','Cache-Control':'no-cache'}
        fixtures.extend([(404,{},b''),(200,{},b''),(200,{},b''),(200,headers,b'')]+cleanup)
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                directory=Path(temporary);p=self.probe(worker,directory)
                head_usage(p);head_usage(p,True);response_headers(p)
                stats=[request for request in server.requests if parse_qs(urlsplit(request[1]).query).get('read-stats')==['true']]
                self.assertEqual(len(stats),3);self.assertTrue(all(request[0]=='HEAD' for request in stats))
                overrides=[request for request in server.requests if 'response-cache-control=' in request[1]];self.assertEqual(len(overrides),1)
                self.assertEqual(parse_qs(urlsplit(overrides[0][1]).query),{'response-cache-control':['no-cache'],'response-content-disposition':['bla'],'response-content-encoding':['aaa'],'response-content-language':['esperanto'],'response-content-type':['foo/bar'],'response-expires':['123']})
                self.assertEqual(len(server.fixtures),0)
                for request in stats+overrides:self.assertIn('authorization',{key.lower() for key in request[2]})

    def test_unordered_marker_handoff_v2_registration_defect_and_invalid_maxkeys(self):
        """Independent nonlexical V1 pages and opaque markers enter the real SDK consumer; preserve original V2 callback defect and exact malformed-query errors without source repair."""
        from sdk_fixtures import fixture_worker
        xml={'Content-Type':'application/xml'};empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        def page(keys):return b'<ListBucketResult>'+b''.join(('<Contents><Key>'+escape(key)+'</Key></Contents>').encode() for key in keys)+b'</ListBucketResult>'
        fixtures=[]
        for version in (1,2):
            fixtures.extend([(404,{},b''),(200,{},b'')]+[(200,{},b'')]*26)
            # First source sort() assertion intentionally never compares actual keys.
            fixtures.extend([(200,xml,page(['unrelated'+str(i) for i in range(26)])),(200,xml,page(['p'+str(i) for i in range(5)])),
             (200,xml,page(['z','y','x','w','v','marker &+/%'])),(200,xml,page(['a','b','c','d','e','f'])),(400,xml,b'<Error><Code>InvalidArgument</Code></Error>')])
            fixtures.extend([(200,{},b'')]+[(204,{},b'')]*26+[(200,xml,empty),(204,{},b''),(404,{},b'')])
        fixtures.extend([(404,{},b''),(200,{},b'')]+[(200,{},b'')]*4+[(400,xml,b'<Error><Code>InvalidArgument</Code></Error>'),(200,{},b'')]+[(204,{},b'')]*4+[(200,xml,empty),(204,{},b''),(404,{},b'')])
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                directory=Path(temporary);p=self.probe(worker,directory)
                for version in (1,2):
                    start=len(server.requests);unordered(p,version)
                    listing=server.requests[start+28:start+33];queries=[parse_qs(urlsplit(request[1]).query,keep_blank_values=True) for request in listing]
                    for query in queries[:4]:self.assertEqual(query.get('allow-unordered'),['true'] if version==1 else None)
                    self.assertEqual(queries[3]['marker' if version==1 else 'start-after'],['marker &+/%'])
                    self.assertEqual(queries[4]['allow-unordered'],['true']);self.assertEqual(queries[4]['delimiter'],['/'])
                invalid_maxkeys(p)
                malformed=[request for request in server.requests if parse_qs(urlsplit(request[1]).query).get('max-keys')==['blah']]
                self.assertEqual(len(malformed),1);self.assertEqual(len(server.fixtures),0)

    def test_bucket_pagination_isolated_account_and_exact_token_handoff(self):
        """Drive empty/one/two account states and reserved-character continuation tokens through real SDK models; a nonempty initial account must stop with not_run and zero writes."""
        from sdk_fixtures import fixture_worker
        from unittest.mock import patch
        import uuid
        xml={'Content-Type':'application/xml'};empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        ids=[uuid.UUID(int=1),uuid.UUID(int=2)];names=['osv-'+value.hex for value in ids]
        def buckets(name=None,token=None):return ('<ListAllMyBucketsResult><Buckets>'+('' if name is None else '<Bucket><Name>'+name+'</Name></Bucket>')+'</Buckets>'+('' if token is None else '<ContinuationToken>'+token+'</ContinuationToken>')+'</ListAllMyBucketsResult>').encode()
        fixtures=[(200,xml,buckets()),(404,{},b''),(200,{},b''),(200,xml,buckets(names[0])),(404,{},b''),(200,{},b''),(200,xml,buckets(names[0],'opaque &amp;+/%')),(200,xml,buckets(names[1]))]
        fixtures+=([(200,{},b''),(200,xml,empty),(204,{},b''),(404,{},b'')]*2)
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                p=self.probe(worker,Path(temporary))
                if 'list_buckets_pagination' not in worker.capabilities:
                    with self.assertRaises(CaseNotRun):list_buckets_paginated(p)
                    self.assertEqual(server.requests,[]);continue
                with patch('osv.bucket_scope.uuid.uuid4',side_effect=ids):list_buckets_paginated(p)
                self.assertEqual(parse_qs(urlsplit(server.requests[7][1]).query),{'max-buckets':['1'],'continuation-token':['opaque &+/%']})
                self.assertEqual(len(server.fixtures),0)
            with self.subTest(language=language,nonempty=True),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,xml,buckets('unrelated'))],'s3') as (worker,server):
                with self.assertRaises(CaseNotRun):list_buckets_paginated(self.probe(worker,Path(temporary)))
                self.assertEqual(len(server.requests),1);self.assertEqual(server.requests[0][0],'GET')


@unittest.skipUnless(os.environ.get('OSV_RUN_QUERY_EDGES_TESTS')=='1','requires actual SDK workers and isolated local service')
class QueryEdgesLiveTest(unittest.TestCase):
    """Retain real local-service differences under all seven source oracles and prove account/bucket safety independently of HTTP fixtures."""
    def test_original_query_cases_preserve_service_outcomes_and_cleanup(self):
        """Run all source cases with real SDKs and an owned sentinel bucket; record exact source failures while verifying unrelated state survives and scoped data is removed."""
        import boto3,json,logging
        from unittest.mock import patch
        from local_moto import LocalMotoServer
        from osv.ceph_query_edges import CASES
        from osv.suites import run_cases
        from osv.worker import Worker
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        server=LocalMotoServer();server.start()
        try:
            host,port=server.get_host_and_port();endpoint=f'http://{host}:{port}'
            root=Path(__file__).resolve().parents[2]
            with patch.dict(os.environ,{'OSV_ACCESS_KEY':'local-test','OSV_SECRET_KEY':'local-test','OSV_SESSION_TOKEN':'','OSV_ENDPOINT':endpoint,
                'OSV_REGION':'us-east-1','OSV_BUCKET':'osv-query-sentinel','OSV_PATH_STYLE':'true','OSV_RETRIES':'0'}):
                client=boto3.client('s3',endpoint_url=endpoint,region_name='us-east-1',aws_access_key_id='local-test',aws_secret_access_key='local-test')
                client.create_bucket(Bucket='osv-query-sentinel')
                for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
                    with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                        command=['java','-Xmx256m','-jar',str(root/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3'] if language=='java' else [str(root/'drivers/cpp/build/osv-cpp-s3')]
                        worker=Worker({'command':command,'language':language,'api':'s3'},30)
                        try:
                            directory=Path(temporary);rows=run_cases(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',{}),CASES)
                            self.assertEqual([row['status'] for row in rows],['fail']*6+['not_run'])
                            self.assertEqual(len(rows),7)
                            self.assertEqual(rows[-1]['status'],'not_run')
                            if 'list_buckets_pagination' in worker.capabilities:self.assertEqual(rows[-1]['missing_requirements'],['account containing no buckets'])
                            else:self.assertEqual(rows[-1]['missing_operations'],['list_buckets_pagination'])
                            self.assertEqual({item['Name'] for item in client.list_buckets()['Buckets']},{'osv-query-sentinel'})
                        finally:worker.close()
        finally:server.stop()
