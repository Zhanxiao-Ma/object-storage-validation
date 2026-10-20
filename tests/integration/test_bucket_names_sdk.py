"""Verify original bucket naming inputs, SDK transport and conservative ownership cleanup."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote,urlsplit
from osv.ceph_bucket_names import GOOD,BAD,CASES,good_name,bad_name,location,recreate
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun

XML={'Content-Type':'application/xml'}
EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
OWNER=b'<AccessControlPolicy><Owner><ID>owned-account</ID></Owner><AccessControlList/></AccessControlPolicy>'
OTHER=b'<AccessControlPolicy><Owner><ID>different-account</ID></Owner><AccessControlList/></AccessControlPolicy>'
# Independent successful ownership evidence and cleanup response documents.
RECOVERY=[(200,{},b''),(404,{},b''),(200,{},b''),(200,XML,OWNER),(200,XML,OWNER),
 (200,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b''),
 (200,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]


@unittest.skipUnless(os.environ.get('OSV_RUN_BUCKET_NAMES_TESTS')=='1','requires genuine SDK workers')
class BucketNamesWireTest(unittest.TestCase):
    """Record raw input names and feed independent ACL/list/error XML into official SDKs, never emulate bucket storage or signing."""
    def probe(self,worker,directory,workload=None):
        return Probe(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',workload or {}))

    def test_all_original_lengths_characters_and_negative_names(self):
        """Exercise every original naming vector with exact names/body/status; preserve literal negatives, generated prefixes, 60..63 boundaries and source empty V1 listing, proving owned cleanup."""
        from sdk_fixtures import fixture_worker
        fixtures=[]
        for mode,_ in GOOD:fixtures.extend([(404,{},b''),(200,{},b'')]+([(200,XML,EMPTY)] if mode=='list_long' else [])+RECOVERY)
        good_fixtures=list(fixtures)
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            fixtures=list(good_fixtures)
            for mode,_ in BAD:fixtures.extend([(404,{},b''),(404,{},b'')] if language=='java' else [(404,{},b''),(400,XML,b'<Error><Code>InvalidBucketName</Code></Error>'),(404,{},b'')])
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                p=self.probe(worker,Path(temporary),{'ceph_literal_bucket_names_authorized':True})
                for mode,_ in GOOD:
                    start=len(server.requests);good_name(p,mode);request=server.requests[start+1]
                    name=unquote(urlsplit(request[1]).path.strip('/'));self.assertEqual(request[0],'PUT')
                    if language=='java':self.assertIn(b'<LocationConstraint>ap-guangzhou</LocationConstraint>',request[3])
                    else:self.assertEqual(request[3],b'')
                    if mode.startswith('long_'):self.assertEqual(len(name),int(mode[5:]));self.assertTrue(name.endswith('a'*(int(mode[5:])-36)))
                    elif mode=='list_long':self.assertEqual(len(name),61);self.assertEqual(server.requests[start+2][0],'GET')
                    elif mode=='dns_long':self.assertEqual(len(name),63)
                    elif mode=='starts_alpha':self.assertTrue(name.startswith('aosv-'));self.assertTrue(name.endswith('foo'))
                    elif mode=='starts_digit':self.assertTrue(name.startswith('0osv-'));self.assertTrue(name.endswith('foo'))
                    elif mode=='period':self.assertTrue(name.endswith('aaa.111'))
                    elif mode=='hyphen':self.assertTrue(name.endswith('aaa-111'))
                    self.assertTrue(any(item[0]=='DELETE' and urlsplit(item[1]).path==urlsplit(request[1]).path for item in server.requests[start:]))
                expected={'short_one':'a','short_two':'aa','ip':'192.168.5.123','underscore':'foo_bar','dash_end':'foo-','dot_dot':'foo..bar','dot_dash':'foo.-bar','dash_dot':'foo-.bar'}
                for mode,_ in BAD:
                    start=len(server.requests)
                    if language=='java':
                        with self.assertRaises(AssertionError) as caught:bad_name(p,mode)
                        self.assertIn('InvalidBucketName',str(caught.exception));request=server.requests[start];self.assertEqual([item[0] for item in server.requests[start:]],['HEAD','HEAD'])
                    else:bad_name(p,mode);request=server.requests[start+1]
                    name=unquote(urlsplit(request[1]).path.strip('/'))
                    if mode=='nonalpha':self.assertTrue(name.startswith('_osv-'));self.assertEqual(len(name),37)
                    else:self.assertEqual(name,expected[mode])
                    self.assertEqual(len(server.requests)-start,2 if language=='java' else 3);self.assertFalse(any(item[0]=='DELETE' for item in server.requests[start:]))
                self.assertEqual(len(server.fixtures),0)

    def test_missing_isolation_location_and_existing_bucket_never_write(self):
        """Fixed names and location require explicit prerequisites with zero HTTP; an existing generated candidate must stop after one HEAD and never create or delete unknown state."""
        from sdk_fixtures import fixture_worker
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{},b'')],'s3') as (worker,server):
                p=self.probe(worker,Path(temporary))
                for mode in ('short_one','short_two','ip','underscore','dash_end','dot_dot','dot_dash','dash_dot'):
                    with self.assertRaises(CaseNotRun):bad_name(p,mode)
                with self.assertRaises(CaseNotRun):location(p)
                self.assertEqual(server.requests,[])
                with self.assertRaises(CaseNotRun):good_name(p,'long_60')
                self.assertEqual([item[0] for item in server.requests],['HEAD'])

    def test_unexpected_success_and_transport_ambiguity_require_owner_proof(self):
        """Unexpected successful invalid-name creation and failed ambiguous creation must purge only after independent matching owner evidence; mismatched owner leaves candidate untouched and reports manual inspection."""
        from sdk_fixtures import fixture_worker
        variants=[('unexpected',[(404,{},b''),(200,{},b'')]+RECOVERY),
         ('ambiguous',[(404,{},b''),(500,XML,b'<Error><Code>InternalError</Code></Error>')]+RECOVERY),
         ('wrong_owner',[(404,{},b''),(200,{},b'')]+RECOVERY[:4]+[(200,XML,OTHER)]+RECOVERY[9:])]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode,fixtures in variants:
                if language=='java' and mode=='unexpected':continue  # Official SDK rejects the short literal before HTTP; the C++ path exercises unexpected service success.
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                    p=self.probe(worker,Path(temporary),{'ceph_literal_bucket_names_authorized':True})
                    with self.assertRaises((AssertionError,RuntimeError)) as caught:
                        if mode=='unexpected':bad_name(p,'short_one')
                        else:good_name(p,'long_60')
                    candidate=urlsplit(server.requests[1][1]).path
                    acl_index=next(index for index,item in enumerate(server.requests) if urlsplit(item[1]).path==candidate and 'acl' in urlsplit(item[1]).query)
                    deleted=[index for index,item in enumerate(server.requests) if item[0]=='DELETE' and urlsplit(item[1]).path==candidate]
                    if mode=='wrong_owner':self.assertEqual(deleted,[]);self.assertIn('owner is unconfirmed',str(caught.exception))
                    else:self.assertEqual(len(deleted),1);self.assertGreater(deleted[0],acl_index)
                    self.assertEqual(len(server.fixtures),0)

    def test_location_real_alternate_identity_and_source_recreation_defect(self):
        """Use independent location config, genuine alternate signer and exact 409 conflict; source same-owner error must retain its invalid e.status access instead of being repaired into success."""
        from sdk_fixtures import fixture_worker
        fixtures=[(404,{},b''),(200,{},b''),(200,XML,b'<LocationConstraint>source-location</LocationConstraint>')]+RECOVERY
        fixtures+=[(404,{},b''),(200,{},b''),(409,XML,b'<Error><Code>BucketAlreadyExists</Code></Error>')]+RECOVERY
        fixtures+=[(404,{},b''),(200,{},b''),(409,XML,b'<Error><Code>BucketAlreadyOwnedByYou</Code></Error>')]+RECOVERY
        fixtures+=[(404,{},b''),(200,{},b''),(200,{},b'')]+RECOVERY
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3',overrides={'OSV_ALT_ACCESS_KEY':'alt-key','OSV_ALT_SECRET_KEY':'alt-secret'}) as (worker,server):
                p=self.probe(worker,Path(temporary),{'ceph_api_name':'source-location'})
                location(p);self.assertIn(b'<LocationConstraint>source-location</LocationConstraint>',server.requests[1][3])
                start=len(server.requests);recreate(p,True)
                headers={key.lower():value for key,value in server.requests[start+2][2].items()};self.assertIn('Credential=alt-key/',headers['authorization'])
                with self.assertRaises(AttributeError) as caught:recreate(p)
                self.assertIn("source ClientError has no attribute 'status'",str(caught.exception))
                recreate(p);self.assertEqual(len(server.fixtures),0)


    def test_lost_worker_after_create_identifies_uncertain_candidate_without_delete(self):
        """Delay an actual CreateBucket HTTP response until the genuine worker deadline kills it; cleanup cannot be proven, so identify the candidate for manual inspection and never issue an unverified deletion."""
        from sdk_fixtures import fixture_worker,FixtureHandler
        import time
        class SlowCreate(FixtureHandler):
            """Inject an independent response delay after recording the real SDK create destination; no storage operation or SDK result is simulated."""
            def respond(self):
                if self.command=='PUT':
                    self.server.delayed_candidates.append(self.path);time.sleep(.7)
                return super().respond()
            do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond
        fixtures=[(404,{},b''),(404,{},b''),(200,{},b'')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3',handler=SlowCreate) as (worker,server):
                server.delayed_candidates=[]
                worker.call({'op':'bucket_head','bucket':'sdk-warmup'});worker.timeout=.3
                with self.assertRaises(RuntimeError) as caught:good_name(self.probe(worker,Path(temporary)),'long_60')
                self.assertEqual(len(server.delayed_candidates),1)
                self.assertIn('manually inspect '+server.delayed_candidates[0].strip('/'),str(caught.exception))
                self.assertTrue(all(item[0]!='DELETE' for item in server.requests))


@unittest.skipUnless(os.environ.get('OSV_RUN_BUCKET_NAMES_TESTS')=='1','requires genuine SDK workers and isolated local service')
class BucketNamesLiveTest(unittest.TestCase):
    """Observe original naming oracles on an isolated service and verify no generated or literal candidate survives cleanup."""
    def test_original_names_default_and_dedicated_authorized_cleanup(self):
        """Run default precise prerequisites then all authorized literal names on a fresh dedicated service; preserve original source failures and verify the independently created sentinel survives each language and run."""
        import boto3,json,logging
        from unittest.mock import patch
        from local_moto import LocalMotoServer
        from osv.worker import Worker
        from osv.suites import run_cases
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        server=LocalMotoServer();server.start()
        try:
            host,port=server.get_host_and_port();endpoint=f'http://{host}:{port}';root=Path(__file__).resolve().parents[2]
            with patch.dict(os.environ,{'OSV_ACCESS_KEY':'local-test','OSV_SECRET_KEY':'local-test','OSV_SESSION_TOKEN':'','OSV_ENDPOINT':endpoint,
                'OSV_REGION':'us-east-1','OSV_BUCKET':'osv-names-sentinel','OSV_PATH_STYLE':'true','OSV_RETRIES':'0','OSV_ALT_ACCESS_KEY':'','OSV_ALT_SECRET_KEY':''}):
                client=boto3.client('s3',endpoint_url=endpoint,region_name='us-east-1',aws_access_key_id='local-test',aws_secret_access_key='local-test')
                client.create_bucket(Bucket='osv-names-sentinel')
                for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
                    for authorized in (False,True):
                        with self.subTest(language=language,authorized=authorized),tempfile.TemporaryDirectory() as temporary:
                            command=['java','-Xmx256m','-jar',str(root/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3'] if language=='java' else [str(root/'drivers/cpp/build/osv-cpp-s3')]
                            worker=Worker({'command':command,'language':language,'api':'s3'},30)
                            try:
                                directory=Path(temporary);workload={'ceph_literal_bucket_names_authorized':True,'ceph_api_name':'eu-west-1'} if authorized else {}
                                rows=run_cases(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',workload),CASES)
                                expected=(12,9,1) if language=='java' and authorized else (14,7,1) if authorized else (11,1,10)
                                self.assertEqual(tuple(sum(row['status']==status for row in rows) for status in ('pass','fail','not_run')),expected)
                                self.assertEqual(len(rows),22)
                                if not authorized:
                                    for row in rows:
                                        if row['case'] in {'ceph_names_'+mode for mode in ('short_one','short_two','ip','underscore','dash_end','dot_dot','dot_dash','dash_dot')}:
                                            self.assertEqual(row['status'],'not_run');self.assertEqual(row['missing_requirements'],['workload.ceph_literal_bucket_names_authorized=true'])
                                self.assertEqual({item['Name'] for item in client.list_buckets()['Buckets']},{'osv-names-sentinel'})
                            finally:worker.close()
        finally:server.stop()
