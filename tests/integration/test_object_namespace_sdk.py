"""Use genuine SDKs and MinIO to verify prefix requests, opaque cursors and history cleanup."""
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
from sdk_fixtures import fixture_worker
from osv.bucket_scope import BucketScope
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_list_exact import CASES as LIST_CASES
from osv.s3_delete_compatibility import CASES as DELETE_CASES

ROOT=Path(__file__).resolve().parents[2]


class ExplicitProxyTest(unittest.TestCase):
    """Observe real SDK proxy routing through a stateless raw-response HTTP fixture."""
    def test_explicit_proxy_is_used_by_both_real_s3_clients(self):
        """Use an unresolvable SDK endpoint with a loopback proxy; expect actual HEAD success at that proxy and explicit metadata."""
        for language in ('java','cpp'):
            with self.subTest(language=language),fixture_worker(language,[(200,{},b'')],backend='s3') as (worker,server):
                target=dict(worker.target)
                target['env']={**os.environ,**worker.target.get('env',{}),
                    'OSV_ENDPOINT':'http://storage.fixture.invalid',
                    'OSV_HTTP_PROXY':f'http://127.0.0.1:{server.server_port}'}
                actual=Worker(target,15)
                try:
                    self.assertTrue(actual.info['explicit_http_proxy'])
                    result=actual.call({'op':'bucket_head'})
                    self.assertTrue(result['ok'],result)
                    self.assertEqual(len(server.requests),1)
                    self.assertEqual(server.requests[0][0],'HEAD')
                    self.assertTrue(server.requests[0][1].startswith('http://storage.fixture.invalid/'))
                finally:actual.close()

    def test_bad_proxy_urls_fail_before_storage_requests(self):
        """Provide credentials, path, unsupported scheme and invalid port in proxy URLs; expect local rejection and no HTTP request."""
        for language in ('java','cpp'):
            for proxy in ('http://user:password@127.0.0.1:1','http://127.0.0.1:1/path',
                          'socks5://127.0.0.1:1','http://127.0.0.1:0','http://127.0.0.1:65536'):
                with self.subTest(language=language,proxy=proxy),fixture_worker(language,[],backend='s3') as (worker,server):
                    target=dict(worker.target)
                    target['env']={**os.environ,'OSV_HTTP_PROXY':proxy}
                    with self.assertRaises(RuntimeError):Worker(target,15)
                    self.assertEqual(server.requests,[])


