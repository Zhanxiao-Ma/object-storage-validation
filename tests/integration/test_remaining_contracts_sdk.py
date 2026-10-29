"""Regression coverage for remaining compatibility audit contracts using real SDK traffic."""
import base64,hashlib,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.scenarios import Context,SDKOperationError,require_ok
from osv.suites import Probe,CaseNotRun,run_cases,case
from osv.ceph_multipart import upload
from osv.ceph_get_part import out_of_range
from osv.ceph_checksum_attributes import multipart
from osv.ceph_list_exact import check_wire_delimiter,ListSdkBoundaryError

XML={'Content-Type':'application/xml'}
NO=(404,{},b'');OK=(200,{},b'');DEL=(204,{},b'')
EMPTY=(200,XML,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>')
CREATE=(200,XML,b'<InitiateMultipartUploadResult><UploadId>u</UploadId></InitiateMultipartUploadResult>')
TAIL=[DEL,OK,DEL,EMPTY,DEL,NO]
def error(status,code):return status,XML,('<Error><Code>'+code+'</Code><Message>fixture rejection</Message></Error>').encode()
def complete(etag='done',checksum=None):
    return 200,XML,('<CompleteMultipartUploadResult><ETag>"'+etag+'"</ETag>'+('' if checksum is None else '<ChecksumSHA256>'+checksum+'</ChecksumSHA256>')+'</CompleteMultipartUploadResult>').encode()
def probe(worker,d):return Probe(Context(worker,'remaining/',d,b'',d/'body',d/'out',{}))

@unittest.skipUnless(os.environ.get('OSV_RUN_REMAINING_TESTS')=='1','requires SDK workers')
class RemainingContractsSdkTest(unittest.TestCase):
    """Drive production cases from independently supplied HTTP responses; never emulate the case implementation."""
    def test_repeat_completion_requires_exact_outcome_and_final_content(self):
        """Run the real small-upload case; identical success or404 NoSuchUpload plus correct GET passes, wrong error, changed ETag or changed final bytes fails."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('success','consumed','wrong_error','wrong_etag','wrong_content'):
                second=error(404,'NoSuchUpload') if mode in ('consumed','wrong_content') else error(403,'AccessDenied') if mode=='wrong_error' else complete('changed' if mode=='wrong_etag' else 'done')
                plan=[NO,OK,CREATE,(200,{'ETag':'"part"'},b''),complete(),second]
                if mode not in ('wrong_error','wrong_etag'):plan.append((200,{},b'B' if mode=='wrong_content' else b'A'))
                plan+=TAIL
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    p=probe(worker,Path(temporary))
                    with patch('osv.ceph_multipart.source_data',return_value=b'A'):
                        if mode in ('success','consumed'):upload(p,'small')
                        else:
                            with self.assertRaises((AssertionError,RuntimeError)):upload(p,'small')
                    self.assertFalse(server.fixtures)

    def test_part_out_of_range_does_not_accept_arbitrary_service_errors(self):
        """Feed actual SDK GetObject failures to the production checker; accept only explicit part/range errors, reject access denial, missing key, successful empty body and unrelated416."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for status,code,valid in [(400,'InvalidPart',True),(416,'InvalidRange',True),(416,'InvalidPartNumber',True),(403,'AccessDenied',False),(404,'NoSuchKey',False),(416,'Other',False),(200,'',False)]:
                with self.subTest(language=language,status=status,code=code),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[error(status,code) if status!=200 else OK],'s3') as (worker,server):
                    result=worker.call({'op':'get_part','key':'key','part_number':2,'output_path':str(Path(temporary)/'out')})
                    if valid:out_of_range(result)
                    else:
                        with self.assertRaises(AssertionError):out_of_range(result)
                    self.assertIn('partNumber=2',server.requests[0][1])

    def test_whitespace_wire_and_sdk_values_are_classified_separately(self):
        """Pass identical XML to both SDKs; Java preserves whitespace, C++ trimming is a decoding failure with untouched wire evidence, never a fabricated pass."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for delimiter in (' ','\n'):
                body=('<ListBucketResult><Delimiter>'+delimiter+'</Delimiter><IsTruncated>false</IsTruncated></ListBucketResult>').encode()
                with self.subTest(language=language,delimiter=delimiter),fixture_worker(language,[(200,XML,body)],'s3') as (worker,server):
                    result=worker.call({'op':'list_v2_observed','delimiter':delimiter,'capture_response_body':True})
                    require_ok(result)
                    if language=='java':check_wire_delimiter(result,delimiter)
                    else:
                        with self.assertRaises(ListSdkBoundaryError) as caught:check_wire_delimiter(result,delimiter)
                        self.assertEqual(caught.exception.evidence,{'wire_delimiter':delimiter,'sdk_delimiter':''})

    def test_attributes_pages_require_real_checksums_complete_rows_and_readback(self):
        """Upload six explicit SHA256 parts with real SDKs; independently validate request digests and exact page contents, rejecting missing/duplicate rows and corrupt per-part checksums."""
        block=b'A'*(5*1024*1024);digest=base64.b64encode(hashlib.sha256(block).digest()).decode()
        total=base64.b64encode(hashlib.sha256(base64.b64decode(digest)*6).digest()).decode()+'-6'
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('valid','missing','duplicate','wrong_checksum'):
                plan=[NO,OK,CREATE]+[(200,{'ETag':'"p'+str(n)+'"','x-amz-checksum-sha256':digest},b'') for n in range(1,7)]+[complete(checksum=total),(200,{},block*6)]
                for marker,maxparts,nums in ((3,1,[4]),(4,10,[5,6])):
                    root=ET.Element('GetObjectAttributesOutput')
                    for k,v in [('ObjectSize',len(block)*6),('ETag','done'),('StorageClass','STANDARD')]:ET.SubElement(root,k).text=str(v)
                    ET.SubElement(ET.SubElement(root,'Checksum'),'ChecksumSHA256').text=total
                    parts=ET.SubElement(root,'ObjectParts')
                    for k,v in [('PartsCount',6),('MaxParts',maxparts),('PartNumberMarker',marker),('IsTruncated','true' if marker==3 else 'false')]:ET.SubElement(parts,k).text=str(v)
                    if marker==3:ET.SubElement(parts,'NextPartNumberMarker').text='4'
                    if marker==4:
                        if mode=='missing':nums=[5]
                        if mode=='duplicate':nums=[5,5]
                    for n in nums:
                        item=ET.SubElement(parts,'Part')
                        for k,v in [('PartNumber',n),('Size',len(block)),('ChecksumSHA256','bad' if mode=='wrong_checksum' and marker==4 else digest)]:ET.SubElement(item,k).text=str(v)
                    plan.append((200,XML,ET.tostring(root)))
                plan+=TAIL
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    with patch('osv.ceph_checksum_attributes.source_data',return_value=block*6):
                        if mode=='valid':multipart(probe(worker,Path(temporary)),'paginated')
                        else:
                            with self.assertRaises((AssertionError,RuntimeError)):multipart(probe(worker,Path(temporary)),'paginated')
                    requests=[r for r in server.requests if r[0]=='PUT' and 'partNumber=' in r[1]]
                    self.assertEqual(len(requests),6)
                    for row in requests:self.assertEqual({k.lower():v for k,v in row[2].items()}['x-amz-checksum-sha256'],digest)
                    completed=next(r for r in server.requests if r[0]=='POST' and 'uploadId=' in r[1])
                    self.assertEqual([n.text for n in ET.fromstring(completed[3]).findall('.//{*}ChecksumSHA256')],[digest]*6)
                    self.assertFalse(server.fixtures)

    def test_unconstructable_protocol_cases_do_not_send_requests(self):
        """Invoke actual case gates before setup; unsupported directory conditions, nonstandard IfNoneMatch and signer-rewritten headers must be not-run with zero HTTP requests."""
        from osv.ceph_conditional_versions import delete_attribute,delete_many_attribute
        from osv.ceph_conditional_writes import put_condition
        from osv.ceph_request_headers import object_headers
        operations=[lambda p:delete_attribute(p,'size','plain'),lambda p:delete_many_attribute(p,'mtime','plain'),lambda p:put_condition(p,'none_bad'),lambda p:object_headers(p,{'Authorization':None},403),lambda p:object_headers(p,{'Content-Length':None},411)]
        with tempfile.TemporaryDirectory() as temporary,fixture_worker('java',[],'s3') as (worker,server):
            p=probe(worker,Path(temporary))
            for fn in operations:
                with self.assertRaises(CaseNotRun):fn(p)
            self.assertEqual(server.requests,[])

    def test_error_evidence_survives_real_sdk_to_report_consumer(self):
        """Send actual HTTP403 and503, then consume SDKOperationError through run_cases; require separate access/limit categories with true service codes."""
        for status,code,category in ((403,'AccessDenied','access_rejected'),(503,'SlowDown','throttled'),(400,'InvalidRequest','service_error')):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as temporary,fixture_worker('java',[error(status,code)],'s3') as (worker,server):
                p=probe(worker,Path(temporary))
                def execute(q):require_ok(q.ctx.worker.call({'op':'get','key':'key','output_path':str(q.ctx.output)}))
                rows=run_cases(p.ctx,[case('diagnostic','diagnostic','diagnostic','get',execute,'test_diagnostic')])
                self.assertEqual(rows[0]['failure_category'],category)
                self.assertEqual(rows[0]['evidence']['error_code'],code)

    def test_scalar_post_fields_are_not_uploaded_as_files_and_errors_are_scrubbed(self):
        """Send the production form through requests to a real HTTP fixture; only file has a filename, and diagnostic responses redact actual credentials/signature while retaining error code."""
        from collections import OrderedDict
        from email.parser import BytesParser
        from email.policy import default
        from types import SimpleNamespace
        import requests
        from osv.ceph_raw_http import post_form,check_post,PostResponseError
        fields=OrderedDict(key='prefix/key',AWSAccessKeyId='private-access',signature=b'private-signature',policy=b'private-policy',file=('data.txt',b'payload'))
        response_body=b'<Error><Code>InvalidArgument</Code><Message>private-access private-signature private-policy</Message></Error>'
        with fixture_worker('java',[(400,XML,response_body)],'s3') as (worker,server):
            response=requests.post('http://127.0.0.1:'+str(server.server_port),files=post_form(fields),timeout=5)
            headers=server.requests[0][2];content_type=next(v for k,v in headers.items() if k.lower()=='content-type')
            parsed=BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\n\r\n').encode()+server.requests[0][3])
            parts=list(parsed.iter_parts());self.assertEqual([p.get_param('name',header='content-disposition') for p in parts],list(fields))
            self.assertEqual([p.get_filename() for p in parts],[None,None,None,None,'data.txt'])
            self.assertEqual(parts[0].get_payload(decode=True),b'prefix/key')
            transport=SimpleNamespace(access='private-access',secret='private-secret')
            with self.assertRaises(PostResponseError) as caught:check_post(response,204,transport,fields)
            self.assertEqual(caught.exception.evidence['error_code'],'InvalidArgument')
            for value in ('private-access','private-signature','private-policy'):self.assertNotIn(value,str(caught.exception))

    def test_default_kms_is_a_positive_roundtrip(self):
        """Run default-KMS production scenario against real SDK replies; require aws:kms on PUT/GET and unchanged data, reject plaintext headers or corrupt readback."""
        from osv.ceph_encryption_objects import invalid
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('valid','plaintext','corrupt'):
                headers={'x-amz-server-side-encryption':'aws:kms'}
                plan=[NO,OK,(200,headers if mode!='plaintext' else {},b'')]
                if mode!='plaintext':plan.append((200,headers,(b'B' if mode=='corrupt' else b'A')*100))
                plan+=TAIL[1:]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                    if mode=='valid':invalid(probe(worker,Path(temporary)),'no_key')
                    else:
                        with self.assertRaises(AssertionError):invalid(probe(worker,Path(temporary)),'no_key')
                    headers={k.lower():v for k,v in server.requests[2][2].items()}
                    self.assertEqual(headers['x-amz-server-side-encryption'],'aws:kms')
                    self.assertNotIn('x-amz-server-side-encryption-aws-kms-key-id',headers)
                    self.assertFalse(server.fixtures)

    def test_multipart_negative_cases_change_only_the_intended_checksum(self):
        """Exercise all three actual checksum branches; bad digest stays well-formed, missing-part branch retains correct composite digest, and positive branch includes real part checksum."""
        from osv.ceph_checksum_attributes import multipart_sha256
        digest=base64.b64encode(hashlib.sha256(b'A'*1024).digest()).decode()
        composite=base64.b64encode(hashlib.sha256(base64.b64decode(digest)).digest()).decode()+'-1'
        create=(200,{**XML,'x-amz-checksum-algorithm':'SHA256'},CREATE[2])
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=[NO,OK]
            for mode in ('bad','missing','success'):
                plan.extend([create,(200,{'ETag':'"part"','x-amz-checksum-sha256':digest},b'')])
                if mode=='success':plan.extend([complete(checksum=composite),OK,(200,{'x-amz-checksum-sha256':composite},b'')])
                else:plan.extend([error(400,'BadDigest' if mode=='bad' else 'InvalidRequest'),error(404,'NoSuchKey')])
                plan.append(DEL)
            plan.extend([OK,DEL,DEL,DEL,EMPTY,DEL,NO])
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                multipart_sha256(probe(worker,Path(temporary)))
                requests=[r for r in server.requests if r[0]=='POST' and 'uploadId=' in r[1]]
                self.assertEqual(len(requests),3)
                checks=[]
                for row in requests:checks.append({k.lower():v for k,v in row[2].items()}['x-amz-checksum-sha256'])
                self.assertEqual(len(base64.b64decode(checks[0].rsplit('-',1)[0],validate=True)),32)
                self.assertNotEqual(checks[0],composite);self.assertEqual(checks[1:],[composite,composite])
                xmlparts=[[n.text for n in ET.fromstring(r[3]).findall('.//{*}ChecksumSHA256')] for r in requests]
                self.assertEqual(xmlparts,[[digest],[],[digest]])
                self.assertFalse(server.fixtures)

    def test_conditional_matrix_uses_only_supported_none_match_values(self):
        """Run the real ordinary conditional-write matrix; every transmitted If-None-Match must be '*', with true412/404 failures and success/delete transitions consumed in order."""
        from osv.ceph_conditional_versions import put_matrix
        put=(200,{'ETag':'"d41d8cd98f00b204e9800998ecf8427e"'},b'')
        plan=[NO,OK,put,error(412,'PreconditionFailed'),put,DEL,error(404,'NoSuchKey'),error(404,'NoSuchKey'),put,put,error(412,'PreconditionFailed'),put]+TAIL[1:]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                put_matrix(probe(worker,Path(temporary)),'basic')
                values=[v for r in server.requests for k,v in r[2].items() if k.lower()=='if-none-match']
                self.assertEqual(values,['*','*','*']);self.assertFalse(server.fixtures)

    def test_repeat_checksum_completion_can_omit_checksum_but_head_must_confirm(self):
        """After genuine first completion, allow a replay response without optional checksum only when independent HEAD checksum and full GET remain correct; reject changed checksum/body."""
        from osv.ceph_checksum_attributes import reupload_checksum
        blocks=[bytes([n])*(5*1024*1024) for n in b'ABC'];digests=[base64.b64encode(hashlib.sha256(b).digest()).decode() for b in blocks]
        total=base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(v) for v in digests)).digest()).decode()+'-3'
        create=(200,{**XML,'x-amz-checksum-algorithm':'SHA256'},CREATE[2])
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('omitted','bad_head','bad_body'):
                plan=[NO,OK,create]+[(200,{'ETag':'"part"','x-amz-checksum-sha256':d},b'') for d in digests]
                plan.extend([complete(checksum=total),complete(),(200,{'x-amz-checksum-sha256':'bad' if mode=='bad_head' else total},b'')])
                if mode!='bad_head':plan.append((200,{'ETag':'"done"'},b'X'+b''.join(blocks)[1:] if mode=='bad_body' else b''.join(blocks)))
                plan+=TAIL
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',overrides={'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    if mode=='omitted':reupload_checksum(probe(worker,Path(temporary)))
                    else:
                        with self.assertRaises(AssertionError):reupload_checksum(probe(worker,Path(temporary)))
                    self.assertFalse(server.fixtures)

    def test_s3_overflow_uses_protocol_verdict_without_erasing_source_difference(self):
        """Run the original JuiceFS-named case through real S3 workers and run_cases;416 InvalidRange passes S3, while403 or unexpected200 fails and the original filesystem mismatch remains visible."""
        from osv.objbench_cases import CASES as JUICE_CASES
        selected=next(c for c in JUICE_CASES if c.name=='juicefs_range_start_overflow')
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for response,expected in [(error(416,'InvalidRange'),'pass'),(error(403,'AccessDenied'),'fail'),(OK,'fail')]:
                with self.subTest(language=language,status=response[0]),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[OK,response],'s3') as (worker,server):
                    rows=run_cases(probe(worker,Path(temporary)).ctx,[selected])
                    self.assertEqual(rows[0]['status'],expected)
                    self.assertEqual(rows[0]['evidence']['source_matches'],response[0]==200)
                    self.assertEqual(rows[0]['evidence']['compatibility_expected'],'416 InvalidRange')

    def test_encrypted_put_client_failure_retains_independent_readback_evidence(self):
        """Trigger the real Java checksum validator with encrypted opaque ETag; preserve the failing call and independently report whether stored bytes match, without declaring corruption from the exception text."""
        from osv.ceph_encryption_objects import transfer
        for actual in (b'A',b'B'):
            headers={'x-amz-server-side-encryption':'AES256','ETag':'"9d5ed678fe57bcca610140957afab571"'}
            plan=[NO,OK,(200,headers,b''),(200,{'x-amz-server-side-encryption':'AES256'},actual)]+TAIL[1:]
            with self.subTest(actual=actual),tempfile.TemporaryDirectory() as temporary,fixture_worker('java',plan,'s3') as (worker,server):
                with self.assertRaises(SDKOperationError) as caught:transfer(probe(worker,Path(temporary)),'s3_explicit',1)
                evidence=caught.exception.evidence['independent_readback']
                self.assertEqual(evidence['matches_input'],actual==b'A')
                self.assertEqual(evidence['sha256'],hashlib.sha256(actual).hexdigest())
                self.assertFalse(server.fixtures)
