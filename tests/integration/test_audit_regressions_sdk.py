"""Regression inputs cross the real scenario, SDK serializer and response parser."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe
from osv.ceph_checksum_attributes import multipart_checksum_attributes
from osv.ceph_conditional_versions import delete_many_attribute

OK=(200,{},b'');NO=(404,{},b'');DEL=(204,{},b'')
XML={'Content-Type':'application/xml'}
EMPTY=(200,XML,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>')

@unittest.skipUnless(os.environ.get('OSV_RUN_AUDIT_TESTS')=='1','requires built real SDK workers')
class AuditRegressionTest(unittest.TestCase):
    """Catch missing service checksums and logical/physical key confusion at actual SDK boundaries."""
    def test_complete_receives_all_actual_part_checksums(self):
        """Run the full 64 MiB production upload producer; expect every service-returned checksum in real Complete XML, then an injected service rejection and complete cleanup."""
        payload=b'A'*(64*1024*1024)
        sums=[base64.b64encode(hashlib.sha256(payload[n:n+5*1024*1024]).digest()).decode() for n in range(0,len(payload),5*1024*1024)]
        plan=[NO,OK,(200,XML,b'<InitiateMultipartUploadResult><UploadId>actual-upload</UploadId></InitiateMultipartUploadResult>')]
        plan += [(200,{'ETag':'"part-'+str(n)+'"','x-amz-checksum-sha256':value},b'') for n,value in enumerate(sums,1)]
        plan += [(400,XML,b'<Error><Code>InvalidRequest</Code><Message>independent-complete-stop</Message></Error>'),DEL,OK,DEL,EMPTY,DEL,NO]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                directory=Path(temporary);probe=Probe(Context(worker,'audit/',directory,b'',directory/'body',directory/'out',{}))
                with patch('osv.ceph_checksum_attributes.source_data',return_value=payload),self.assertRaisesRegex(RuntimeError,'independent-complete-stop'):
                    multipart_checksum_attributes(probe)
                complete=[row for row in server.requests if row[0]=='POST' and 'uploadId=' in row[1]]
                self.assertEqual(len(complete),1)
                root=ET.fromstring(complete[0][3])
                self.assertEqual([node.text for node in root.findall('.//{*}ChecksumSHA256')],sums)
                self.assertEqual([node.text for node in root.findall('.//{*}PartNumber')],[str(n) for n in range(1,14)])
                self.assertFalse(server.fixtures)

    def test_conditional_delete_consumes_actual_prefixed_key(self):
        """Execute the real prefixed conditional-delete scenario; expect actual SDK Deleted keys to satisfy the full case and no prefix stripping or out-of-scope cleanup."""
        key='audit/scope-'+'a'*32+'/obj'
        error=(200,XML,('<DeleteResult><Error><Key>'+key+'</Key><Code>PreconditionFailed</Code></Error></DeleteResult>').encode())
        success=(200,XML,('<DeleteResult><Deleted><Key>'+key+'</Key></Deleted></DeleteResult>').encode())
        plan=[EMPTY,(200,XML,b'<VersioningConfiguration/>'),(200,{'ETag':'"d41d8cd98f00b204e9800998ecf8427e"'},b''),error,success,success,(200,XML,b'<DeleteResult/>'),EMPTY,EMPTY]
        with tempfile.TemporaryDirectory() as temporary,fixture_worker('cpp',plan,'s3') as (worker,server):
            directory=Path(temporary)
            ctx=Context(worker,'audit/',directory,b'',directory/'body',directory/'out',{},object_namespace=True)
            with patch('osv.bucket_scope.uuid.uuid4',return_value=SimpleNamespace(hex='a'*32)):
                delete_many_attribute(Probe(ctx),'match','plain')
            batches=[ET.fromstring(row[3]) for row in server.requests if row[0]=='POST']
            self.assertEqual(len(batches),4)
            for batch in batches:self.assertEqual([node.text for node in batch.findall('.//{*}Key')],[key])
            self.assertFalse(server.fixtures)
