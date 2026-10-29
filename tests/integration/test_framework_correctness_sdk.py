"""Exercise genuine SDK wire output and production failure consumers without cloud fixtures."""
import base64, hashlib, json, os, tempfile, unittest
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from osv.scenarios import Context
from osv.suites import Probe, run_cases, case, CaseNotRun
from osv.bucket_scope import BucketScope
from osv.ceph_checksum_attributes import checksum_upload
from osv.ceph_list_exact import check_list_field, ListContractError
from osv.ceph_select_runtime import checked
from osv.ceph_metadata_edges import content_encoding
from osv.response_contract import MissingResponseField
from osv.report import functional_detail

XML={'Content-Type':'application/xml'}
@unittest.skipUnless(os.environ.get('OSV_RUN_FRAMEWORK_CORRECTNESS_TESTS')=='1','requires built SDK workers')
class FrameworkCorrectnessSdkTest(unittest.TestCase):
    """Validate final requests, unchanged strict failures and diagnostic evidence using independent raw HTTP responses."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')

    def test_delete_md5_covers_exact_sdk_xml_for_quiet_versions_unicode_and_empty(self):
        """Hash the actual bytes received from both SDKs; MD5 must match final XML for empty/special/versioned/1000-key requests without altering requested key order or quiet mode."""
        variants=[([],False),([{'key':'a<&雪'},{'key':'a<&雪'},{'key':'version','version_id':'v<&1'}],True),([{'key':str(i)} for i in range(1000)],False)]
        for language in self.languages():
            for objects,quiet in variants:
                with self.subTest(language=language,n=len(objects),quiet=quiet),fixture_worker(language,[(200,XML,b'<DeleteResult/>')],'s3',{'OSV_REQUEST_CHECKSUM_CALCULATION':'when_required'}) as (worker,server):
                    result=worker.call({'op':'delete_many','objects':objects,'quiet':quiet})
                    self.assertTrue(result['ok'],result)
                    _,_,headers,body=server.requests[0];headers={k.lower():v for k,v in headers.items()}
                    self.assertEqual(headers.get('content-md5'),base64.b64encode(hashlib.md5(body).digest()).decode())
                    root=ET.fromstring(body)
                    self.assertEqual([e.findtext('{*}Key') for e in root.findall('{*}Object')],[x['key'] for x in objects])
                    self.assertEqual(root.findtext('{*}Quiet'),str(quiet).lower())
                    self.assertFalse(server.fixtures)

    def test_required_checksum_field_uses_real_sdk_output_and_reports_path(self):
        """Feed create-upload XML through each SDK into the actual upload context: missing algorithm must fail with its path, matching algorithm passes, wrong algorithm fails, and all paths abort the real upload."""
        for language in self.languages():
            for algorithm in (None,'SHA256','CRC32'):
                headers={} if algorithm is None else {'x-amz-checksum-algorithm':algorithm}
                plan=[(200,headers,b'<InitiateMultipartUploadResult><UploadId>id</UploadId></InitiateMultipartUploadResult>'),(204,{},b'')]
                with self.subTest(language=language,algorithm=algorithm),tempfile.TemporaryDirectory() as temp,fixture_worker(language,plan,'s3') as (worker,server):
                    d=Path(temp);p=Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{},object_namespace=True));bucket=BucketScope(p)
                    if algorithm=='SHA256':
                        with checksum_upload(bucket,'obj','SHA256') as ident:self.assertEqual(ident,'id')
                    else:
                        with self.assertRaises(AssertionError) as raised:
                            with checksum_upload(bucket,'obj','SHA256'):pass
                        if algorithm is None:
                            self.assertIsInstance(raised.exception,MissingResponseField)
                            self.assertEqual(raised.exception.evidence['missing_response_field'],'$.checksum.ChecksumAlgorithm')
                            self.assertEqual(raised.exception.evidence['operation'],'mp_create_metadata')
                    self.assertEqual([x[0] for x in server.requests],['POST','DELETE']);self.assertFalse(server.fixtures)

    def test_list_wire_evidence_preserves_sdk_serialization_and_strict_echo(self):
        """Observe actual boolean query serialization, then pass SDK output into the strict StartAfter check: missing/wrong echoes fail, exact echo passes; credentials and capture state must not leak."""
        for language in self.languages():
            for echo in (None,'scope/a','wrong'):
                root=ET.Element('ListBucketResult');ET.SubElement(root,'IsTruncated').text='false'
                if echo is not None:ET.SubElement(root,'StartAfter').text=echo
                plan=[(200,XML,ET.tostring(root)),(200,XML,b'<ListBucketResult/>')]
                with self.subTest(language=language,echo=echo),fixture_worker(language,plan,'s3') as (worker,server):
                    result=worker.call({'op':'list_v2_observed','prefix':'scope/','start_after':'scope/a','fetch_owner':True,'capture_response_body':True})
                    self.assertTrue(result['ok'],result)
                    actual=parse_qs(urlsplit(server.requests[0][1]).query)
                    self.assertEqual(result['request_query']['fetch-owner'],actual['fetch-owner'][0])
                    self.assertEqual(result['request_query']['start-after'],'scope/a')
                    if echo=='scope/a':check_list_field(result,'start_after','scope/a')
                    else:
                        with self.assertRaises(ListContractError) as raised:check_list_field(result,'start_after','scope/a')
                        self.assertIn('request_query',raised.exception.evidence)
                        self.assertIn('response_body_b64',raised.exception.evidence)
                    self.assertNotIn('Authorization',json.dumps(result.get('request_query',{})))
                    second=worker.call({'op':'list_v2_observed','prefix':'other/'})
                    self.assertNotIn('request_query',second);self.assertNotIn('response_body_b64',second)
                    self.assertFalse(server.fixtures)

    def test_select_service_rejection_remains_failed_with_status_code_and_phase(self):
        """Pass real Select405 and PUT403 responses into production exception/run_cases/report consumers; both remain fail with actual service code/status and correct failing operation, never Python execution_error or pass."""
        for language in self.languages():
            for operation,op,status,code in [('SelectObjectContent','select',405,'MethodNotAllowed'),('PutObject','put_http',403,'AccessDenied')]:
                xml=f'<Error><Code>{code}</Code><Message>fixture rejection</Message></Error>'.encode()
                with self.subTest(language=language,op=op),tempfile.TemporaryDirectory() as temp,fixture_worker(language,[(status,XML,xml)],'s3') as (worker,server):
                    d=Path(temp);(d/'body').write_bytes(b'x')
                    request={'op':op,'key':'obj','body_path':str(d/'body'),'output_path':str(d/'out')}
                    if op=='select':request['input']={'Bucket':'fixture-1250000000','Key':'obj','Expression':'select * from s3object','ExpressionType':'SQL','InputSerialization':{'CSV':{'FileHeaderInfo':'NONE'}},'OutputSerialization':{'CSV':{}}}
                    def execute(p):return checked(worker.call(request),operation)
                    ctx=Context(worker,'fixture/',d,b'',d/'body',d/'out',{})
                    row=run_cases(ctx,[case('fixture_rejection','test','success','',execute,'fixture')])[0]
                    self.assertEqual(row['status'],'fail');self.assertEqual(row['failure_category'],'service_error' if status==405 else 'access_rejected')
                    self.assertEqual(row['evidence']['status_code'],status);self.assertEqual(row['evidence']['error_code'],code)
                    self.assertEqual(row['evidence']['operation'],operation);self.assertIn(code,functional_detail(row))
                    self.assertFalse(server.fixtures)

    def test_unverified_aws_chunked_contract_never_sends_fabricated_request(self):
        """Invoke the production encoding case with a live SDK worker; unavailable signed framing must be explicit not_run and send zero requests."""
        for language in self.languages():
            with tempfile.TemporaryDirectory() as temp,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(temp);ctx=Context(worker,'fixture/',d,b'',d/'body',d/'out',{})
                row=run_cases(ctx,[case('fixture_encoding','test','encoding preserved','',content_encoding,'fixture')])[0]
                self.assertEqual(row['status'],'not_run');self.assertIn('signed aws-chunked',row['missing_requirements'][0]);self.assertEqual(server.requests,[])

    def test_bulk_condition_result_requires_exact_error_or_deleted_row(self):
        """Decode independent DeleteResult XML through both SDKs into the production oracle; absent/duplicate/wrong key/code/mixed rows fail and exact rejection/success pass."""
        from osv.ceph_conditional_versions import check_bulk_condition_result
        error='<Error><Key>obj</Key><Code>PreconditionFailed</Code></Error>'
        deleted='<Deleted><Key>obj</Key></Deleted>'
        variants=[(error,True,True),(deleted,False,True),('',True,False),(deleted,True,False),
                  (error+error,True,False),(error+deleted,True,False),(error.replace('obj','other'),True,False),
                  (error.replace('PreconditionFailed','AccessDenied'),True,False),('',False,False),
                  (deleted+deleted,False,False),(error+deleted,False,False)]
        for language in self.languages():
            for xml,rejected,good in variants:
                wire=('<DeleteResult>'+xml+'</DeleteResult>').encode()
                with self.subTest(language=language,xml=xml,rejected=rejected),fixture_worker(language,[(200,XML,wire)],'s3') as (worker,server):
                    result=worker.call({'op':'delete_many_versions','objects':[{'key':'obj'}]})
                    self.assertTrue(result['ok'],result)
                    if good:check_bulk_condition_result(result,'obj',rejected)
                    else:
                        with self.assertRaises(AssertionError) as raised:check_bulk_condition_result(result,'obj',rejected)
                        self.assertEqual(raised.exception.evidence['operation'],'DeleteObjects')
                        self.assertEqual(raised.exception.evidence['errors'],result['errors'])
                    self.assertFalse(server.fixtures)

    def test_empty_completion_has_xml_root_and_rejects_nonempty_input(self):
        """Inspect real SDK bytes: explicit empty XML has no Part children; nonempty input fails locally and normal completion preserves its part."""
        error=b'<Error><Code>MalformedXML</Code></Error>'
        for language in self.languages():
            with self.subTest(language=language),fixture_worker(language,[(400,XML,error),(200,XML,b'<CompleteMultipartUploadResult><ETag>"ok"</ETag></CompleteMultipartUploadResult>')],'s3') as (worker,server):
                result=worker.call({'op':'mp_complete_empty','key':'obj','upload_id':'id'})
                self.assertFalse(result['ok']);root=ET.fromstring(server.requests[0][3])
                self.assertEqual(root.tag,'{http://s3.amazonaws.com/doc/2006-03-01/}CompleteMultipartUpload')
                self.assertEqual(list(root),[])
                parts=[{'part_number':1,'etag':'part-tag'}]
                invalid=worker.call({'op':'mp_complete_empty','key':'obj','upload_id':'id','parts':parts})
                self.assertFalse(invalid['ok']);self.assertEqual(len(server.requests),1)
                normal=worker.call({'op':'mp_complete_observed','key':'obj','upload_id':'id','parts':parts})
                self.assertTrue(normal['ok'],normal)
                self.assertEqual(ET.fromstring(server.requests[1][3]).findtext('{*}Part/{*}ETag'),'part-tag')
                self.assertFalse(server.fixtures)

    def test_negative_put_always_reads_state_even_after_unexpected_response(self):
        """Run the complete production conditional PUT case: response/state cross product passes only exact rejection plus unchanged bytes, and all paths read back and clean up."""
        from osv.ceph_conditional_writes import put_condition
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for language in self.languages():
            for rejected in (True,False):
                for preserved in (True,False):
                    response=(412,XML,b'<Error><Code>PreconditionFailed</Code></Error>') if rejected else (200,{'ETag':'"b24d4be77066cb0bf70247b7d9176ebb"'},b'')
                    plan=[(404,{},b''),(200,{},b''),(200,{'ETag':'"37b51d194a7513e45b56f6524f2d51f2"'},b''),(200,{'ETag':'"37b51d194a7513e45b56f6524f2d51f2"'},b'bar'),response,
                          (200,{},b'bar' if preserved else b'zar'),(200,{},b''),(204,{},b''),(200,XML,empty),(204,{},b''),(404,{},b'')]
                    with self.subTest(language=language,rejected=rejected,preserved=preserved),tempfile.TemporaryDirectory() as temp,fixture_worker(language,plan,'s3') as (worker,server):
                        d=Path(temp);p=Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{}))
                        if rejected and preserved:put_condition(p,'match_bad')
                        else:
                            with self.assertRaises(AssertionError) as raised:put_condition(p,'match_bad')
                            self.assertEqual(raised.exception.evidence['rejection_matches'],rejected)
                            self.assertEqual(raised.exception.evidence['object_state_matches'],preserved)
                        self.assertEqual(server.requests[5][0],'GET');self.assertFalse(server.fixtures)

    def test_absent_readback_requires_nosuchkey_not_access_denied_or_empty_object(self):
        """Use real SDK GET output in the production negative oracle; only 404/NoSuchKey proves absence, while 403, wrong 404 code and zero-byte success fail."""
        from osv.rejected_mutation import check_rejected_mutation
        variants=[(404,'NoSuchKey',None,True),(404,'NoSuchBucket',None,False),(403,'AccessDenied',None,False),(200,None,b'',False)]
        for language in self.languages():
            for status,code,body,good in variants:
                wire=body if body is not None else f'<Error><Code>{code}</Code></Error>'.encode()
                with self.subTest(language=language,status=status,code=code),tempfile.TemporaryDirectory() as temp,fixture_worker(language,[(status,XML,wire)],'s3') as (worker,server):
                    d=Path(temp);(d/'out').write_bytes(b'stale');bucket=BucketScope(Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{})))
                    response={'ok':False,'status_code':400,'error_code':'MalformedXML'}
                    if good:check_rejected_mutation(bucket,'obj',response,400,'MalformedXML')
                    else:
                        with self.assertRaises(AssertionError) as raised:check_rejected_mutation(bucket,'obj',response,400,'MalformedXML')
                        self.assertFalse(raised.exception.evidence['object_state_matches'])
                    self.assertFalse(server.fixtures)

    def test_checked_page_collects_wire_without_caller_capture_flag(self):
        """Invoke the production pagination entry point through both SDKs: missing StartAfter fails with actual token/query/XML evidence, not helper-only synthetic evidence."""
        from osv.ceph_list_exact import checked_page
        for language in self.languages():
            with tempfile.TemporaryDirectory() as temp,fixture_worker(language,[(200,XML,b'<ListBucketResult><ContinuationToken>opaque-token</ContinuationToken><IsTruncated>false</IsTruncated></ListBucketResult>')],'s3') as (worker,server):
                d=Path(temp);bucket=BucketScope(Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{})))
                with self.assertRaises(ListContractError) as raised:
                    checked_page(bucket,2,{'token':'opaque-token','start_after':'bar'},checks={'start_after':'bar'})
                evidence=raised.exception.evidence
                self.assertEqual(evidence['request_query']['continuation-token'],'opaque-token')
                self.assertEqual(evidence['request_query']['start-after'],'bar')
                self.assertIn(b'<ListBucketResult>',base64.b64decode(evidence['response_body_b64']))
                self.assertFalse(server.fixtures)

    def test_bad_part_checksum_runs_without_init_echo_and_keeps_strict_rejection(self):
        """Run the independent production checksum case with absent init echo: actual bad digest reaches UploadPart, exact BadDigest passes, acceptance/wrong rejection fails and upload is aborted."""
        from osv.ceph_checksum_attributes import multipart_bad_part_digest
        empty=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for language in self.languages():
            for status,code in ((400,'BadDigest'),(200,None),(400,'InvalidRequest')):
                part=(status,{'ETag':'"part"'} if code is None else XML,b'' if code is None else f'<Error><Code>{code}</Code></Error>'.encode())
                plan=[(404,{},b''),(200,{},b''),(200,XML,b'<InitiateMultipartUploadResult><UploadId>id</UploadId></InitiateMultipartUploadResult>'),part,(204,{},b''),(200,{},b''),(204,{},b''),(200,XML,empty),(204,{},b''),(404,{},b'')]
                with self.subTest(language=language,status=status,code=code),tempfile.TemporaryDirectory() as temp,fixture_worker(language,plan,'s3') as (worker,server):
                    d=Path(temp);p=Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{}))
                    if code=='BadDigest':multipart_bad_part_digest(p)
                    else:
                        with self.assertRaises(AssertionError):multipart_bad_part_digest(p)
                    request=server.requests[3];headers={k.lower():v for k,v in request[2].items()}
                    self.assertEqual(request[3],b'foo');self.assertEqual(headers['x-amz-checksum-sha256'],base64.b64encode(bytes(32)).decode())
                    self.assertNotEqual(headers['x-amz-checksum-sha256'],base64.b64encode(hashlib.sha256(b'foo').digest()).decode())
                    self.assertEqual(server.requests[4][0],'DELETE');self.assertFalse(server.fixtures)

    def test_transport_failure_keeps_readback_but_is_not_a_protocol_verdict(self):
        """Drop the real PUT connection, then return real GET bytes: the production oracle must fail as transport_or_sdk and retain unchanged-state evidence."""
        from sdk_fixtures import FixtureHandler
        from osv.rejected_mutation import check_rejected_mutation
        class DropPutHandler(FixtureHandler):
            """Close PUT without an HTTP response; subsequent GET uses the independent raw response fixture."""
            def do_PUT(self):
                self.close_connection=True
                self.connection.shutdown(__import__('socket').SHUT_RDWR)
                self.connection.close()
        for language in self.languages():
            with tempfile.TemporaryDirectory() as temp,fixture_worker(language,[(200,{},b'bar')],'s3',handler=DropPutHandler) as (worker,server):
                d=Path(temp);(d/'body').write_bytes(b'zar')
                bucket=BucketScope(Probe(Context(worker,'fixture/',d,b'',d/'body',d/'out',{})))
                result=bucket.raw('put_http',key='obj',body_path=str(d/'body'))
                self.assertFalse(result['ok']);self.assertLess(result.get('status_code') or 0,200)
                with self.assertRaises(AssertionError) as raised:
                    check_rejected_mutation(bucket,'obj',result,412,'PreconditionFailed',b'bar',operation='PutObject')
                self.assertEqual(raised.exception.failure_category,'transport_or_sdk')
                self.assertTrue(raised.exception.evidence['object_state_matches'])
                self.assertTrue(raised.exception.evidence['response_error'])
                self.assertFalse(server.fixtures)
