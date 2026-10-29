"""Independent wire documents cover remaining raw grant headers and Unicode URI errors."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote,urlsplit
from osv.ceph_metadata_tail import CASES,header_grants,unreadable
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun

XML={'Content-Type':'application/xml'}
EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
PERMISSIONS=('READ','WRITE','READ_ACP','WRITE_ACP','FULL_CONTROL')


def acl_xml(wrong=False):
    """Provide independent literal ACL wire documents; wrong display name exercises the original consumer, never a substitute ACL implementation."""
    name='wrong-display' if wrong else 'independent-display'
    return ('<AccessControlPolicy><Owner><ID>main-owner</ID></Owner><AccessControlList>'+''.join('<Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>independent-id</ID><DisplayName>'+name+'</DisplayName></Grantee><Permission>'+permission+'</Permission></Grant>' for permission in PERMISSIONS)+'</AccessControlList></AccessControlPolicy>').encode()


@unittest.skipUnless(os.environ.get('OSV_RUN_METADATA_TAIL_TESTS')=='1','requires real SDK workers')
class MetadataTailWireTest(unittest.TestCase):
    """Feed literal ACL and URI error documents into actual SDK response parsers and source consumers, with genuine independent alternate credentials."""
    def probe(self,worker,directory,identities=True):
        workload={'identities':{'alt':{'canonical_id':'independent-id','display_name':'independent-display'}}} if identities else {}
        return Probe(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',workload))

    def test_all_grant_headers_original_acl_fields_and_real_alternate_writes(self):
        """Preserve all five unquoted id= headers and source ACL field assertions; foo/bar and cleanup ACL must use the actual alt signer, and the owned bucket must be deleted."""
        from sdk_fixtures import fixture_worker
        fixtures=[(404,{},b''),(200,{},b''),(200,XML,acl_xml()),(200,{},b''),(200,{},b''),
         (200,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3',overrides={'OSV_ALT_ACCESS_KEY':'alt-key','OSV_ALT_SECRET_KEY':'alt-secret'}) as (worker,server):
                result=header_grants(self.probe(worker,Path(temporary)));self.assertTrue(result['cleanup_confirmed'])
                headers={name.lower():value for name,value in server.requests[1][2].items()}
                for name in ('read','write','read-acp','write-acp','full-control'):self.assertEqual(headers['x-amz-grant-'+name],'id=independent-id')
                self.assertIn('Credential=local-test/',headers['authorization'])
                for index in (3,4):self.assertIn('Credential=alt-key/',{name.lower():value for name,value in server.requests[index][2].items()}['authorization'])
                self.assertEqual(server.requests[3][3],b'bar');self.assertTrue(server.requests[3][1].endswith('/foo'))
                self.assertEqual({name.lower():value for name,value in server.requests[4][2].items()}['x-amz-acl'],'public-read-write')
                self.assertEqual(len(server.fixtures),0)

    def test_bad_acl_oracle_restores_alt_acl_and_missing_metadata_is_zero_http(self):
        """Wrong independent DisplayName must fail the original grant consumer while still restoring the alt cleanup ACL and deleting its bucket; missing alt expected metadata must send no requests."""
        from sdk_fixtures import fixture_worker
        fixtures=[(404,{},b''),(200,{},b''),(200,XML,acl_xml(True)),(200,{},b''),(200,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3',overrides={'OSV_ALT_ACCESS_KEY':'alt-key','OSV_ALT_SECRET_KEY':'alt-secret'}) as (worker,server):
                with self.assertRaises(CaseNotRun):header_grants(self.probe(worker,Path(temporary),False))
                self.assertEqual(server.requests,[])
                with self.assertRaises(AssertionError):header_grants(self.probe(worker,Path(temporary)))
                self.assertTrue(all(not item[1].endswith('/foo') for item in server.requests))
                headers={name.lower():value for name,value in server.requests[3][2].items()}
                self.assertEqual(headers['x-amz-acl'],'public-read-write');self.assertIn('Credential=alt-key/',headers['authorization'])
                self.assertEqual(len(server.fixtures),0)

    def test_valid_unicode_uri_rejects_fabricated_parse_errors(self):
        """The actual SDK must encode a valid Unicode key exactly once; both fabricated HTTP400 parse errors must fail, regardless of message, with cleanup intact."""
        from sdk_fixtures import fixture_worker
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for message in ("Couldn't parse the specified URI.",'different message'):
                fixtures=[(404,{},b''),(200,{},b''),(400,XML,('<Error><Code>InvalidRequest</Code><Message>'+message+'</Message></Error>').encode()),
                 (200,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=language,message=message),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                    with self.assertRaises(AssertionError):unreadable(self.probe(worker,Path(temporary)))
                    self.assertTrue(unquote(urlsplit(server.requests[2][1]).path).endswith('/\xae\x8a-'))
                    self.assertEqual(server.requests[2][0],'GET');self.assertEqual(len(server.fixtures),0)


@unittest.skipUnless(os.environ.get('OSV_RUN_METADATA_TAIL_TESTS')=='1','requires genuine SDK workers and isolated local service')
class MetadataTailLiveTest(unittest.TestCase):
    """Preserve original source service differences and missing-identity prerequisites while proving scoped bucket cleanup."""
    def test_source_default_outcomes_keep_unrelated_bucket(self):
        """An unconfigured alternate identity must be not_run; valid Unicode keys must round-trip and all owned buckets must be cleaned while the sentinel survives."""
        import boto3,logging
        from unittest.mock import patch
        from local_moto import LocalMotoServer
        from osv.worker import Worker
        from osv.suites import run_cases
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        server=LocalMotoServer();server.start()
        try:
            host,port=server.get_host_and_port();root=Path(__file__).resolve().parents[2]
            with patch.dict(os.environ,{'OSV_ACCESS_KEY':'local-test','OSV_SECRET_KEY':'local-test','OSV_SESSION_TOKEN':'','OSV_ENDPOINT':f'http://{host}:{port}',
                'OSV_REGION':'us-east-1','OSV_BUCKET':'osv-tail-sentinel','OSV_PATH_STYLE':'true','OSV_RETRIES':'0','OSV_ALT_ACCESS_KEY':'','OSV_ALT_SECRET_KEY':''}):
                client=boto3.client('s3',endpoint_url=os.environ['OSV_ENDPOINT'],region_name='us-east-1',aws_access_key_id='local-test',aws_secret_access_key='local-test');client.create_bucket(Bucket='osv-tail-sentinel')
                for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
                    with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                        command=['java','-Xmx256m','-jar',str(root/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3'] if language=='java' else [str(root/'drivers/cpp/build/osv-cpp-s3')]
                        worker=Worker({'command':command,'language':language,'api':'s3'},30)
                        try:
                            d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'independent',d/'body',d/'output',{}),CASES)
                            self.assertEqual([row['status'] for row in rows],['not_run','pass']);self.assertEqual(rows[0]['missing_operations'],['identity_alt'])
                            self.assertEqual({item['Name'] for item in client.list_buckets()['Buckets']},{'osv-tail-sentinel'})
                        finally:worker.close()
        finally:server.stop()
