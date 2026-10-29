"""Checksum helper regression through the real C++ SDK and raw HTTP fixtures."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.ceph_checksum_attributes import checksum_type_helper
from osv.scenarios import Context,SDKOperationError
from osv.suites import Probe

XML={'Content-Type':'application/xml'}
TAIL=[(204,{},b''),(200,{},b''),(204,{},b''),(200,XML,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'),(204,{},b''),(404,{},b'')]

@unittest.skipUnless(os.environ.get('OSV_RUN_CHECKSUM_HELPER_TESTS')=='1','requires built C++ SDK')
class ChecksumHelperSdkTest(unittest.TestCase):
    def test_real_helper_contract(self):
        """Accept valid bare/suffixed attributes and FULL_OBJECT requests; reject bad digests, counts, rows and downloaded bytes using real SDK output."""
        for algorithm,modes in [('SHA256',('bare','suffix','bad_suffix','bad_digest','missing_row','duplicate','bad_count','bad_part_digest','bad_full_body','bad_part_body','short_part','bad_complete')),('SHA1',('bare',)),('CRC32',('bare','no_part_checksum','bad_part_body_unchecked','short_part_unchecked'))]:
            payloads=[bytes([x])*(5*1024*1024) for x in b'ABC'];payload=b''.join(payloads)
            composite_type=algorithm.startswith('SHA');typ='COMPOSITE' if composite_type else 'FULL_OBJECT';field='Checksum'+algorithm
            if composite_type:
                digests=[hashlib.new(algorithm.lower(),b).digest() for b in payloads]
                sums=[base64.b64encode(v).decode() for v in digests]
                digest=base64.b64encode(hashlib.new(algorithm.lower(),b''.join(digests)).digest()).decode();complete=digest+'-3'
            else:
                import zlib
                sums=[base64.b64encode(zlib.crc32(b).to_bytes(4,'big')).decode() for b in payloads]
                digest=complete=base64.b64encode(zlib.crc32(payload).to_bytes(4,'big')).decode()
            header='x-amz-checksum-'+algorithm.lower()
            for mode in modes:
                plan=[(404,{},b''),(200,{},b''),(200,{'x-amz-checksum-algorithm':algorithm,'x-amz-checksum-type':typ},b'<InitiateMultipartUploadResult><UploadId>upload</UploadId></InitiateMultipartUploadResult>')]
                plan += [(200,{'ETag':'"part"',header:v},b'') for v in sums]
                if mode=='bad_complete':plan.append((400,XML,b'<Error><Code>BadDigest</Code><Message>independent rejection</Message></Error>'))
                else:
                    plan += [(200,XML,('<CompleteMultipartUploadResult><ETag>"done"</ETag><'+field+'>'+complete+'</'+field+'><ChecksumType>'+typ+'</ChecksumType></CompleteMultipartUploadResult>').encode()),(200,{},b''),(200,{header:complete,'x-amz-checksum-type':typ},b'')]
                    root=ET.Element('GetObjectAttributesResponse');checks=ET.SubElement(root,'Checksum')
                    value=digest+'-2' if mode=='bad_suffix' else complete if mode=='suffix' else 'AAAA' if mode=='bad_digest' else digest
                    ET.SubElement(checks,field).text=value;ET.SubElement(checks,'ChecksumType').text=typ
                    parts=ET.SubElement(root,'ObjectParts');ET.SubElement(parts,'PartsCount').text='2' if mode=='bad_count' else '3'
                    if composite_type:
                        numbers=[1,2] if mode=='missing_row' else [1,2,2] if mode=='duplicate' else [1,2,3]
                        for n in numbers:
                            part=ET.SubElement(parts,'Part')
                            for k,v in [('PartNumber',str(n)),('Size',str(len(payloads[n-1]))),(field,'AAAA' if mode=='bad_part_digest' else sums[n-1])]:ET.SubElement(part,k).text=v
                    plan.append((200,XML,ET.tostring(root)))
                    if mode not in ('bad_suffix','bad_digest','missing_row','duplicate','bad_count','bad_part_digest'):
                        plan.append((200,{},b'Z'+payload[1:] if mode=='bad_full_body' else payload))
                        if mode!='bad_full_body':
                            for n,b in enumerate(payloads):
                                part=b[:-1] if mode in ('short_part','short_part_unchecked') else b'Z'+b[1:] if mode in ('bad_part_body','bad_part_body_unchecked') else b
                                plan.append((206,{} if mode in ('no_part_checksum','bad_part_body_unchecked','short_part_unchecked') else {header:sums[n],'x-amz-checksum-type':typ},part))
                                if mode in ('bad_part_body','short_part','bad_part_body_unchecked','short_part_unchecked'):break
                plan+=TAIL
                with self.subTest(algorithm=algorithm,mode=mode),tempfile.TemporaryDirectory() as tmp,fixture_worker('cpp',plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (w,server):
                    d=Path(tmp);p=Probe(Context(w,'helper/',d,b'',d/'body',d/'out',{}))
                    if mode in ('bare','suffix','no_part_checksum'):checksum_type_helper(p,algorithm)
                    else:
                        with self.assertRaises((AssertionError,SDKOperationError)):checksum_type_helper(p,algorithm)
                    requests=[r for r in server.requests if r[0]=='POST' and 'uploadId=' in r[1]]
                    self.assertEqual(len(requests),1)
                    headers={k.lower():v for k,v in requests[0][2].items()}
                    self.assertEqual(headers['x-amz-checksum-type'],typ)
                    self.assertEqual(headers[header],complete)
                    self.assertEqual([e.text for e in ET.fromstring(requests[0][3]).findall('.//{*}'+field)],sums)
                    self.assertFalse(server.fixtures)