@unittest.skipUnless(os.environ.get('OSV_RUN_SCOPE_TESTS')=='1','requires local Docker MinIO and genuine SDK binaries')
class PrefixScopeSdkTest(unittest.TestCase):
    """Prove existing-bucket object fixtures preserve unrelated objects and actual historical IDs."""
    @classmethod
    def setUpClass(cls):
        """Start loopback-only resource-limited MinIO; expect an isolated real S3 service with temporary local credentials."""
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
        cls.endpoint=f'http://127.0.0.1:{port}'
        cls.container='osv-scope-'+uuid.uuid4().hex[:12]
        cls.password=secrets.token_urlsafe(24)
        environment={**os.environ,'MINIO_ROOT_USER':'osvlocal','MINIO_ROOT_PASSWORD':cls.password}
        subprocess.run(['docker','run','-d','--name',cls.container,'--network','host','--cpus=2','--memory=2g','--memory-swap=2g',
            '-e','MINIO_ROOT_USER','-e','MINIO_ROOT_PASSWORD','-e','MINIO_BROWSER=off',
            'osv/minio:RELEASE.2025-02-03T21-03-04Z','server','--address',f'127.0.0.1:{port}','/data'],env=environment,check=True,capture_output=True)
        for attempt in range(60):
            try:urllib.request.urlopen(cls.endpoint+'/minio/health/live',timeout=2).close();break
            except OSError:
                if attempt==59:raise
                time.sleep(.2)

    @classmethod
    def tearDownClass(cls):
        """Remove only this fixture's container and anonymous volume; expect no persistent local test service."""
        subprocess.run(['docker','rm','-fv',cls.container],check=True,capture_output=True)

    def target(self,language,bucket):
        command=(['java','-Xmx256m','-XX:ActiveProcessorCount=2','-jar',str(ROOT/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3']
                 if language=='java' else [str(ROOT/'drivers/cpp/build/osv-cpp-s3')])
        return {'command':command,'language':language,'api':'s3','env':{
            'OSV_ENDPOINT':self.endpoint,'OSV_BUCKET':bucket,'OSV_REGION':'us-east-1','OSV_PATH_STYLE':'true',
            'OSV_ACCESS_KEY':'osvlocal','OSV_SECRET_KEY':self.password,'OSV_SESSION_TOKEN':'',
            'AWS_EC2_METADATA_DISABLED':'true','OSV_REQUEST_CHECKSUM_CALCULATION':'when_required',
            'OSV_RETRIES':'0','OSV_HTTP_PROXY':'','http_proxy':'','https_proxy':'','HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','all_proxy':''}}

    def test_real_listing_delete_and_existing_version_history_are_prefix_confined(self):
        """Feed genuine SDK writes/tokens/version IDs into genuine consumers; expect no fixture bucket mutation and preservation of unrelated objects/history."""
        for language in ('java','cpp'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);bucket='osv-'+uuid.uuid4().hex
                worker=Worker(self.target(language,bucket),30)
                original=worker.call
                try:
                    self.assertTrue(original({'op':'bucket_create_raw'})['ok'])
                    body=directory/'body';output=directory/'read';body.write_bytes(b'independent sentinel')
                    self.assertTrue(original({'op':'put','key':'sentinel','body_path':str(body)})['ok'])
                    requests=[]
                    def observe(request):
                        requests.append(request)
                        self.assertFalse(request['op'].startswith('bucket_'),request)
                        return original(request)
                    worker.call=observe
                    ctx=Context(worker,'osv/'+uuid.uuid4().hex+'/',directory,b'payload',body,output,
                                {'object_scope':'prefix','bucket_management':False})
                    selected={'ceph_exact_list_v1_delimiter_basic','ceph_exact_list_v2_continuationtoken_both',
                              'ceph_exact_list_v2_fetchowner_notempty','ceph_exact_list_v2_encoding_basic'}
                    cases=[c for c in LIST_CASES if c.name in selected]+[c for c in DELETE_CASES if c.name in {'s3_delete_many_quiet','s3_delete_many_verbose'}]
                    worker.call=original
                    baseline=run_cases(replace(ctx,workload={'object_scope':'bucket','bucket_management':True}),cases)
                    worker.call=observe
                    rows=run_cases(ctx,cases)
                    # Encoding/FetchOwner expose existing SDK/service differences; prefix isolation must retain the same verdicts.
                    self.assertEqual({r['case']:r['status'] for r in rows},{r['case']:r['status'] for r in baseline},(baseline,rows))
                    required={'ceph_exact_list_v1_delimiter_basic','ceph_exact_list_v2_continuationtoken_both','s3_delete_many_quiet','s3_delete_many_verbose'}
                    self.assertTrue(all(row['status']=='pass' for row in rows if row['case'] in required),rows)
                    self.assertTrue(original({'op':'get','key':'sentinel','output_path':str(output)})['ok'])
                    self.assertEqual(output.read_bytes(),b'independent sentinel')
                    self.assertEqual(original({'op':'list','prefix':ctx.prefix})['keys'],[])
                    with BucketScope(Probe(replace(ctx,object_namespace=True,scope_state={}))) as typed:
                        typed.put('typed-object',b'actual typed API producer')
                        typed.call('api.PutObjectAcl',input={'Key':'typed-object','ACL':'private'})
                        observed=typed.call('api.GetObjectAcl',input={'Key':'typed-object'})
                        self.assertEqual(observed['status_code'],200)
                        self.assertIn('Owner',observed['data'])
                        typed.call('get',key='typed-object',output_path=str(output))
                        self.assertEqual(output.read_bytes(),b'actual typed API producer')
                        sent=next(r for r in requests if r['op']=='api.PutObjectAcl')
                        self.assertEqual(sent['input']['Key'],typed.key('typed-object'))
                    # Versioning is enabled only by fixture setup, never by the production object scope.
                    self.assertTrue(original({'op':'versioning_put','status':'Enabled'})['ok'])
                    body.write_bytes(b'outside historical version')
                    outside=original({'op':'put_version','key':'outside-history','body_path':str(body)})['version_id']
                    probe=Probe(replace(ctx,object_namespace=True,scope_state={}))
                    with BucketScope(probe) as scope:
                        scope.put('history',b'actual first version')
                        first=scope.call('head_version',key='history')['version_id']
                        scope.put('history',b'actual second version')
                        scope.call('get_version',key='history',version_id=first,output_path=str(output))
                        self.assertEqual(output.read_bytes(),b'actual first version')
                        scope.call('delete_version',key='history')
                    self.assertTrue(scope.cleanup_completed)
                    listing=original({'op':'versions_list','prefix':scope.prefix})
                    self.assertEqual(listing['versions'],[]);self.assertEqual(listing['delete_markers'],[])
                    original({'op':'get_version','key':'outside-history','version_id':outside,'output_path':str(output)})
                    self.assertEqual(output.read_bytes(),b'outside historical version')
                finally:
                    worker.call=original
                    listing=original({'op':'versions_list'})
                    for row in listing.get('versions',[])+listing.get('delete_markers',[]):
                        original({'op':'delete_version','key':row['key'],'version_id':row['version_id']})
                    for key in original({'op':'list','prefix':''}).get('keys',[]):original({'op':'delete','key':key})
                    original({'op':'bucket_delete'})
                    worker.close()

    def test_authenticated_post_forms_feed_real_prefixed_objects_into_sdk_readback(self):
        """Send actual signed POST forms with prefixed policy/key fields; expect real SDK byte verification and no object left outside the run prefix."""
        from osv.ceph_raw_http import CASES
        for language in ('java','cpp'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);bucket='osv-'+uuid.uuid4().hex
                worker=Worker(self.target(language,bucket),30)
                try:
                    self.assertTrue(worker.call({'op':'bucket_create_raw'})['ok'])
                    ctx=Context(worker,'osv/'+uuid.uuid4().hex+'/',directory,b'payload',directory/'body',directory/'read',
                        {'object_scope':'prefix','bucket_management':False,
                         'ceph_http':{'endpoint':self.endpoint,'addressing_style':'path'}})
                    names={'ceph_raw_post_authenticated','ceph_raw_post_filename','ceph_raw_post_metadata'}
                    rows=run_cases(ctx,[c for c in CASES if c.name in names])
                    self.assertEqual(len(rows),3)
                    self.assertTrue(all(row['status']=='pass' for row in rows),rows)
                    self.assertEqual(worker.call({'op':'list','prefix':''})['keys'],[])
                finally:
                    for key in worker.call({'op':'list','prefix':''}).get('keys',[]):worker.call({'op':'delete','key':key})
                    worker.call({'op':'bucket_delete'})
                    worker.close()

    def test_denied_bucket_versioning_read_does_not_gate_object_cases_or_leak_markers(self):
        """Deny only the optional config probe; expect real object HEAD metadata to select cleanup for disabled/enabled/suspended versioning, including real null versions and rejected writes without marker creation."""
        for language in ('java','cpp'):
            for enabled in (False,True,"Suspended"):
                with self.subTest(language=language,versioning=enabled),tempfile.TemporaryDirectory() as temporary:
                    directory=Path(temporary);bucket='osv-'+uuid.uuid4().hex
                    worker=Worker(self.target(language,bucket),30);original=worker.call
                    try:
                        self.assertTrue(original({'op':'bucket_create_raw'})['ok'])
                        if enabled:self.assertTrue(original({'op':'versioning_put','status':'Enabled'})['ok'])
                        if enabled=='Suspended':self.assertTrue(original({'op':'versioning_put','status':'Suspended'})['ok'])
                        def deny_config(request):
                            if request['op']=='versioning_get':
                                return {'ok':False,'status_code':403,'error_code':'AccessDenied','error':'optional bucket-config permission denied'}
                            return original(request)
                        worker.call=deny_config
                        ctx=Context(worker,'osv/'+uuid.uuid4().hex+'/',directory,b'payload',directory/'body',directory/'read',
                                    {'object_scope':'prefix','bucket_management':False},object_namespace=True,scope_state={})
                        with BucketScope(Probe(ctx)) as scope:
                            scope.put('object',b'actual first producer')
                            scope.put('object',b'actual replacement producer')
                            scope.call('get',key='object',output_path=str(ctx.output))
                            self.assertEqual(ctx.output.read_bytes(),b'actual replacement producer')
                            if enabled is True:self.assertTrue(scope.versioned)
                        with BucketScope(Probe(ctx)) as rejected:
                            ctx.body.write_bytes(b'actual independent bytes')
                            response=rejected.raw('put_http',key='bad',body_path=str(ctx.body),wire_headers={'Content-MD5':'invalid-md5'})
                            self.assertFalse(response['ok'])
                        with BucketScope(Probe(ctx)) as deleted_missing:
                            deleted_missing.call('delete',key='never-created')
                        self.assertTrue(deleted_missing.cleanup_completed)
                        self.assertTrue(scope.cleanup_completed);self.assertTrue(rejected.cleanup_completed)
                        versions=original({'op':'versions_list','prefix':ctx.prefix})
                        self.assertEqual(versions['versions'],[]);self.assertEqual(versions['delete_markers'],[])
                        if enabled:
                            # A denied history inventory must never be mistaken for confirmed cleanup.
                            blocked=BucketScope(Probe(ctx))
                            def deny_history(request):
                                if request['op']=='versions_list':return {'ok':False,'status_code':403,'error_code':'AccessDenied'}
                                return deny_config(request)
                            worker.call=deny_history
                            with self.assertRaisesRegex(RuntimeError,'cleanup failed'):
                                with blocked:
                                    blocked.put('only-history',b'actual retained version')
                                    blocked.call('delete',key='only-history')
                            self.assertFalse(blocked.cleanup_completed)
                            retained=original({'op':'versions_list','prefix':blocked.prefix})
                            if enabled is True:self.assertTrue(retained['versions'])
                            self.assertTrue(retained['delete_markers'])
                            worker.call=deny_config
                            blocked.__exit__(None,None,None)
                            self.assertTrue(blocked.cleanup_completed)
                    finally:
                        worker.call=original
                        listed=original({'op':'versions_list'})
                        for row in listed.get('versions',[])+listed.get('delete_markers',[]):original({'op':'delete_version','key':row['key'],'version_id':row['version_id']})
                        original({'op':'bucket_delete'});worker.close()
