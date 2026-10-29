"""Exercise corrected protocol oracles with real SDK transport and independent response vectors."""
import os,tempfile,unittest,hashlib
from pathlib import Path
from urllib.parse import unquote,urlsplit
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe
from osv.ceph_metadata_tail import unreadable
from osv.ceph_bucket_inventory import anonymous
from osv.ceph_request_headers import object_headers
from osv.ceph_conditional_versions import delete_match

XML={'Content-Type':'application/xml'}
EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
def tag(body):return {'ETag':'"'+hashlib.md5(body).hexdigest()+'"'}
TAG=tag(b'')
def error(status,code):return status,XML,f'<Error><Code>{code}</Code></Error>'.encode()
MISSING=error(404,'NoSuchKey')
START=[(404,{},b''),(200,{},b'')]
CLEAN=[(200,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]

@unittest.skipUnless(os.environ.get('OSV_RUN_PROTOCOL_ORACLE_TESTS')=='1','requires built SDK workers')
class ProtocolOracleSdkTest(unittest.TestCase):
    """Verify requests reach real SDKs and production scenarios reject independent semantic counterexamples."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def probe(self,worker,d):return Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{}))

    def test_unicode_key_is_encoded_once_and_content_must_match(self):
        """A missing valid Unicode key returns404, then stored bytes must round-trip; corrupt or absent content must fail with cleanup."""
        for language in self.languages():
            for body in (b'unicode-key-content',b'corrupt',None):
                get=MISSING if body is None else (200,{},body)
                plan=START+[MISSING,(200,tag(b'unicode-key-content'),b''),get]+CLEAN
                with self.subTest(language=language,body=body),tempfile.TemporaryDirectory() as t,fixture_worker(language,plan,'s3') as (w,s):
                    p=self.probe(w,Path(t))
                    if body==b'unicode-key-content':unreadable(p)
                    else:
                        with self.assertRaises((AssertionError,RuntimeError)):unreadable(p)
                    for req in s.requests[2:5]:self.assertTrue(unquote(urlsplit(req[1]).path).endswith('/\xae\x8a-'))
                    self.assertEqual(s.requests[3][3],b'unicode-key-content');self.assertFalse(s.fixtures)

    def test_anonymous_list_requires_actual_denial_not_empty_success(self):
        """Genuine unsigned requests pass only403/AccessDenied; empty/nonempty200 and unrelated rejection codes fail."""
        variants=[(error(403,'AccessDenied'),True),(error(403,'InvalidAccessKeyId'),False),(error(500,'InternalError'),False),
                  ((200,XML,b'<ListAllMyBucketsResult><Buckets/></ListAllMyBucketsResult>'),False),
                  ((200,XML,b'<ListAllMyBucketsResult><Buckets><Bucket><Name>leaked</Name></Bucket></Buckets></ListAllMyBucketsResult>'),False)]
        for language in self.languages():
            for response,good in variants:
                with self.subTest(language=language,response=response),tempfile.TemporaryDirectory() as t,fixture_worker(language,[response],'s3') as (w,s):
                    p=self.probe(w,Path(t))
                    if good:anonymous(p)
                    else:
                        with self.assertRaises(AssertionError):anonymous(p)
                    self.assertNotIn('authorization',{k.lower():v for k,v in s.requests[0][2].items()});self.assertFalse(s.fixtures)

    def test_unknown_expectation_requires_matching_object_state(self):
        """Accept200 with exact stored bytes or417 with absent object; reject other errors, success with bad bytes and417 after publishing an object."""
        variants=[((200,TAG,b''),(200,{},b''),True), (error(417,'ExpectationFailed'),MISSING,True),
                  (error(403,'AccessDenied'),MISSING,False),(error(500,'InternalError'),MISSING,False),
                  (error(417,'ExpectationFailed'),(200,{},b''),False),((200,TAG,b''),(200,{},b'bad'),False)]
        for language in self.languages():
            for response,get,good in variants:
                with self.subTest(language=language,response=response[0],state=get[0],body=get[2]),tempfile.TemporaryDirectory() as t,fixture_worker(language,START+[response,get]+CLEAN,'s3') as (w,s):
                    p=self.probe(w,Path(t))
                    if good:object_headers(p,{'Expect':'200'})
                    else:
                        with self.assertRaises((AssertionError,RuntimeError)):object_headers(p,{'Expect':'200'})
                    headers={k.lower():v for k,v in s.requests[2][2].items()}
                    self.assertEqual(headers['expect'],'200');self.assertEqual(s.requests[3][0],'GET');self.assertFalse(s.fixtures)

    def test_conditional_delete_rejection_and_success_both_require_readback(self):
        """Run the full production DELETE scenario: exact rejection preserves bytes, accepted deletion removes them, missing conditional deletes reject; false responses or state fail."""
        for language in self.languages():
            for mode in ('good','wrong_delete_succeeds','rejection_but_deleted','wrong_bytes','success_but_exists','missing_returns_success'):
                wrong=(204,{},b'') if mode=='wrong_delete_succeeds' else error(412,'PreconditionFailed')
                first_read=MISSING if mode in ('wrong_delete_succeeds','rejection_but_deleted') else (200,{},b'bad' if mode=='wrong_bytes' else b'conditional-delete-content')
                plan=START+[(200,tag(b'conditional-delete-content'),b''),wrong,first_read]
                if mode in ('good','success_but_exists','missing_returns_success'):
                    plan += [(204,{},b''),(200,{},b'still here') if mode=='success_but_exists' else MISSING]
                if mode in ('good','missing_returns_success'):
                    plan += [((204,{},b'') if mode=='missing_returns_success' else MISSING),MISSING]
                if mode=='good':plan += [MISSING,MISSING,(200,tag(b'wildcard-delete-content'),b''),(204,{},b''),MISSING]
                plan += CLEAN
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as t,fixture_worker(language,plan,'s3') as (w,s):
                    p=self.probe(w,Path(t))
                    if mode=='good':delete_match(p,'plain')
                    else:
                        with self.assertRaises(AssertionError) as raised:delete_match(p,'plain')
                        if mode in ('wrong_delete_succeeds','rejection_but_deleted','wrong_bytes','missing_returns_success'):
                            evidence=raised.exception.evidence
                            self.assertEqual(evidence['operation'],'DeleteObject')
                            self.assertEqual(evidence['object_state_matches'],mode=='missing_returns_success')
                            self.assertEqual(evidence['rejection_matches'],mode in ('rejection_but_deleted','wrong_bytes'))
                    self.assertEqual(s.requests[4][0],'GET');self.assertFalse(s.fixtures)

    def test_copy_range_separates_source_size_and_preserves_strict_bounds(self):
        """Run production range scenario: valid >5MiB source/control must round-trip, both overflow bounds reject, unrelated400/200 and published destinations fail; all uploads abort."""
        from osv.ceph_multipart_copy import invalid_range
        create=(200,XML,b'<InitiateMultipartUploadResult><UploadId>id</UploadId></InitiateMultipartUploadResult>')
        copied=(200,XML,b'<CopyPartResult><ETag>"part"</ETag></CopyPartResult>')
        complete=(200,XML,b'<CompleteMultipartUploadResult><ETag>"completed"</ETag></CompleteMultipartUploadResult>')
        variants=[('argument',error(400,'InvalidArgument'),MISSING,True),('range',error(416,'InvalidRange'),MISSING,True),
                  ('wrong-code',error(400,'AccessDenied'),MISSING,False),('wrong-pair',error(416,'InvalidArgument'),MISSING,False),
                  ('service-error',error(500,'InternalError'),MISSING,False),('accepted',copied,MISSING,False),
                  ('published',error(416,'InvalidRange'),(200,{},b'A'),False),('end-accepted',copied,MISSING,False)]
        for language in self.languages():
            for name,reject,get,good in variants:
                plan=START+[(200,tag(b'A'*(5*1024*1024+1)),b''),create,copied,complete,(200,{},b'AAAAA'),error(404,'NoSuchUpload')]
                if name=='end-accepted':plan += [create,error(400,'InvalidArgument'),MISSING,(204,{},b'')]
                plan += [create,reject,get,(204,{},b'')]
                if good:plan += [create,reject,get,(204,{},b'')]
                # Source and both destination keys are tracked before cleanup.
                plan += [(200,{},b''),(204,{},b''),(204,{},b''),(204,{},b''),(200,XML,EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=language,name=name),tempfile.TemporaryDirectory() as t,fixture_worker(language,plan,'s3',{'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (w,s):
                    p=self.probe(w,Path(t))
                    if good:invalid_range(p)
                    else:
                        with self.assertRaises(AssertionError):invalid_range(p)
                    self.assertEqual(len(s.requests[2][3]),5*1024*1024+1)
                    ranges=[{k.lower():v for k,v in x[2].items()}['x-amz-copy-source-range'] for x in s.requests if any(k.lower()=='x-amz-copy-source-range' for k in x[2])]
                    expected=['bytes=0-4','bytes=5242881-5242886']
                    if good or name=='end-accepted':expected+=['bytes=0-5242886']
                    self.assertEqual(ranges,expected);self.assertFalse(s.fixtures)

    def test_bucket_unknown_expectation_rejects_only417_and_confirms_absence(self):
        """Use real CreateBucket with unknown Expect:417 plus confirmed absence passes; unrelated403 and417 with an existing bucket fail, including cleanup observation."""
        from osv.ceph_request_headers import bucket_headers
        for language in self.languages():
            for status,exists,good in ((417,False,True),(403,False,False),(417,True,False)):
                plan=[(404,{},b''),error(status,'ExpectationFailed' if status==417 else 'AccessDenied')]
                if status==417:plan += [(200 if exists else 404,{},b'')]
                plan += [(404,{},b'')]
                with self.subTest(language=language,status=status,exists=exists),tempfile.TemporaryDirectory() as t,fixture_worker(language,plan,'s3') as (w,s):
                    p=self.probe(w,Path(t))
                    if good:bucket_headers(p,{'Expect':'200'})
                    else:
                        with self.assertRaises(AssertionError):bucket_headers(p,{'Expect':'200'})
                    self.assertFalse(s.fixtures)

    def test_anonymous_redirect_and_nonxml_are_execution_failures(self):
        """Feed genuine SDK a redirect or malformed webpage; neither is a compatibility pass or a bucket-authorization mismatch, and unsigned requests stay unsigned."""
        from osv.scenarios import SDKOperationError
        for language in self.languages():
            for response in ((307,{},b''),(200,{'Content-Type':'text/html'},b'<html attribute=broken>')):
                with self.subTest(language=language,status=response[0]),tempfile.TemporaryDirectory() as t,fixture_worker(language,[response],'s3') as (w,s):
                    with self.assertRaises(SDKOperationError) as raised:anonymous(self.probe(w,Path(t)))
                    self.assertIn(raised.exception.failure_category,('transport_or_sdk','sdk_response_decoding'))
                    self.assertFalse(s.fixtures)
