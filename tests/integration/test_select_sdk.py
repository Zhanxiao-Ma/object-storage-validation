"""Official Java/C++ Select decoders consume independent CRC-framed AWS eventstream fixtures."""
import ast,json,os,struct,tempfile,unittest,zlib
from collections import deque
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker,FixtureHandler
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun,run_cases
from osv.ceph_select_runtime import Runtime,Client,ClientError,EventStreamError,execute,payload
from osv import ceph_select_sources as source
from osv.ceph_select_register import SOURCES,CASES

OK=(200,{},b'');NO=(404,{},b'');DEL=(204,{},b'')
EMPTY=(200,{},b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>')
ECHO=b'__fixture_previous_actual_put_body__'


def frame(kind,payload=b'',message='event',extra=None):
    """Encode independent wire frames with published AWS eventstream header/prelude/message CRCs; official SDKs remain the sole decoder."""
    fields={':message-type':message,**({':event-type':kind,':content-type':'text/xml' if kind in ('Stats','Progress') else 'application/octet-stream'} if message=='event' else {}),**(extra or {})}
    headers=b''
    for key,value in fields.items():
        key=key.encode();value=value.encode();headers+=bytes([len(key)])+key+b'\x07'+struct.pack('>H',len(value))+value
    prelude=struct.pack('>II',16+len(headers)+len(payload),len(headers));body=prelude+struct.pack('>I',zlib.crc32(prelude))+headers+payload
    return body+struct.pack('>I',zlib.crc32(body))


def stream(*frames):return 200,{'Content-Type':'application/vnd.amazon.eventstream'},b''.join(frames)
def details(kind,scanned,returned):return frame(kind,('<'+kind+'><BytesScanned>'+str(scanned)+'</BytesScanned><BytesProcessed>'+str(scanned)+'</BytesProcessed><BytesReturned>'+str(returned)+'</BytesReturned></'+kind+'>').encode())


class DroppedCreateHandler(FixtureHandler):
    """Accept a genuine CreateBucket request, drop its response, then replay independent HEAD/ACL proof inputs; implement no storage state machine."""
    def respond(self):
        if self.command=='PUT' and self.server.fixtures and self.server.fixtures[0][0]==0:
            import socket
            body=self.chunks(self.rfile) if 'chunked' in self.headers.get('Transfer-Encoding','').lower() else self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.server.requests.append((self.command,self.path,dict(self.headers),body));self.server.fixtures.popleft()
            self.close_connection=True
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            self.connection.close();return
        super().respond()


class ProducerReplay(deque):
    """Feed a captured actual PUT body directly to the official GET consumer; implement no object map, query evaluator, or Select decoder."""
    def __init__(self,values,server):super().__init__(values);self.server=server
    def popleft(self):
        status,headers,body=super().popleft()
        if body==ECHO:body=next(request[3] for request in reversed(self.server.requests) if request[0]=='PUT')
        return status,headers,body


def context(worker,path):return Probe(Context(worker,'unused/',path,b'',path/'body',path/'out',{}))
def request():return {'Bucket':'osv-fixture','Key':'input.csv','ExpressionType':'SQL','Expression':'select _1 from s3object;','InputSerialization':{'CSV':{'FileHeaderInfo':'NONE','RecordDelimiter':'\n','FieldDelimiter':',','QuoteCharacter':'"','QuoteEscapeCharacter':'\\'},'CompressionType':'NONE'},'OutputSerialization':{'CSV':{}},'RequestProgress':{'Enabled':True}}


@unittest.skipUnless(os.environ.get('OSV_RUN_SELECT_TESTS')=='1','enable official SDK Select eventstream fixtures')
class SelectSdkTest(unittest.TestCase):
    """Use only actual SDK requests/events and raw input frames; assert native bytes, event order and source error timing."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def test_actual_events_preserve_records_offsets_progress_stats_cont_and_end(self):
        """Send independent Records/Progress/Stats/Cont/End wire frames; expect actual SDK callbacks to preserve bytes and event boundaries without concatenating UTF-8 first."""
        wire=[frame('Records',b'a,\n'),details('Progress',12,3),frame('Records',b'\xe4'),frame('Records',b'\xb8\xad'),frame('Cont'),details('Stats',12,6),frame('End')]
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[stream(*wire)],'s3') as (worker,server):
                path=Path(temporary)/'records';result=worker.call({'op':'select','input':request(),'output_path':str(path)})
                self.assertTrue(result['ok'],result);self.assertEqual(result['status_code'],200);self.assertEqual(path.read_bytes(),b'a,\n\xe4\xb8\xad')
                self.assertEqual([event['type'] for event in result['events']],['Records','Progress','Records','Records','Cont','Stats','End'])
                events=list(payload(result,path));self.assertEqual([e['Records']['Payload'] for e in events if 'Records' in e],[b'a,\n',b'\xe4',b'\xb8\xad'])
                self.assertEqual(events[1]['Progress']['Details']['BytesScanned'],12);self.assertEqual(events[5]['Stats']['Details']['BytesReturned'],6)
                method,url,headers,body=server.requests[0];self.assertEqual(method,'POST');self.assertIn('select',url);self.assertIn('select-type=2',url)
                xml=ET.fromstring(body);values={node.tag.split('}')[-1]:node.text for node in xml.iter()};self.assertEqual(values['Expression'],request()['Expression']);self.assertEqual(values['ExpressionType'],'SQL');self.assertEqual(values['CompressionType'],'NONE');self.assertEqual(values['Enabled'],'true')
                self.assertEqual(values['QuoteEscapeCharacter'],'\\');self.assertEqual(values['FieldDelimiter'],',');self.assertFalse(server.fixtures)

    def test_actual_typed_input_output_serialization_preserves_present_fields(self):
        """Pass complete independent serialization vectors into official SDK models; expect actual XML to retain compression, quote/escape/output delimiters, JSON type and scan-range fields exactly."""
        csv=request();csv.update({'ScanRange':{'Start':3,'End':91},'InputSerialization':{'CSV':{'FileHeaderInfo':'USE','Comments':'#','QuoteEscapeCharacter':'!','RecordDelimiter':'|','FieldDelimiter':'$','QuoteCharacter':"'",'AllowQuotedRecordDelimiter':True},'CompressionType':'GZIP'},'OutputSerialization':{'CSV':{'QuoteFields':'ALWAYS','QuoteEscapeCharacter':'~','RecordDelimiter':'@','FieldDelimiter':';','QuoteCharacter':'"'}}})
        json_request=request();json_request.update({'InputSerialization':{'JSON':{'Type':'DOCUMENT'},'CompressionType':'BZIP2'},'OutputSerialization':{'JSON':{'RecordDelimiter':'#'}}});json_request.pop('RequestProgress')
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[stream(frame('End')),stream(frame('End'))],'s3') as (worker,server):
                for index,values in enumerate((csv,json_request)):
                    result=worker.call({'op':'select','input':values,'output_path':str(Path(temporary)/str(index))});self.assertTrue(result['ok'],result)
                    xml=ET.fromstring(server.requests[-1][3])
                    def plain(node):
                        children=list(node)
                        return {child.tag.split('}')[-1]:plain(child) for child in children} if children else node.text
                    expected={key:value for key,value in values.items() if key not in ('Bucket','Key')}
                    def strings(value):return {key:strings(item) for key,item in value.items()} if isinstance(value,dict) else str(value).lower() if isinstance(value,bool) else str(value)
                    self.assertEqual(plain(xml),strings(expected))
                self.assertFalse(server.fixtures)

    def test_initial_http_rejection_is_not_a_payload_event(self):
        """Return a genuine HTTP403 XML service error; expect the official SDK top-level AccessDenied and no fabricated stream events."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[(403,{'Content-Type':'application/xml'},b'<Error><Code>AccessDenied</Code><Message>denied-original</Message></Error>')],'s3') as (worker,server):
                result=worker.call({'op':'select','input':request(),'output_path':str(Path(temporary)/'out')});self.assertFalse(result['ok']);self.assertEqual(result['status_code'],403);self.assertEqual(result['error_code'],'AccessDenied');self.assertNotIn('events',result)

    def test_actual_event_service_error_follows_partial_records(self):
        """Return HTTP200 then actual error frame; expect native decoded error after partial Records at Payload iteration; preserve pinned Java missing-code behavior and C++ real header code."""
        wire=stream(frame('Records',b'before'),frame('',message='error',extra={':error-code':'ProcessingTimeError',':error-message':'independent bad SQL'}))
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[wire],'s3') as (worker,server):
                path=Path(temporary)/'out';result=worker.call({'op':'select','input':request(),'output_path':str(path)});self.assertTrue(result['ok'],result)
                events=payload(result,path);self.assertEqual(next(events),{'Records':{'Payload':b'before'}})
                with self.assertRaises(EventStreamError) as error:next(events)
                # Pinned Java 2.20.162 loses headers-only stream error code; retain the native S3Exception instead of inventing a parsed code.
                self.assertEqual(error.exception.response['Error']['Code'],None if language=='java' else 'ProcessingTimeError')
                if language=='cpp':self.assertIn('ProcessingTimeError',str(error.exception))
                self.assertFalse(server.fixtures)

    def test_crc_corruption_is_an_official_decoder_failure_not_success_records(self):
        """Corrupt a valid Records message CRC independently; expect genuine decoder failure and no fabricated End; retain C++ Records callback emitted before its CRC check and Java rejection before Records."""
        bad=bytearray(frame('Records',b'corrupt'));bad[-1]^=1
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[stream(bytes(bad))],'s3') as (worker,server):
                path=Path(temporary)/'out';result=worker.call({'op':'select','input':request(),'output_path':str(path)})
                if result['ok']:
                    self.assertTrue(any(event['type']=='Error' for event in result['events']),result);self.assertFalse(any(event['type']=='End' for event in result['events']),result)
                    self.assertEqual([event['type'] for event in result['events']],['Records','Error'] if language=='cpp' else ['Error'])
                    self.assertEqual(path.read_bytes(),b'corrupt' if language=='cpp' else b'')
                    with self.assertRaises(RuntimeError) as error:list(payload(result,path))
                    self.assertNotIsInstance(error.exception,EventStreamError)
                else:self.assertTrue(result.get('error_message') or result.get('error'),result)

    def test_missing_end_never_synthesizes_a_success_end(self):
        """Truncate at a complete Records frame without End; expect only genuinely decoded callbacks and preserve whichever native EOF failure the SDK actually reports."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[stream(frame('Records',b'partial'))],'s3',{'OSV_REQUEST_TIMEOUT_MS':'2000'}) as (worker,server):
                path=Path(temporary)/'out';result=worker.call({'op':'select','input':request(),'output_path':str(path)})
                self.assertTrue(result['ok'],result);self.assertFalse(any(event['type']=='End' for event in result['events']),result);self.assertEqual(path.read_bytes(),b'partial')

    def test_whole_original_csv_count_reads_its_actual_put_and_selects(self):
        """Execute the unchanged 1234x10 source generator/upload/readback/count body; expect exact source SQL, real PUT bytes entering GET and owned cleanup, without claiming its defective helper checks count equality."""
        plan=[NO,OK,OK,(200,{},ECHO),stream(frame('Records',b'1234,\n'),frame('End')),OK,DEL,EMPTY,DEL,NO]
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                server.fixtures=ProducerReplay(server.fixtures,server);execute(context(worker,Path(temporary)),'test_count_operation');self.assertFalse(server.fixtures)
                upload=next(body for method,url,headers,body in server.requests if method=='PUT' and '/' in url[1:]);self.assertEqual(len(upload.splitlines()),1234)
                query=next(body for method,url,headers,body in server.requests if method=='POST');self.assertIn(b'select count(0) from s3object;',query)

    def test_whole_original_json_count_retains_three_queries_and_repeated_bucket_create(self):
        """Run unchanged source JSON count with both 1-row/3-row uploads and all three JSON-path queries; expect real source re-create request and native DOCUMENT serialization plus source cleanup."""
        one=stream(frame('Records',b'1\n'),frame('End'));three=stream(frame('Records',b'3\n'),frame('End'))
        plan=[NO,OK,OK,(200,{},ECHO),one,one,OK,OK,(200,{},ECHO),three,OK,DEL,EMPTY,DEL,NO]
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                server.fixtures=ProducerReplay(server.fixtures,server);execute(context(worker,Path(temporary)),'test_count_json_operation');self.assertFalse(server.fixtures)
                queries=[ET.fromstring(body) for method,url,headers,body in server.requests if method=='POST']
                self.assertEqual(len(queries),3)
                expressions=[]
                for xml in queries:
                    values={node.tag.split('}')[-1]:node.text for node in xml.iter()};self.assertEqual(values['Type'],'DOCUMENT');expressions.append(values['Expression']);self.assertNotIn('CompressionType',values)
                self.assertEqual(expressions,['select count(0) from s3object[*];','select count(0) from s3object[*].root;','select count(0) from s3object[*].root;'])
                creates=[url for method,url,headers,body in server.requests if method=='PUT' and url.split('?',1)[0].strip('/').count('/')==0];self.assertEqual(len(creates),2);self.assertEqual(creates[0],creates[1])

    def test_original_run_helper_uses_max_progress_and_raises_on_split_utf8(self):
        """Feed real decoded events into the original helper; expect greatest BytesScanned Progress retained, exact Records bytes and original per-event UTF-8 failure on split multibyte characters."""
        vectors=[(stream(details('Progress',20,4),frame('Records',b'one'),details('Progress',10,1),details('Progress',20,99),details('Stats',25,3),frame('End')),True),(stream(frame('Records',b'\xe4'),frame('Records',b'\xb8\xad'),frame('End')),False)]
        for language in self.languages():
            for wire,progress in vectors:
                with self.subTest(language=language,progress=progress),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[NO,OK,wire,OK,EMPTY,DEL,NO],'s3') as (worker,server):
                    with Runtime(context(worker,Path(temporary))) as runtime:
                        name=runtime.name();Client(runtime).create_bucket(Bucket=name)
                        if progress:
                            records,status=source.run_s3select(name,'input.csv','select _1 from s3object;',progress=True)
                            self.assertEqual(records,[{'Payload':b'one'}]);self.assertEqual(status,{'Progress':{'Details':{'BytesScanned':20,'BytesProcessed':20,'BytesReturned':4}},'Stats':{'Details':{'BytesScanned':25,'BytesProcessed':25,'BytesReturned':3}},'End':{}})
                        else:
                            with self.assertRaises(UnicodeDecodeError):source.run_s3select(name,'input.csv','select _1 from s3object;')
                    self.assertFalse(server.fixtures)

    def test_both_original_aggregate_sources_keep_literal_testbuck2_and_all_nine_queries(self):
        """Run both unchanged 10000x10 aggregate sources through actual SDKs; expect exact testbuck2 bucket/queries, actual upload→readback, both owned cleanups and no UUID substitution for the literal."""
        for language in self.languages():
            for name in ('test_column_sum_min_max','test_json_column_sum_min_max'):
                responses=[stream(frame('Records',b'1,\n'),frame('End'))]*7+[stream(frame('Records',b'0,0,0\n'),frame('End'))]*2
                plan=[NO,NO,OK,OK,(200,{},ECHO),NO,OK,OK,(200,{},ECHO)]+responses+[OK,DEL,EMPTY,DEL,NO]*2
                with self.subTest(language=language,source=name),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                    server.fixtures=ProducerReplay(server.fixtures,server);p=context(worker,Path(temporary));p.ctx.workload['ceph_literal_bucket_names_authorized']=True
                    execute(p,name);self.assertFalse(server.fixtures)
                    self.assertEqual(server.requests[0][:2],('HEAD','/testbuck2'))
                    writes=[(url,body) for method,url,headers,body in server.requests if method=='PUT' and url.split('?',1)[0].strip('/').count('/')==1]
                    self.assertEqual(len(writes),2);self.assertTrue(writes[1][0].startswith('/testbuck2/'));self.assertEqual(writes[0][1],writes[1][1])
                    if name=='test_column_sum_min_max':self.assertEqual(len(writes[0][1].splitlines()),10000)
                    else:self.assertEqual(len(json.loads(writes[0][1])['root']),10000)
                    selects=[(url,ET.fromstring(body)) for method,url,headers,body in server.requests if method=='POST']
                    self.assertEqual(len(selects),9);self.assertTrue(selects[7][0].startswith('/testbuck2/'));self.assertFalse(selects[8][0].startswith('/testbuck2/'))
                    expressions=[next(node.text for node in xml.iter() if node.tag.split('}')[-1]=='Expression') for url,xml in selects]
                    if name=='test_column_sum_min_max':expected=['select min(int(_1)) from s3object;','select min(int(_4)) from s3object;','select avg(int(_6)) from s3object;','select max(int(_4)) from s3object;','select max(int(_7)) from s3object;','select sum(int(_4)) from s3object;','select sum(int(_7)) from s3object;','select count(0),sum(int(_1)),sum(int(_2)) from s3object where (int(_1)-int(_2)) = 2;','select count(0),sum(int(_1)),sum(int(_2)) from s3object where (int(_1)-int(_2)) = 4;']
                    else:expected=['select min(_1.c1) from s3object[*].root;','select min(_1.c4) from s3object[*].root;','select avg(_1.c6) from s3object[*].root;','select max(_1.c4) from s3object[*].root;','select max(_1.c7) from s3object[*].root;','select sum(_1.c4) from s3object[*].root;','select sum(_1.c7) from s3object[*].root;','select count(0),sum(_1.c1),sum(_1.c2) from s3object[*].root where (_1.c1-_1.c2) = 2;','select count(0),sum(_1.c1),sum(_1.c2) from s3object[*].root where (_1.c1-_1.c2) = 4;']
                    self.assertEqual(expressions,expected)
                    deletes=[url for method,url,headers,body in server.requests if method=='DELETE'];self.assertIn('/testbuck2',deletes);self.assertTrue(any(url.startswith('/testbuck2/') for url in deletes))

    def test_literal_sources_require_authorization_before_any_http_and_never_claim_existing_bucket(self):
        """Omit the literal isolation authorization or return an existing testbuck2; expect not_run before first source bucket mutation and no PUT/DELETE against either namespace."""
        for language in self.languages():
            for name in ('test_column_sum_min_max','test_json_column_sum_min_max'):
                for authorized in (False,True):
                    with self.subTest(language=language,source=name,authorized=authorized),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[OK] if authorized else [],'s3') as (worker,server):
                        p=context(worker,Path(temporary));p.ctx.workload['ceph_literal_bucket_names_authorized']=authorized
                        with self.assertRaises(CaseNotRun) as error:execute(p,name)
                        self.assertEqual(error.exception.requirements,['HEAD 404 for testbuck2'] if authorized else ['workload.ceph_literal_bucket_names_authorized=true'])
                        self.assertEqual([(method,url) for method,url,headers,body in server.requests],[('HEAD','/testbuck2')] if authorized else [])
                        self.assertFalse(server.fixtures)

    def test_literal_recheck_and_rejected_create_clean_only_the_successfully_owned_first_bucket(self):
        """Let testbuck2 appear after preflight or reject its CreateBucket; expect the real source refusal and cleanup of only its first created bucket, never DELETE of unowned testbuck2."""
        denied=(403,{'Content-Type':'application/xml'},b'<Error><Code>AccessDenied</Code><Message>independent literal create denial</Message></Error>')
        for language in self.languages():
            for appeared in (True,False):
                plan=[NO,NO,OK,OK,(200,{},ECHO)]+([OK] if appeared else [NO,denied])+[OK,DEL,EMPTY,DEL,NO]
                with self.subTest(language=language,appeared=appeared),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3') as (worker,server):
                    server.fixtures=ProducerReplay(server.fixtures,server);p=context(worker,Path(temporary));p.ctx.workload['ceph_literal_bucket_names_authorized']=True
                    with self.assertRaises(CaseNotRun if appeared else ClientError):execute(p,'test_column_sum_min_max')
                    self.assertFalse(server.fixtures);self.assertFalse(any(method=='DELETE' and url.startswith('/testbuck2') for method,url,headers,body in server.requests))
                    self.assertEqual(sum(method=='DELETE' for method,url,headers,body in server.requests),2)

    def test_submitted_create_with_dropped_response_recovers_only_with_two_real_owner_proofs(self):
        """Drop a genuine CreateBucket reply after submission; expect original SDK failure retained, cleanup only when independent configured/control/candidate owners match, and explicit unknown bucket evidence without candidate DELETE otherwise."""
        def acl(owner):return 200,{'Content-Type':'application/xml'},('<AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Owner><ID>'+owner+'</ID></Owner><AccessControlList/></AccessControlPolicy>').encode()
        for language in self.languages():
            for mode in ('match','mismatch','missing','wrong_config'):
                plan=[NO,(0,{},b''),OK]
                if mode!='missing':
                    plan += [OK,NO,OK,acl('actual-independent-owner'),acl('foreign-owner' if mode=='mismatch' else 'actual-independent-owner')]
                    if mode=='match':plan += [OK,EMPTY,DEL,NO]
                    plan += [OK,EMPTY,DEL,NO]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,plan,'s3',handler=DroppedCreateHandler) as (worker,server):
                    p=context(worker,Path(temporary))
                    if mode!='missing':p.ctx.workload['identities']={'main':{'canonical_id':'wrong-configured-owner' if mode=='wrong_config' else 'actual-independent-owner'}}
                    with self.assertRaises(RuntimeError) as error:execute(p,'test_count_operation')
                    self.assertNotIsInstance(error.exception,CaseNotRun);self.assertFalse(server.fixtures)
                    candidate=server.requests[0][1];self.assertEqual(server.requests[1][:2],('PUT',candidate))
                    candidate_deletes=[row for row in server.requests if row[0]=='DELETE' and row[1]==candidate]
                    self.assertEqual(len(candidate_deletes),1 if mode=='match' else 0)
                    if mode=='match':
                        self.assertIn('Actual Select SDK transport failure',str(error.exception));self.assertNotIn('cleanup_pending',str(error.exception))
                        acl_requests=[row[1] for row in server.requests if row[0]=='GET' and 'acl' in row[1]];self.assertEqual(len(acl_requests),2);self.assertNotEqual(acl_requests[0],acl_requests[1])
                    else:
                        self.assertIn(candidate.lstrip('/'),str(error.exception));self.assertIn('unknown bucket',str(error.exception));self.assertIn('cleanup_pending',str(error.exception))

    def test_original_helpers_keep_non_equality_oracle_and_todo_not_run(self):
        """Prove original unequal nonempty values still pass helper, empty nonempty fails, and immediate-return TODO cannot be counted as a service pass."""
        source.s3select_assert_result('wrong','right')
        with self.assertRaises(AssertionError):source.s3select_assert_result('','right')
        for language in self.languages():
            with fixture_worker(language,[],'s3') as (worker,server),tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(CaseNotRun) as error:execute(context(worker,Path(temporary)),'test_output_serial_expressions')
                self.assertIn('upstream_todo',str(error.exception.requirements));self.assertFalse(server.requests)
                rows=run_cases(context(worker,Path(temporary)).ctx,[case for case in CASES if case.name=='ceph_select_output_serial_expressions'])
                self.assertEqual(rows[0]['status'],'not_run');self.assertEqual(rows[0]['missing_requirements'],['upstream_todo']);self.assertFalse(server.requests)


class SelectSourceFidelityTest(unittest.TestCase):
    """Validate static migrated functions against pinned source fingerprints without invoking any upstream runner."""
    def test_all_34_active_original_function_bodies_match_inventory_and_static_copy(self):
        """Compare exact source segments and SHA for all active bodies; expect duplicate shadows excluded and no edited source assertion or source input."""
        import hashlib
        root=Path(__file__).resolve().parents[2]
        inventory=json.loads((root/'osv/data/upstream.json').read_text())['ceph']['cases']
        original={row['name']:row for row in inventory if 'test_s3select.py::' in row['id'] and row['active_definition']}
        text=Path(source.__file__).read_text();actual={node.name:node for node in ast.parse(text).body if isinstance(node,ast.FunctionDef)}
        self.assertEqual(len(SOURCES),34);self.assertEqual(set(original),{name for name,line,sha in SOURCES})
        for name,line,sha in SOURCES:
            self.assertEqual(original[name]['line'],line);self.assertEqual(original[name]['sha256'],sha)
            self.assertEqual(hashlib.sha256(ast.get_source_segment(text,actual[name]).encode()).hexdigest(),sha)
        self.assertTrue(all('没有结果相等oracle' in case.expected for case in CASES))

    def test_native_payload_consumer_rejects_bad_offsets_length_and_trailing_bytes(self):
        """Give the actual consumer independent malformed metadata or bytes; expect strict contiguous boundaries/truncation/remaining-data errors instead of accepting manufactured payloads."""
        for event,data,size in [({'type':'Records','offset':1,'length':1},b'x',1),({'type':'Records','offset':0,'length':2},b'x',2),({'type':'Records','offset':0,'length':1},b'xy',1),({'type':'Records','offset':0,'length':1},b'x',2)]:
            with self.subTest(event=event,data=data,size=size),tempfile.TemporaryDirectory() as temporary:
                path=Path(temporary)/'records';path.write_bytes(data)
                with self.assertRaises(RuntimeError):list(payload({'events':[event],'records_size':size},path))


    def test_failed_create_result_classification_keeps_http200_parse_failures_ambiguous(self):
        """Inject independent failed RPC result records into the actual Client.create_bucket consumer; expect every non-4xx failure including HTTP200 parse errors retained as ambiguous, while explicit 4xx stay original failures without recovery claims. This boundary test does not simulate SDK or service behavior."""
        from collections import deque
        class Inputs:
            def __init__(self,result):self.inputs=deque([{'ok':False,'status_code':404,'error_kind':'not_found'},result]);self.calls=[]
            def call(self,request):self.calls.append(request);return self.inputs.popleft()
        for status,ambiguous in ((None,True),(0,True),(-1,True),(200,True),(302,True),(500,True),(400,False),(403,False),(409,False),(499,False)):
            with self.subTest(status=status),tempfile.TemporaryDirectory() as temporary:
                worker=Inputs({'ok':False,'status_code':status,'error_code':'IndependentNativeFailure','error_message':'independent failed RPC result','error_kind':'sdk_error'})
                runtime=Runtime(context(worker,Path(temporary)));name=runtime.name()
                with self.assertRaises(RuntimeError) as error:Client(runtime).create_bucket(Bucket=name)
                self.assertNotIsInstance(error.exception,CaseNotRun)
                self.assertEqual(runtime.ambiguous,{name} if ambiguous else set());self.assertFalse(runtime.buckets[name].created)
                self.assertEqual([request['op'] for request in worker.calls],['bucket_head','bucket_create_raw'])
