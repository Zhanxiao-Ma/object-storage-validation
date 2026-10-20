"""Independent 30-MiB source inputs and HTTP documents exercise encrypted multipart consumers."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from xml.etree import ElementTree as ET
from osv.ceph_encryption_tail import CASES,multipart,torrent
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun

XML={'Content-Type':'application/xml'}
EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
SIZE=31457280
PART=5242880


@unittest.skipUnless(os.environ.get('OSV_RUN_ENCRYPTION_TAIL_TESTS')=='1','requires genuine SDK workers')
class EncryptionTailWireTest(unittest.TestCase):
    """Feed independent literal raw payloads and HTTP responses through actual SDKs; verify original geometry and opaque service output handoff without a storage model."""
    def probe(self,worker,directory,workload=None):
        return Probe(Context(worker,'unused/',directory,b'independent',directory/'body',directory/'output',workload or {}))

    def test_original_six_parts_metadata_opaque_etags_and_actual_range_consumers(self):
        """Use six independent constant-letter source chunks; real SDK UploadPart ETags must enter completion, original 1M/10M Range loops must consume actual full-GET size, metadata and byte slices must match exactly."""
        from sdk_fixtures import fixture_worker
        data=b''.join(bytes([value])*PART for value in b'ABCDEF')
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('kms','s3_default'):
                headers={'Content-Type':'text/plain','x-amz-meta-foo':'bar'}
                if mode=='s3_default':headers['x-amz-server-side-encryption']='AES256'
                fixtures=[(404,{},b''),(200,{},b'')]+([(200,{},b'')] if mode=='s3_default' else [])
                fixtures += [(200,XML,b'<InitiateMultipartUploadResult><UploadId>opaque &amp;+/=</UploadId></InitiateMultipartUploadResult>')]
                fixtures += [(200,{'ETag':'"service-part-'+str(index)+'"'},b'') for index in range(1,7)]
                fixtures += [(200,XML,b'<CompleteMultipartUploadResult><ETag>"complete"</ETag></CompleteMultipartUploadResult>'),
                 (200,XML,b'<ListBucketResult><Contents><Key>multipart_enc</Key><Size>31457280</Size></Contents></ListBucketResult>'),(200,headers,data)]
                expected_ranges=[]
                for stride in (1000000,10000000):
                    fixtures.append((200,headers,data))
                    for offset in range(0,SIZE,stride):
                        end=min(offset+stride,SIZE);fixtures.append((206,headers,data[offset:end]));expected_ranges.append(f'bytes={offset}-{end-1}')
                fixtures += [(404,XML,b'<Error><Code>NoSuchUpload</Code></Error>'),(200,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                    result=multipart(self.probe(worker,Path(temporary),{'kms_key_id':'independent-main'}),mode,data=data)
                    self.assertTrue(result['cleanup_confirmed'])
                    parts=[item for item in server.requests if item[0]=='PUT' and 'partNumber=' in item[1]];self.assertEqual(len(parts),6)
                    for index,item in enumerate(parts):
                        query=parse_qs(urlsplit(item[1]).query);self.assertEqual(query['uploadId'],['opaque &+/=']);self.assertEqual(query['partNumber'],[str(index+1)])
                        self.assertEqual(item[3],bytes([b'ABCDEF'[index]])*PART)
                        actual={key.lower():value for key,value in item[2].items()};self.assertEqual(actual['content-type'],'text/plain')
                        if mode=='kms':self.assertEqual(actual['x-amz-server-side-encryption-aws-kms-key-id'],'independent-main')
                        else:self.assertNotIn('x-amz-server-side-encryption',actual)
                    complete=next(item for item in server.requests if item[0]=='POST' and 'uploadId=' in item[1]);document=ET.fromstring(complete[3])
                    records=[element for element in document.iter() if element.tag.rsplit('}',1)[-1]=='ETag'];self.assertEqual([item.text for item in records],['service-part-'+str(index) for index in range(1,7)])
                    ranges=[];whole=[]
                    for item in server.requests:
                        if item[0]=='GET' and urlsplit(item[1]).path.endswith('/multipart_enc'):
                            values={key.lower():value for key,value in item[2].items()}
                            if 'range' in values:ranges.append(values['range'])
                            else:whole.append(item)
                            self.assertNotIn('x-amz-server-side-encryption',values)
                    self.assertEqual(ranges,expected_ranges);self.assertEqual(len(whole),3);self.assertEqual(len(server.fixtures),0)

    def test_invalid_chunk_helpers_preserve_success_or_failure_and_abort_actual_upload(self):
        """Original invalid-key helpers contain no rejection assertions: six accepted parts must pass, while real service failure must fail and abort its actual upload ID; missing independent keys must be zero HTTP."""
        from sdk_fixtures import fixture_worker
        data=b'X'*SIZE
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('invalid_secondary','invalid_missing'):
                for rejected in (False,True):
                    parts=[(400,XML,b'<Error><Code>InvalidRequest</Code></Error>')] if rejected else [(200,{'ETag':'"part'+str(i)+'"'},b'') for i in range(1,7)]
                    fixtures=[(404,{},b''),(200,{},b''),(200,XML,b'<InitiateMultipartUploadResult><UploadId>real-incomplete-id</UploadId></InitiateMultipartUploadResult>')]+parts+[(204,{},b''),(200,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
                    with self.subTest(language=language,mode=mode,rejected=rejected),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                        p=self.probe(worker,Path(temporary),{'kms_key_id':'independent-main','secondary_kms_key_id':'independent-secondary'})
                        if rejected:
                            with self.assertRaises(RuntimeError):multipart(p,mode,data=data)
                        else:self.assertEqual(multipart(p,mode,data=data)['uploaded_parts'],6)
                        actual_parts=[item for item in server.requests if item[0]=='PUT' and 'partNumber=' in item[1]]
                        for item in actual_parts:self.assertEqual({name.lower():value for name,value in item[2].items()}['x-amz-server-side-encryption-aws-kms-key-id'],'independent-secondary' if mode=='invalid_secondary' else 'testkey-not-present')
                        self.assertEqual(len([item for item in server.requests if item[0]=='DELETE' and parse_qs(urlsplit(item[1]).query).get('uploadId')==['real-incomplete-id']]),1)
                        self.assertFalse(any(item[0]=='POST' and 'uploadId=' in item[1] for item in server.requests));self.assertEqual(len(server.fixtures),0)
            with tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3') as (worker,server):
                with self.assertRaises(CaseNotRun):multipart(self.probe(worker,Path(temporary)),'kms')
                with self.assertRaises(CaseNotRun):multipart(self.probe(worker,Path(temporary),{'kms_key_id':'independent-main'}),'invalid_secondary')
                self.assertEqual(server.requests,[])

    def test_optional_torrent_success_absence_and_rejection_oracles(self):
        """Original 7MiB Avatar.mpg must be uploaded literally; a distinct torrent body or exact404/NoSuchKey passes, original object bytes and other failures do not pass; SDK signs the bare torrent flag once."""
        from sdk_fixtures import fixture_worker
        variants=[('torrent',200,{},b'd8:announce13:https://tracke'),('absent',404,XML,b'<Error><Code>NoSuchKey</Code></Error>'),
         ('same',200,{},b'A'*(7*1024*1024)),('invalid_utf8',200,{},b'\xff'),('denied',403,XML,b'<Error><Code>AccessDenied</Code></Error>')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode,status,headers,body in variants:
                fixtures=[(404,{},b''),(200,{},b''),(200,{},b''),(status,headers,body),(200,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,fixtures,'s3') as (worker,server):
                    if mode=='invalid_utf8':
                        with self.assertRaises(UnicodeDecodeError):torrent(self.probe(worker,Path(temporary)))
                    elif mode in ('same','denied'):
                        with self.assertRaises(AssertionError):torrent(self.probe(worker,Path(temporary)))
                    else:torrent(self.probe(worker,Path(temporary)))
                    self.assertEqual(server.requests[2][3],b'A'*(7*1024*1024));self.assertTrue(urlsplit(server.requests[3][1]).path.endswith('/Avatar.mpg'))
                    self.assertEqual(urlsplit(server.requests[3][1]).query,'torrent=' if language=='cpp' else 'torrent');self.assertEqual(len(server.fixtures),0)


@unittest.skipUnless(os.environ.get('OSV_RUN_ENCRYPTION_TAIL_TESTS')=='1','requires genuine SDK workers and isolated local service')
class EncryptionTailLiveTest(unittest.TestCase):
    """Run the original default encryption multipart workload on a real isolated service, retaining optional-feature and prerequisite outcomes."""
    def test_original_default_source_workload_and_cleanup(self):
        """Real 30MiB source-generated data must survive SDK upload/completion/metadata/full/range reads; missing KMS keys remain not_run and torrent retains its actual source oracle; preserve an unrelated sentinel."""
        import boto3,json,logging
        from unittest.mock import patch
        from local_moto import LocalMotoServer
        from osv.worker import Worker
        from osv.suites import run_cases
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        server=LocalMotoServer();server.start()
        try:
            host,port=server.get_host_and_port();root=Path(__file__).resolve().parents[2]
            with patch.dict(os.environ,{'OSV_ACCESS_KEY':'local-test','OSV_SECRET_KEY':'local-test','OSV_SESSION_TOKEN':'','OSV_ENDPOINT':f'http://{host}:{port}',
                'OSV_REGION':'us-east-1','OSV_BUCKET':'osv-encryption-tail-sentinel','OSV_PATH_STYLE':'true','OSV_RETRIES':'0'}):
                client=boto3.client('s3',endpoint_url=os.environ['OSV_ENDPOINT'],region_name='us-east-1',aws_access_key_id='local-test',aws_secret_access_key='local-test');client.create_bucket(Bucket='osv-encryption-tail-sentinel')
                for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
                    with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                        command=['java','-Xmx256m','-jar',str(root/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3'] if language=='java' else [str(root/'drivers/cpp/build/osv-cpp-s3')]
                        worker=Worker({'command':command,'language':language,'api':'s3'},30)
                        try:
                            d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'independent',d/'body',d/'output',{}),CASES)
                            self.assertEqual([row['status'] for row in rows],['not_run']*3+['pass','fail'])
                            self.assertEqual([row['status'] for row in rows[:3]],['not_run']*3)
                            self.assertEqual({item['Name'] for item in client.list_buckets()['Buckets']},{'osv-encryption-tail-sentinel'})
                        finally:worker.close()
        finally:server.stop()
