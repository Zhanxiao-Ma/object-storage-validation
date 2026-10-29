"""Exercise checksum scenario assertions through actual SDK HTTP producers and consumers."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.ceph_checksum_attributes import multipart_checksum_attributes
from osv.scenarios import Context
from osv.suites import Probe

MIB=1024*1024
OK=(200,{},b'');NO=(404,{},b'');DEL=(204,{},b'');XML={'Content-Type':'application/xml'}
EMPTY=(200,XML,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>')
TAIL=[DEL,OK,DEL,EMPTY,DEL,NO]

@unittest.skipUnless(os.environ.get('OSV_RUN_CHECKSUM_INTEGRITY_TESTS')=='1','requires built SDK workers')
class ChecksumIntegritySdkTest(unittest.TestCase):
    """Use fixed independent bytes and raw HTTP evidence instead of a copied storage implementation."""
    def test_complete_readback_and_attributes_are_all_required(self):
        """Run the full 64MiB scenario in both SDKs; accept independently correct composite forms and reject corrupt bytes, part checksums, completion digests, missing/duplicate rows and wrong count suffixes."""
        payload=b'A'*(64*MIB)
        digests=[hashlib.sha256(payload[n:n+5*MIB]).digest() for n in range(0,len(payload),5*MIB)]
        sums=[base64.b64encode(v).decode() for v in digests]
        base=base64.b64encode(hashlib.sha256(b''.join(digests)).digest()).decode();composite=base+'-13'
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('bare','suffixed','bad_part','bad_complete','bad_body','short_body','missing_row','duplicate_row','bad_suffix'):
                plan=[NO,OK,(200,XML,b'<InitiateMultipartUploadResult><UploadId>upload</UploadId></InitiateMultipartUploadResult>')]
                values=sums if mode!='bad_part' else [base64.b64encode(bytes(32)).decode()]
                plan += [(200,{'ETag':'"part-'+str(n)+'"','x-amz-checksum-sha256':v},b'') for n,v in enumerate(values,1)]
                if mode!='bad_part':
                    complete=composite if mode!='bad_complete' else base64.b64encode(bytes(32)).decode()+'-13'
                    plan.append((200,XML,('<CompleteMultipartUploadResult><ETag>"done"</ETag><ChecksumSHA256>'+complete+'</ChecksumSHA256></CompleteMultipartUploadResult>').encode()))
                    if mode!='bad_complete':
                        plan.append((200,{},payload[:-1] if mode=='short_body' else payload if mode!='bad_body' else b'B'+payload[1:]))
                        if mode not in ('bad_body','short_body'):
                            root=ET.Element('GetObjectAttributesOutput');ET.SubElement(root,'ObjectSize').text=str(len(payload))
                            ET.SubElement(ET.SubElement(root,'Checksum'),'ChecksumSHA256').text=(base if mode=='bare' else base+'-12' if mode=='bad_suffix' else composite)
                            parts=ET.SubElement(root,'ObjectParts');ET.SubElement(parts,'PartsCount').text='13';ET.SubElement(parts,'IsTruncated').text='false'
                            numbers=list(range(1,14)) if mode!='missing_row' else list(range(1,13))
                            if mode=='duplicate_row':numbers[-1]=12
                            for n in numbers:
                                row=ET.SubElement(parts,'Part')
                                for name,v in (('PartNumber',str(n)),('Size',str(5*MIB if n<13 else 4*MIB)),('ChecksumSHA256',sums[n-1])):ET.SubElement(row,name).text=v
                            plan.append((200,XML,ET.tostring(root)))
                plan += TAIL
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'checksum/',d,b'',d/'body',d/'out',{}))
                    with patch('osv.ceph_checksum_attributes.source_data',return_value=payload):
                        if mode in ('bare','suffixed'):multipart_checksum_attributes(p)
                        else:
                            with self.assertRaises(AssertionError):multipart_checksum_attributes(p)
                    uploads=[r for r in server.requests if r[0]=='PUT' and 'partNumber=' in r[1]]
                    for n,row in enumerate(uploads):
                        headers={k.lower():v for k,v in row[2].items()}
                        self.assertEqual(headers['x-amz-checksum-sha256'],sums[n])
                        self.assertEqual(base64.b64encode(hashlib.sha256(row[3]).digest()).decode(),sums[n])
                    completes=[r for r in server.requests if r[0]=='POST' and 'uploadId=' in r[1]]
                    if completes:self.assertEqual([n.text for n in ET.fromstring(completes[0][3]).findall('.//{*}ChecksumSHA256')],sums)
                    self.assertFalse(server.fixtures)

    def test_cleanup_failure_keeps_the_original_upload_failure(self):
        """Reject a real SDK UploadPart, then deny Abort; the final scenario error must contain both independent service failures and still perform scoped object cleanup."""
        plan=[NO,OK,(200,XML,b'<InitiateMultipartUploadResult><UploadId>upload</UploadId></InitiateMultipartUploadResult>'),
              (400,XML,b'<Error><Code>InvalidRequest</Code><Message>part-rejected</Message></Error>'),
              (403,XML,b'<Error><Code>AccessDenied</Code><Message>abort-denied</Message></Error>'),OK,DEL,EMPTY,DEL,NO]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'checksum/',d,b'',d/'body',d/'out',{}))
                with self.assertRaisesRegex(RuntimeError,'part-rejected.*checksum multipart cleanup failed.*abort-denied'):
                    multipart_checksum_attributes(p)
                self.assertFalse(server.fixtures)

    def test_dead_worker_during_abort_keeps_original_service_failure(self):
        """Receive a real UploadPart rejection then terminate its actual worker before Abort; the cleanup transport exception must retain the primary service message."""
        from osv.bucket_scope import BucketScope
        from osv.ceph_checksum_attributes import checksum_upload
        plan=[(200,XML,b'<InitiateMultipartUploadResult><UploadId>upload</UploadId></InitiateMultipartUploadResult>'),
              (400,XML,b'<Error><Code>InvalidRequest</Code><Message>original-part-failure</Message></Error>')]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'checksum/',d,b'',d/'body',d/'out',{}));p.ctx.body.write_bytes(b'raw')
                bucket=BucketScope(p)
                with self.assertRaisesRegex(RuntimeError,'original-part-failure.*checksum multipart cleanup failed'):
                    with checksum_upload(bucket,'key','SHA256',check_algorithm=False) as ident:
                        try:bucket.call('mp_part',key='key',upload_id=ident,part_number=1,body_path=str(p.ctx.body))
                        finally:worker.close()
                self.assertFalse(server.fixtures)
