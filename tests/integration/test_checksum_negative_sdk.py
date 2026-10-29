"""Validate checksum-negative requests through real SDK HTTP output."""
import base64,hashlib,os,tempfile,unittest
from pathlib import Path
from sdk_fixtures import fixture_worker
from osv.ceph_checksum_attributes import object_checksum
from osv.scenarios import Context
from osv.suites import Probe

@unittest.skipUnless(os.environ.get('OSV_RUN_CHECKSUM_NEGATIVE_TESTS')=='1','requires SDK workers')
class ChecksumNegativeSdkTest(unittest.TestCase):
    """Check transmitted negative inputs and post-rejection object state independently."""
    def test_mismatch_is_well_formed_and_failed_overwrite_preserves_original(self):
        """Use genuine SDK requests; require valid 32-byte wrong SHA256, BadDigest, and unchanged original bytes; reject accepted writes, wrong errors and silently modified content."""
        digest=base64.b64encode(hashlib.sha256(b'A'*1024).digest()).decode()
        xml={'Content-Type':'application/xml'}
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('valid_rejection','accepted','wrong_error','changed_object'):
                code='InvalidRequest' if mode=='wrong_error' else 'BadDigest'
                failure=(400,xml,('<Error><Code>'+code+'</Code><Message>checksum rejected</Message></Error>').encode())
                if mode=='accepted':failure=(200,{},b'')
                plan=[(404,{},b''),(200,{},b''),(200,{'ETag':'"opaque"','x-amz-checksum-sha256':digest},b''),
                      (200,{},b''),(200,{'x-amz-checksum-sha256':digest},b''),failure]
                if mode in ('valid_rejection','changed_object'):plan.append((200,{},(b'A' if mode=='valid_rejection' else b'B')*1024))
                plan.extend([(200,{},b''),(204,{},b''),(200,xml,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'),(204,{},b''),(404,{},b'')])
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'checksum/',d,b'',d/'body',d/'out',{}))
                    if mode=='valid_rejection':object_checksum(p,'SHA256')
                    else:
                        with self.assertRaises((AssertionError,RuntimeError)):object_checksum(p,'SHA256')
                    requests=[r for r in server.requests if r[0]=='PUT' and '/myobj' in r[1]]
                    self.assertEqual(len(requests),2)
                    request=requests[1];headers={k.lower():v for k,v in request[2].items()}
                    raw=base64.b64decode(headers['x-amz-checksum-sha256'],validate=True)
                    self.assertEqual(len(raw),32);self.assertNotEqual(raw,hashlib.sha256(request[3]).digest())
                    self.assertEqual(request[3],b'B'*1024);self.assertFalse(server.fixtures)
