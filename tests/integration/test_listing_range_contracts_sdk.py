"""Check raw protocol and typed SDK observations without rewriting either response."""
import hashlib,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote,parse_qs,urlsplit
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.bucket_scope import BucketScope
from osv.scenarios import Context
from osv.suites import Probe
from osv.ceph_list_exact import encoded_page
from osv.ceph_range import ranged_http

@unittest.skipUnless(os.environ.get('OSV_RUN_LIST_RANGE_CONTRACT_TESTS')=='1','requires built SDK workers')
class ListingRangeContractSdkTest(unittest.TestCase):
    """Pass captured real HTTP XML and official SDK getter values to the production consumers."""
    def test_url_encoding_validates_whole_namespace_and_exact_sdk_decoding(self):
        """Use independent encoded XML with escaped prefix, literal percent and plus; accept equivalent slash encoding, reject malformed/decoded wire values, missing/duplicate keys and wrong namespace."""
        prefix='audit +%/scope-'+'a'*32+'/'
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for version in (1,2):
                for mode in ('encoded_slash','plain_slash','space_plus','missing_encoding','bad_escape','raw_space','wrong_namespace','missing_key','duplicate_key'):
                    safe='/' if mode=='plain_slash' else ''
                    rawkey=quote(prefix+'x%2By',safe=safe);rawgroup=quote(prefix+'foo+/',safe=safe)
                    if mode=='space_plus':rawkey=rawkey.replace('%20','+');rawgroup=rawgroup.replace('%20','+')
                    if mode=='bad_escape':rawkey+='%'
                    if mode=='raw_space':rawkey=rawkey.replace('%20',' ')
                    if mode=='wrong_namespace':rawkey=quote('outside/x%2By',safe=safe)
                    root=ET.Element('ListBucketResult')
                    for name,value in [('Prefix',quote(prefix,safe=safe)),('Delimiter',quote('/',safe=safe)),('IsTruncated','false')]+([] if mode=='missing_encoding' else [('EncodingType','url')]):ET.SubElement(root,name).text=value
                    for _ in range(0 if mode=='missing_key' else 2 if mode=='duplicate_key' else 1):ET.SubElement(ET.SubElement(root,'Contents'),'Key').text=rawkey
                    ET.SubElement(ET.SubElement(root,'CommonPrefixes'),'Prefix').text=rawgroup
                    plan=[(200,{'Content-Type':'application/xml'},ET.tostring(root))]
                    with self.subTest(language=language,version=version,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                        d=Path(temporary);p=Probe(Context(worker,'audit +%/',d,b'',d/'body',d/'out',{},object_namespace=True))
                        with patch('osv.bucket_scope.uuid.uuid4',return_value=SimpleNamespace(hex='a'*32)):bucket=BucketScope(p)
                        if mode in ('encoded_slash','plain_slash','space_plus'):
                            result=encoded_page(bucket,version,{'encoding_type':'url','delimiter':'/'},['x%252By'],['foo%2B/'],{'delimiter':'/'})
                            self.assertEqual(result['sdk_representation'],'decoded' if language=='java' else 'encoded')
                        else:
                            with self.assertRaises((AssertionError,RuntimeError)):encoded_page(bucket,version,{'encoding_type':'url','delimiter':'/'},['x%252By'],['foo%2B/'],{'delimiter':'/'})
                        query=parse_qs(urlsplit(server.requests[0][1]).query);self.assertEqual(query['prefix'],[prefix]);self.assertEqual(query['encoding-type'],['url'])
                        self.assertFalse(server.fixtures)

    def test_range_standard_negotiated_and_invalid_responses(self):
        """Run the actual Range case; standard206 and correctly negotiated Java append-md5 pass, while wrong range, ordinary200 and corrupt bytes remain failures."""
        expected=b'cont'
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('standard','variant','ordinary_200','wrong_range','wrong_body','bad_md5'):
                status=206;headers={'Content-Range':'bytes 4-7/11'};body=expected
                if mode in ('variant','bad_md5'):
                    status=200;headers={'x-amz-content-range':'bytes 4-7/11','x-amz-transfer-encoding':'append-md5'}
                    body=expected+(hashlib.md5(expected).digest() if mode=='variant' else bytes(16))
                if mode=='ordinary_200':status=200
                if mode=='wrong_range':headers['Content-Range']='bytes 3-6/11'
                if mode=='wrong_body':body=b'xxxx'
                good=mode=='standard' or mode=='variant' and language=='java'
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(200,{},b''),(status,headers,body)],'s3') as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'range/',d,b'',d/'body',d/'out',{}))
                    if good:
                        result=ranged_http(p,'closed');self.assertEqual(result['range_response'],'standard_206' if mode=='standard' else 'negotiated_append_md5_200')
                    else:
                        with self.assertRaises((AssertionError,RuntimeError)):ranged_http(p,'closed')
                    request={k.lower():v for k,v in server.requests[1][2].items()};self.assertEqual(request['range'],'bytes=4-7')
                    self.assertFalse(server.fixtures)

    def test_request_header_observation_is_scoped_and_excludes_credentials(self):
        """Capture genuine Java Range negotiation, then issue a plain GET; only allowlisted headers may appear and state must not leak to the next RPC."""
        with tempfile.TemporaryDirectory() as temporary,fixture_worker('java',[(206,{'Content-Range':'bytes 0-0/1'},b'x'),(200,{},b'x')],'s3') as (worker,server):
            path=str(Path(temporary)/'out')
            first=worker.call({'op':'get_http','key':'k','range_header':'bytes=0-0','output_path':path})
            self.assertTrue(first['ok'],first);self.assertEqual(first['request_headers']['range'],'bytes=0-0')
            self.assertLessEqual(set(first['request_headers']),{'range','x-amz-te'})
            second=worker.call({'op':'get','key':'k','output_path':path});self.assertTrue(second['ok'],second);self.assertNotIn('request_headers',second)
