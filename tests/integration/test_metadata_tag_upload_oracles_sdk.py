"""Independent HTTP vectors for metadata, tag limits and consumed upload IDs."""
import os,tempfile,unittest,hashlib
from pathlib import Path
from sdk_fixtures import fixture_worker
from test_protocol_oracles_sdk import START,CLEAN,XML,MISSING,error,tag
from osv.scenarios import Context
from osv.suites import Probe
from osv.ceph_metadata_edges import unicode_metadata
from osv.ceph_tags import tagging
from osv.ceph_multipart import upload

def probe(w,t):
    d=Path(t);return Probe(Context(w,'oracle/',d,b'',d/'body',d/'out',{}))

@unittest.skipUnless(os.environ.get('OSV_RUN_METADATA_TAG_ORACLES')=='1','requires real SDK workers')
class MetadataTagUploadOracleTest(unittest.TestCase):
    def test_unicode_header_encodings_and_content(self):
        """Real SDKs send ASCII RFC2047; decoded B/Q/plain values must match, while mojibake, missing metadata and corrupt object fail."""
        variants=[('=?UTF-8?B?SGVsbG8gV29ybGTDqQ==?=',b'bar',True),('=?UTF-8?Q?Hello_World=C3=A9?=',b'bar',True),('=?UTF-8?B?SGVsbG8gV29ybGTDg8Kp?=',b'bar',False),(None,b'bar',False),('=?UTF-8?Q?Hello_World=C3=A9?=',b'bad',False)]
        for lang in ('java','cpp'):
            for value,body,good in variants:
                headers={} if value is None else {'x-amz-meta-meta1':value}
                plan=START+[(200,tag(b'bar'),b''),(200,headers,body)]+CLEAN
                with self.subTest(lang=lang,value=value,body=body),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,s):
                    if good:unicode_metadata(probe(w,d))
                    else:
                        with self.assertRaises((AssertionError,KeyError)):unicode_metadata(probe(w,d))
                    sent={k.lower():v for k,v in s.requests[2][2].items()}
                    self.assertEqual(sent['x-amz-meta-meta1'],'=?UTF-8?B?SGVsbG8gV29ybGTDqQ==?=');self.assertFalse(s.fixtures)

    def test_tag_limit_rejection_preserves_existing_tags(self):
        """Excess-tag rejection accepts relevant400 errors only; success, unrelated errors, deletion or partial tag updates must fail after actual GET."""
        tags=b'<Tagging><TagSet><Tag><Key>0</Key><Value>0</Value></Tag><Tag><Key>1</Key><Value>1</Value></Tag></TagSet></Tagging>'
        variants=[(error(400,c),tags,True) for c in ('InvalidTag','BadRequest','TooManyTaggings')]
        variants += [(error(400,'MalformedXML'),tags,False),(error(403,'AccessDenied'),tags,False),((200,{},b''),tags,False),(error(400,'BadRequest'),b'<Tagging><TagSet/></Tagging>',False)]
        for lang in ('java','cpp'):
            for response,state,good in variants:
                plan=[(200,tag(b'A'*(7*1024*1024)),b''),(200,{},b''),(200,XML,tags),response,(200,XML,state)]
                with self.subTest(lang=lang,response=response,state=state),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,s):
                    if good:tagging(probe(w,d),'excess')
                    else:
                        with self.assertRaises(AssertionError):tagging(probe(w,d),'excess')
                    self.assertEqual(s.requests[-1][0],'GET');self.assertFalse(s.fixtures)

    def test_wrong_customer_key_denial_with_readback_controls(self):
        """Accept only wrong-key service denials after valid-key reads; success, unrelated errors and changed data must fail."""
        from osv.ceph_sse_customer import rejected_customer_key,OTHER
        from osv.bucket_scope import BucketScope
        for lang in ('java','cpp'):
            for response,body,good in [(error(400,'InvalidRequest'),b'abc',True),(error(403,'AccessDenied'),b'abc',True),((200,{},b'abc'),b'abc',False),(error(500,'InternalError'),b'abc',False),(error(403,'AccessDenied'),b'bad',False)]:
                plan=[(200,{},b'abc'),response,(200,{},body)]
                with self.subTest(lang=lang,response=response,body=body),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,s):
                    p=probe(w,d);bucket=BucketScope(p)
                    if good:rejected_customer_key(bucket,p,'obj',OTHER,b'abc')
                    else:
                        with self.assertRaises(AssertionError):rejected_customer_key(bucket,p,'obj',OTHER,b'abc')
                    self.assertFalse(s.fixtures)

    def test_self_copy_preserves_data_for_success_and_rejection(self):
        """Run both production copy scenarios through SDKs; accept exact rejection or matching success, reject lost bytes/metadata/type and wrong result ETag."""
        from osv.ceph_copy import self_copy
        from osv.ceph_copy_contracts import basic
        headers={**tag(b'foo'),'Content-Type':'text/plain','x-amz-meta-preserved':'value'}
        copied=(200,XML,('<CopyObjectResult><ETag>'+headers['ETag']+'</ETag></CopyObjectResult>').encode())
        for lang in ('java','cpp'):
            for scoped in (False,True):
                for mode in ('success','rejected','bad_body','bad_metadata','bad_type','bad_etag','wrong_error'):
                    response=error(400,'InvalidRequest') if mode=='rejected' else error(403,'AccessDenied') if mode=='wrong_error' else (200,XML,b'<CopyObjectResult><ETag>wrong</ETag></CopyObjectResult>') if mode=='bad_etag' else copied
                    after={**headers}
                    if mode=='bad_metadata':after['x-amz-meta-preserved']='wrong'
                    if mode=='bad_type':after['Content-Type']='other/type'
                    plan=([(404,{},b''),(200,{},b'')] if scoped else [])+[(200,tag(b'foo'),b''),(200,headers,b'foo'),response,(200,after,b'bad' if mode=='bad_body' else b'foo')]+(CLEAN if scoped else [])
                    with self.subTest(lang=lang,scoped=scoped,mode=mode),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,server):
                        action=lambda:basic(probe(w,d),'self') if scoped else self_copy(probe(w,d),False)
                        if mode in ('success','rejected'):action()
                        else:
                            with self.assertRaises(AssertionError):action()
                        self.assertFalse(server.fixtures)

    def test_not_modified_uses_observed_last_modified_without_future_offset(self):
        """Feed an actual Last-Modified response into both production scenarios; expect the same HTTP date on the wire and rejection of200/403 in place of304."""
        from osv.ceph_conditions import conditional
        from osv.ceph_state_boundaries import conditional_read
        date='Wed, 01 Jan 2025 00:00:00 GMT'
        headers={**tag(b'bar'),'Last-Modified':date,'Content-Length':'3'}
        for lang in ('java','cpp'):
            for scoped in (False,True):
                for status in (304,200,403):
                    response=(status,tag(b'bar'),b'bar' if status==200 else b'')
                    plan=(START if scoped else [])+[(200,tag(b'bar'),b''),(200,headers,b'bar' if scoped else b''),response]+(CLEAN if scoped else [])
                    with self.subTest(lang=lang,scoped=scoped,status=status),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,server):
                        action=lambda:conditional_read(probe(w,d),'not_modified') if scoped else conditional(probe(w,d),'if_modified_since_ms',False)
                        if status==304:action()
                        else:
                            with self.assertRaises(AssertionError):action()
                        sent=next({k.lower():v for k,v in row[2].items()} for row in server.requests if any(k.lower()=='if-modified-since' for k in row[2]))
                        self.assertEqual(sent['if-modified-since'],date);self.assertFalse(server.fixtures)

    def test_conditional_write_matrices_read_back_each_rejection(self):
        """Run actual PUT and multipart matrices with distinct rejected data; reject412 with altered/created objects,200 success and403 instead of precondition failure."""
        from osv.ceph_conditional_versions import put_matrix
        def write_plan(multipart,body,response):
            if not multipart:return [response]
            return [(200,XML,b'<InitiateMultipartUploadResult><UploadId>matrix-id</UploadId></InitiateMultipartUploadResult>'),(200,tag(body),b''),response,(204,{},b'')]
        for lang in ('java','cpp'):
            for multipart in (False,True):
                expected=b'abc' if multipart else b''
                success=(200,XML,b'<CompleteMultipartUploadResult><ETag>"completed-1"</ETag></CompleteMultipartUploadResult>') if multipart else (200,tag(expected),b'')
                for variant in ('good','changed','accepted','denied','created'):
                    rejection=success if variant=='accepted' else error(403,'AccessDenied') if variant=='denied' else error(412,'PreconditionFailed')
                    plan=START+write_plan(multipart,expected,success)+write_plan(multipart,b'must-not-commit',rejection)+[(200,{},b'changed' if variant=='changed' else expected)]
                    if variant in ('good','created'):
                        plan+=write_plan(multipart,expected,success)+[(204,{},b'')]+write_plan(multipart,b'must-not-commit',error(404,'NoSuchKey'))+([(200,{},b'created')] if variant=='created' else [MISSING])
                        if variant=='good':
                            plan+=write_plan(multipart,b'must-not-commit',error(404,'NoSuchKey'))+[MISSING]
                            plan+=write_plan(multipart,expected,success)*2+write_plan(multipart,b'must-not-commit',error(412,'PreconditionFailed'))+[(200,{},expected)]+write_plan(multipart,expected,success)
                    plan+=CLEAN
                    with self.subTest(lang=lang,multipart=multipart,variant=variant),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,server):
                        if variant=='good':put_matrix(probe(w,d),'basic',multipart)
                        else:
                            with self.assertRaises(AssertionError) as caught:put_matrix(probe(w,d),'basic',multipart)
                            self.assertIn('object_state_matches',caught.exception.evidence)
                        self.assertFalse(server.fixtures)

    def test_empty_cursor_requires_invalid_argument(self):
        """Transmit an explicit empty continuation token with both real SDKs; only400/InvalidArgument passes, omitted-token-like200 or unrelated failures do not."""
        from osv.ceph_list_exact import token_vector
        for lang in ('java','cpp'):
            for response,good in [(error(400,'InvalidArgument'),True),(error(403,'AccessDenied'),False),((200,XML,b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'),False)]:
                plan=START+[(200,tag(k),b'') for k in (b'bar',b'baz',b'foo',b'quxx')]+[response]+CLEAN[:2]+[(204,{},b'')]*3+CLEAN[2:]
                with self.subTest(lang=lang,response=response),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,s):
                    if good:token_vector(probe(w,d),'empty')
                    else:
                        with self.assertRaises(AssertionError):token_vector(probe(w,d),'empty')
                    self.assertIn('continuation-token=',s.requests[6][1]);self.assertFalse(s.fixtures)

    def test_consumed_upload_id_and_original_object_state(self):
        """Use real SDK create/part outputs in abort and completion; reject wrong status, accepted mutation and changed state, preserving valid ID/ETag requests."""
        for lang in ('java','cpp'):
            for mode in ('abort_missing','missing_upload'):
                for variant in (('good','bad_status','accepted','changed','source_lost','wrong_source_id') if mode=='abort_missing' else ('good','bad_status','accepted','changed')):
                    response=error(404,'NoSuchUpload') if variant in ('good','changed','source_lost','wrong_source_id') else error(400,'InvalidArgument') if variant=='bad_status' else (200,XML,b'<CompleteMultipartUploadResult><ETag>"done"</ETag></CompleteMultipartUploadResult>')
                    state=(200,{},b'changed') if variant=='changed' else (200,{},b'') if mode=='abort_missing' else MISSING
                    plan=START+([(200,tag(b''),b'')] if mode=='abort_missing' else [])
                    plan += [(200,XML,b'<InitiateMultipartUploadResult><UploadId>real-issued-id</UploadId></InitiateMultipartUploadResult>')]
                    if mode=='missing_upload':plan += [(200,{'ETag':'"actual-part-etag"'},b''),(204,{},b'')]
                    plan += [response,state]
                    if mode=='abort_missing' and variant in ('good','source_lost','wrong_source_id'):
                        upload_xml=b'' if variant=='source_lost' else b'<Upload><Key>upload-id-source</Key><UploadId>'+ (b'wrong-id' if variant=='wrong_source_id' else b'real-issued-id') +b'</UploadId></Upload>'
                        plan.append((200,XML,b'<ListMultipartUploadsResult>'+upload_xml+b'</ListMultipartUploadsResult>'))
                    plan += [error(404,'NoSuchUpload') if mode=='missing_upload' else (204,{},b'')]+(CLEAN[:2]+[(204,{},b'')]+CLEAN[2:] if mode=='abort_missing' else CLEAN)
                    with self.subTest(lang=lang,mode=mode,variant=variant),tempfile.TemporaryDirectory() as d,fixture_worker(lang,plan,'s3') as (w,s):
                        if variant=='good':upload(probe(w,d),mode)
                        else:
                            with self.assertRaises(AssertionError) as caught:upload(probe(w,d),mode)
                            if variant in ('source_lost','wrong_source_id'):self.assertIn('source upload preserved',str(caught.exception))
                            else:self.assertEqual(caught.exception.evidence['operation'],'AbortMultipartUpload' if mode=='abort_missing' else 'CompleteMultipartUpload')
                        requests=[r for r in s.requests if 'uploadId=' in r[1]]
                        self.assertTrue(all('real-issued-id' in r[1] for r in requests))
                        if mode=='abort_missing':
                            deletes=[r for r in requests if r[0]=='DELETE']
                            self.assertIn('/mymultipart?',deletes[0][1]);self.assertIn('/upload-id-source?',deletes[-1][1])
                        if mode=='missing_upload':self.assertIn(b'actual-part-etag',next(r[3] for r in requests if r[0]=='POST'))
                        self.assertFalse(s.fixtures)
