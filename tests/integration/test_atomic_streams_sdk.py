"""Prove real SDK atomic sink callbacks using a held-open HTTP response, not request batches."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit,parse_qs
from sdk_fixtures import FixtureHandler,fixture_worker
from test_lifecycle_expiration_sdk import scope_begin,scope_end
from osv.ceph_atomic_streams import CASES
from osv.scenarios import Context
from osv.suites import run_cases

class PausedBodyWriter:
    """Hold the literal original A response after its first chunk until actual nested SDK B PUT finishes; no object-state emulation."""
    def __init__(self,underlying,server):self.underlying=underlying;self.server=server
    def write(self,value):
        if len(value)>=1024 and value.startswith(b"A"*1024):
            self.underlying.write(value[:1024]);self.underlying.flush();self.server.events.append("first_A_chunk_sent")
            self.server.first_chunk.set()
            if not self.server.nested_done.wait(10):raise RuntimeError("nested SDK B PUT did not finish while A response was suspended")
            self.server.events.append("remaining_A_released");return self.underlying.write(value[1024:])+1024
        return self.underlying.write(value)
    def __getattr__(self,name):return getattr(self.underlying,name)

class AtomicReadHandler(FixtureHandler):
    """Replay independent wire documents and record barrier/client connections, allowing only genuine nested SDK transfer to release A's remaining bytes."""
    def respond(self):
        self.server.connections.append((self.command,self.path,self.client_address[1]))
        if self.command=="GET" and self.server.fixtures and self.server.fixtures[0][2].startswith(b"A"*1024):
            original=self.wfile;self.wfile=PausedBodyWriter(original,self.server)
            try:return super().respond()
            finally:self.wfile=original
        result=super().respond()
        if self.command=="PUT" and self.server.first_chunk.is_set():
            self.server.events.append("nested_B_put_finished");self.server.nested_done.set()
        return result
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

class AtomicUploadHandler(FixtureHandler):
    """Supply literal A-before-completion/B-after-completion documents based only on actual transport completion; record actual nested callback traffic without implementing object persistence or signing."""
    def respond(self):
        if not urlsplit(self.path).path.endswith("/testobj") or self.command not in ("GET","PUT"):return super().respond()
        if "chunked" in self.headers.get("Transfer-Encoding","").lower():body=self.chunks(self.rfile)
        else:body=self.rfile.read(int(self.headers.get("Content-Length","0")))
        decoded=self.headers.get("x-amz-decoded-content-length")
        if ("aws-chunked" in self.headers.get("Content-Encoding","") or self.headers.get("x-amz-content-sha256","").startswith("STREAMING-")) and (decoded is None or len(body)!=int(decoded)):
            import io
            try:body=self.chunks(io.BytesIO(body))
            except ValueError:
                # The deliberate EOF-verifier failure can abort the real SDK
                # before it sends the final framed chunk. Keep the observed
                # partial bytes and abort event; never treat them as a full PUT.
                if not getattr(self.server,"bad_callback",None):raise
                self.server.requests.append((self.command,self.path,dict(self.headers),body))
                self.server.events.append("outer_upload_aborted_after_callback_error")
                self.close_connection=True
                return
        self.server.requests.append((self.command,self.path,dict(self.headers),body))
        status=200;headers={};payload=b""
        if self.command=="GET":
            final=self.server.outer_finished
            self.server.events.append("final_B_get" if final else "A_get_before_outer_finished")
            payload=(b"B" if final else b"A")*self.server.expected_size
            if not final:
                self.server.a_gets=getattr(self.server,"a_gets",0)+1
                defect=getattr(self.server,"bad_callback",None)
                if self.server.a_gets>=2 and defect:
                    if defect=="byte":payload=payload[:-1]+b"X"
                    elif defect=="short":payload=payload[:-1]
                    else:status=403;headers={"Content-Type":"application/xml"};payload=b"<Error><Code>AccessDenied</Code><Message>independent EOF verifier denied</Message></Error>"
        elif self.command=="PUT":
            if body.startswith(b"B") and len(body)==self.server.expected_size:
                self.server.events.append("outer_B_body_received");self.server.outer_finished=True
            elif body.startswith(b"A"):
                self.server.events.append("A_put_before_outer_finished" if not self.server.outer_finished else "LATE_A_put")
            else:self.server.events.append("empty_put")
        elif self.command=="DELETE":status=204
        self.send_response(status)
        for name,value in headers.items():self.send_header(name,value)
        self.send_header("Content-Length",str(len(payload)));self.end_headers()
        if self.command!="HEAD":self.wfile.write(payload)
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond


def request_body(handler):
    """Decode only actual HTTP/AWS chunk framing, sharing the fixture parser; never reconstruct SDK bodies or signatures."""
    if "chunked" in handler.headers.get("Transfer-Encoding","").lower():body=handler.chunks(handler.rfile)
    else:body=handler.rfile.read(int(handler.headers.get("Content-Length","0")))
    decoded=handler.headers.get("x-amz-decoded-content-length")
    if ("aws-chunked" in handler.headers.get("Content-Encoding","") or handler.headers.get("x-amz-content-sha256","").startswith("STREAMING-")) and (decoded is None or len(body)!=int(decoded)):
        import io
        body=handler.chunks(io.BytesIO(body))
    return body

def reply(handler,status,headers=None,payload=b""):
    handler.send_response(status)
    for name,value in (headers or {}).items():handler.send_header(name,value)
    handler.send_header("Content-Length",str(len(payload)));handler.end_headers()
    if handler.command!="HEAD":handler.wfile.write(payload)

class AtomicMultipartHandler(FixtureHandler):
    """Return literal opaque UploadIds/ETags and independent final A, recording actual calibration traffic and duplicate completion XML rather than emulating multipart storage."""
    def respond(self):
        query=parse_qs(urlsplit(self.path).query,keep_blank_values=True);path=urlsplit(self.path).path
        special=path.endswith("/mymultipart") or "uploads" in query
        if not special:return super().respond()
        if self.command=="DELETE" and "uploadId" not in query and getattr(self.server,"cleanup",False):return super().respond()
        try:body=request_body(self)
        except ValueError:
            self.close_connection=True;self.server.events.append("actual_upload_aborted");return
        self.server.requests.append((self.command,self.path,dict(self.headers),body))
        fault=getattr(self.server,"fault",None)
        if self.command=="POST" and "uploads" in query:
            self.server.creates+=1
            if fault=="unknown_create" and self.server.creates==1:return reply(self,500,{"Content-Type":"application/xml"},b"<Error><Code>InternalError</Code><Message>unknown actual Create outcome</Message></Error>")
            upload="dry-real-%+/?=" if self.server.creates==1 else "new-real-%+/?="
            return reply(self,200,{"Content-Type":"application/xml"},("<InitiateMultipartUploadResult><UploadId>"+upload+"</UploadId></InitiateMultipartUploadResult>").encode())
        if self.command=="PUT":
            if body==b"C"*8:etag='"dry-real-etag"'
            elif body==b"B"*8:etag='"""nested-real-etag"""'
            elif body==b"A"*8:etag='""outer-real-etag""'
            else:return reply(self,400,{"Content-Type":"application/xml"},b"<Error><Code>InvalidRequest</Code><Message>independent expected eight original bytes</Message></Error>")
            if fault=="part_missing_etag" and body==b"C"*8:return reply(self,200)
            return reply(self,200,{"ETag":etag})
        if self.command=="POST":return reply(self,200,{"Content-Type":"application/xml"},b"<CompleteMultipartUploadResult><ETag>unused-completion-etag</ETag></CompleteMultipartUploadResult>")
        if self.command=="GET" and "uploads" in query:
            self.server.cleanup=True
            self.server.mpu_lists+=1
            orphan=("<Upload><Key>mymultipart</Key><UploadId>orphan-real-%+/?=</UploadId></Upload>" if fault=="unknown_create" and self.server.mpu_lists==1 else "")
            return reply(self,200,{"Content-Type":"application/xml"},("<ListMultipartUploadsResult><IsTruncated>false</IsTruncated>"+orphan+"</ListMultipartUploadsResult>").encode())
        if self.command=="GET":return reply(self,200,{},b"A"*8)
        return reply(self,204)
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

class AtomicConditionalHandler(FixtureHandler):
    """Replay independent initial ETag, nested B success and outer C412; preserve exact wire If-Match inputs and genuine negative status rather than synthesizing worker responses."""
    def respond(self):
        if not urlsplit(self.path).path.endswith("/testobj") or self.command not in ("GET","PUT"):return super().respond()
        try:body=request_body(self)
        except ValueError:self.close_connection=True;return
        self.server.requests.append((self.command,self.path,dict(self.headers),body))
        if self.command=="GET":
            self.server.gets+=1;return reply(self,200,{},(b"A" if self.server.gets==1 else b"B")*(1024*1024))
        if body.startswith(b"A"):return reply(self,200,{"ETag":getattr(self.server,"initial_etag",'"e6065c4aa2ab1603008fc18410f579d4"')})
        if body.startswith(b"B"):return reply(self,200,{"ETag":'"3310df4c5ca4509740f3ada8d0c946c2"'})
        return reply(self,412,{"Content-Type":"application/xml"},b"<Error><Code>PreconditionFailed</Code><Message>independent actual outer C condition rejected</Message></Error>")
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

class AtomicGoneHandler(FixtureHandler):
    """Send actual first successful DeleteBucket and subsequent real NoSuchBucket responses; keep repeated EOF callback errors distinguishable from outer PUT failures."""
    def respond(self):
        path=urlsplit(self.path).path
        if self.command=="DELETE" and path.count("/")==1:
            self.server.delete_calls+=1;self.server.requests.append((self.command,self.path,dict(self.headers),b""))
            if self.server.delete_calls==1:return reply(self,204)
            return reply(self,404,{"Content-Type":"application/xml"},b"<Error><Code>NoSuchBucket</Code><Message>actual repeated nested delete</Message></Error>")
        if self.command=="PUT" and path.endswith("/foo"):
            try:body=request_body(self)
            except ValueError:self.close_connection=True;self.server.aborts+=1;return
            self.server.requests.append((self.command,self.path,dict(self.headers),body))
            code=getattr(self.server,"put_error_code","NoSuchBucket")
            return reply(self,404,{"Content-Type":"application/xml"},("<Error><Code>"+code+"</Code><Message>actual outer upload bucket gone</Message></Error>").encode())
        return super().respond()
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond


class AtomicVersionsHandler(FixtureHandler):
    """Hold real PUT replies until the coordinator lists, replay literal opaque version rows, and record joins/actual version-delete consumers without storing objects."""
    def respond(self):
        query=parse_qs(urlsplit(self.path).query,keep_blank_values=True);path=urlsplit(self.path).path
        helper_list=self.command=="GET" and "versions" in query and query.get("max-keys")!=["1000"]
        put=self.command=="PUT" and path.endswith("/myobj")
        delete=self.command=="DELETE" and path.endswith("/myobj") and not self.server.source_finished
        if not (helper_list or put or delete):
            if "versions" in query:self.server.events.append("owned_cleanup_list")
            return super().respond()
        body=request_body(self);self.server.requests.append((self.command,self.path,dict(self.headers),body))
        if put:
            with self.server.lock:index=self.server.puts;self.server.puts+=1
            self.server.events.append("put_started")
            if not self.server.list_seen.wait(5):
                self.server.barrier_timeouts+=1
                return reply(self,500,{"Content-Type":"application/xml"},b"<Error><Code>InternalError</Code><Message>coordinator incorrectly joined before listing</Message></Error>")
            self.server.events.append("put_reply")
            if getattr(self.server,"background_error",False) and index==0:return reply(self,403,{"Content-Type":"application/xml"},b"<Error><Code>AccessDenied</Code><Message>independent background PUT denied</Message></Error>")
            return reply(self,200,{"x-amz-version-id":"put-id-"+str(index)})
        if delete:return reply(self,204)
        index=self.server.lists;self.server.lists+=1;self.server.events.append("coordinator_list");self.server.list_seen.set()
        fault=getattr(self.server,"version_fault",None)
        if index==0 and fault=="list_denied":
            self.server.source_finished=True
            return reply(self,403,{"Content-Type":"application/xml"},b"<Error><Code>AccessDenied</Code><Message>actual coordinator list denied</Message></Error>")
        if index==0 and fault=="missing_id":
            self.server.source_finished=True;rows="<Version><Key>myobj</Key></Version>"
        elif index<4:
            ident="listed-real-"+str(index)+"%+/?="
            rows="<Version><Key>myobj</Key><VersionId>"+ident+"</VersionId></Version>"
        else:
            self.server.source_finished=True
            rows="<Version><Key>myobj</Key><VersionId>unexpected-still-present</VersionId></Version>" if fault=="remaining_version" else ""
        return reply(self,200,{"Content-Type":"application/xml"},("<ListVersionsResult><IsTruncated>false</IsTruncated>"+rows+"</ListVersionsResult>").encode())
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond

def version_setup(server):
    server.source_finished=False;server.lock=threading.Lock();server.puts=server.lists=server.barrier_timeouts=0;server.events=[];server.list_seen=threading.Event()


def documents(size,body_a=None,body_b=None):
    return scope_begin()+[(200,{},b""),(200,{},b"A"*size if body_a is None else body_a),(200,{},b""),(200,{},b"B"*size if body_b is None else body_b)]+scope_end(("testobj",))

def setup(server):
    server.first_chunk=threading.Event();server.nested_done=threading.Event();server.events=[];server.connections=[]

def context(worker,temporary):
    path=Path(temporary);return Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})

def languages():return os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(",")

@unittest.skipUnless(os.environ.get("OSV_RUN_ATOMIC_STREAM_TESTS")=="1","enable actual SDK atomic streams")
class AtomicStreamSdkTest(unittest.TestCase):
    """Exercise original stream callbacks and independent full-byte/size oracles through both actual SDKs."""
    def test_all_original_read_sizes_nested_put_finishes_before_a_response_resumes(self):
        """Download original 1/4/8MiB A through a real SDK sink whose first positive write performs original-client B PUT; expect B completion before HTTP releases remaining A, each A chunk intact, fresh final B verification and owner cleanup."""
        for language in languages():
            for case,size in zip(CASES,(1024*1024,4*1024*1024,8*1024*1024)):
                docs=documents(size)
                with self.subTest(language=language,size=size),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,docs,"s3",handler=AtomicReadHandler) as (worker,server):
                    setup(server);result=run_cases(context(worker,temporary),[case])[0]
                    self.assertEqual(result["status"],"pass",result)
                    self.assertEqual(server.events,["first_A_chunk_sent","nested_B_put_finished","remaining_A_released"])
                    self.assertEqual(len(server.requests),len(docs));self.assertEqual(len(server.fixtures),0)
                    puts=[row for row in server.requests if row[0]=="PUT" and urlsplit(row[1]).path.endswith("/testobj")]
                    self.assertEqual(len(puts),2)
                    for request,byte in zip(puts,(b"A",b"B")):
                        self.assertEqual(len(request[3]),size);self.assertEqual(hashlib.sha256(request[3]).digest(),hashlib.sha256(byte*size).digest())
                    gets=[row for row in server.connections if row[0]=="GET" and urlsplit(row[1]).path.endswith("/testobj")]
                    put_ports={row[2] for row in server.connections if row[0]=="PUT" and urlsplit(row[1]).path.endswith("/testobj")}
                    self.assertEqual(len(gets),2);self.assertNotEqual(gets[0][2],gets[1][2])
                    self.assertNotIn(gets[0][2],put_ports);self.assertNotIn(gets[1][2],put_ports)

    def test_download_byte_and_size_failures_remain_failures_and_cleanup_runs(self):
        """Send a wrong trailing A byte, short A/B bodies or a genuine nested PUT403 through actual SDK sinks; expect byte/size failures or the original nested error message, with owner cleanup in every case."""
        for language in languages():
            for defect in ("A_byte","A_short","B_short","nested_PUT_error"):
                size=1024*1024
                # Wrong A byte is after the first held chunk so the original nested
                # B PUT still happens before the real sink encounters corruption.
                a=b"A"*(size-1)+b"X" if defect=="A_byte" else b"A"*(size-1) if defect=="A_short" else b"A"*size
                docs=scope_begin()+[(200,{},b""),(200,{},a),((403,{"Content-Type":"application/xml"},b"<Error><Code>AccessDenied</Code><Message>independent nested PUT rejected</Message></Error>") if defect=="nested_PUT_error" else (200,{},b""))]+([(200,{},b"B"*(size-1))] if defect=="B_short" else [])+scope_end(("testobj",))
                with self.subTest(language=language,defect=defect),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,docs,"s3",handler=AtomicReadHandler) as (worker,server):
                    setup(server);result=run_cases(context(worker,temporary),[CASES[0]])[0]
                    self.assertEqual(result["status"],"fail",result)
                    if defect=="nested_PUT_error":self.assertIn("independent nested PUT rejected",str(result))
                    deletes=[row for row in server.requests if row[0]=="DELETE"]
                    self.assertEqual(len(deletes),2);self.assertEqual(len(server.fixtures),0)

    def test_invalid_size_and_mode_rejected_without_http(self):
        """Supply zero/negative/bool/float/null/overflow sizes, invalid mode types and non-main identities; expect local validation failure before any SDK object mutation."""
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3") as (worker,server):
                for change in ({"size":0},{"size":-1},{"size":True},{"size":1.5},{"size":"1"},{"size":None},{"size":2**63},{"size":2**64},{"mode":"unknown"},{"mode":None},{"mode":1},{"identity":None},{"identity":1}):
                    response=worker.call({"op":"atomic_stream","bucket":"literal-bucket","key":"testobj","mode":"read","size":1024,**change})
                    self.assertFalse(response["ok"],response);self.assertEqual(server.requests,[])

    def test_all_original_upload_eof_modes_preserve_nested_requests_and_conditions(self):
        """Run original write1/4/8MiB, dual-write1/4/8MiB and conditional1MiB through real SDK EOF reads; expect every actual callback request before outer B body completes, fresh A verification or same-client A PUT, exact If-Match:* and final all-B verification."""
        source_inputs=(("write",1024*1024),("write",4*1024*1024),("write",8*1024*1024),("dual_write",1024*1024),("dual_write",4*1024*1024),("dual_write",8*1024*1024),("conditional_write",1024*1024))
        for language in languages():
            for case,(mode,size) in zip(CASES[3:],source_inputs):
                with self.subTest(language=language,mode=mode,size=size),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("testobj",)),"s3",handler=AtomicUploadHandler) as (worker,server):
                    server.expected_size=size;server.outer_finished=False;server.events=[]
                    result=run_cases(context(worker,temporary),[case])[0]
                    self.assertEqual(result["status"],"pass",result)
                    self.assertEqual(len(server.fixtures),0)
                    requests=[row for row in server.requests if urlsplit(row[1]).path.endswith("/testobj")]
                    puts=[row for row in requests if row[0]=="PUT"]
                    self.assertEqual(puts[-1][3],b"B"*size)
                    self.assertEqual(puts[0][3],b"" if mode=="dual_write" else b"A"*size)
                    self.assertEqual([row for row in server.events if row=="LATE_A_put"],[])
                    index=server.events.index("outer_B_body_received")
                    self.assertEqual(server.events[-1],"final_B_get")
                    if mode=="dual_write":
                        nested=[row for row in puts[1:] if row[3].startswith(b"A")]
                        self.assertGreaterEqual(len(nested),1)
                        for row in nested:self.assertEqual(row[3],b"A"*size)
                        self.assertTrue(all(event=="A_put_before_outer_finished" for event in server.events[1:index]))
                    else:
                        gets=[event for event in server.events[:index] if event=="A_get_before_outer_finished"]
                        self.assertGreaterEqual(len(gets),1 if mode=="conditional_write" else 2)
                    for row in puts:
                        actual={key.lower():value for key,value in row[2].items()}
                        self.assertEqual(actual.get("if-match"),"*" if mode=="conditional_write" and row[3].startswith(b"B") else None)

    def test_upload_eof_verifier_errors_are_not_hidden_by_sdk_body_wrapping(self):
        """Corrupt or deny the real GET invoked inside an upload EOF callback; expect the original byte/size/service error and owned cleanup, with no successful final-B verification claimed."""
        for language in languages():
            for defect in ("byte","short","server403"):
                with self.subTest(language=language,defect=defect),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("testobj",)),"s3",handler=AtomicUploadHandler) as (worker,server):
                    server.expected_size=1024*1024;server.outer_finished=False;server.events=[];server.bad_callback=defect
                    result=run_cases(context(worker,temporary),[CASES[3]])[0]
                    self.assertEqual(result["status"],"fail",result)
                    self.assertIn({"byte":"atomic download byte differs","short":"atomic download size","server403":"independent EOF verifier denied"}[defect],str(result))
                    self.assertNotIn("final_B_get",server.events)
                    self.assertEqual(len([row for row in server.requests if row[0]=="DELETE"]),2)
                    self.assertEqual(len(server.fixtures),0)

    def test_one_connection_never_enters_nested_same_client_deadlock(self):
        """Configure only one actual SDK connection; expect same-client dual upload capability withheld and direct RPC rejected before HTTP, rather than blocking in the official C++ handle pool."""
        for language in languages():
            with self.subTest(language=language),fixture_worker(language,[],"s3",overrides={"OSV_CONNECTIONS":"1"}) as (worker,server):
                for mode in ("dual_write","multipart_resend","dual_conditional_write","bucket_gone"):
                    self.assertNotIn("atomic_stream_"+mode,worker.capabilities)
                    response=worker.call({"op":"atomic_stream","mode":mode,"size":8 if mode=="multipart_resend" else 1024,"bucket":"literal-bucket","key":"testobj"})
                    self.assertFalse(response["ok"],response);self.assertIn("OSV_CONNECTIONS >= 2",str(response));self.assertEqual(server.requests,[])

    def test_real_multipart_calibration_uses_new_ids_and_duplicate_original_parts(self):
        """Run original eight-byte C calibration and A/B resend through both actual SDKs; expect real dry/new UploadIds, unchanged C/A/B bodies, actual B then A ETags stripped only at boundaries in two PartNumber1 completion entries, and actual final A verification."""
        from xml.etree import ElementTree as ET
        case=next(c for c in CASES if c.name=="ceph_atomic_stream_multipart_resend")
        for language in languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("mymultipart",)),"s3",handler=AtomicMultipartHandler) as (worker,server):
                server.events=[];server.creates=server.mpu_lists=0
                result=run_cases(context(worker,temporary),[case])[0];self.assertEqual(result["status"],"pass",result)
                parts=[row for row in server.requests if row[0]=="PUT" and "partNumber=" in row[1]]
                self.assertEqual(sorted(row[3] for row in parts),sorted((b"C"*8,b"B"*8,b"A"*8)))
                for row in parts:self.assertEqual(parse_qs(urlsplit(row[1]).query)["uploadId"],["dry-real-%+/?=" if row[3]==b"C"*8 else "new-real-%+/?="])
                completed=[row for row in server.requests if row[0]=="POST" and "uploadId=" in row[1]]
                self.assertEqual(len(completed),2)
                expected=(("dry-real-%+/?=",["dry-real-etag"]),("new-real-%+/?=",["nested-real-etag","outer-real-etag"]))
                for row,(upload,tags) in zip(completed,expected):
                    self.assertEqual(parse_qs(urlsplit(row[1]).query)["uploadId"],[upload])
                    root=ET.fromstring(row[3]);actual=[{child.tag.rsplit("}",1)[-1]:child.text for child in part} for part in root]
                    self.assertEqual([item["ETag"] for item in actual],tags);self.assertEqual([item["PartNumber"] for item in actual],["1"]*len(tags))
                self.assertEqual(server.creates,2);self.assertEqual(server.mpu_lists,1);self.assertEqual(len(server.fixtures),0)

    def test_multipart_missing_etag_and_unknown_create_clean_real_owned_upload_ids(self):
        """Omit a genuine dry UploadPart ETag or fail Create with unknown server outcome; expect original failure, actual known-ID abort or real ListMultipartUploads-produced orphan-ID abort, then scoped object/bucket cleanup."""
        case=next(c for c in CASES if c.name=="ceph_atomic_stream_multipart_resend")
        for language in languages():
            for fault in ("part_missing_etag","unknown_create"):
                with self.subTest(language=language,fault=fault),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("mymultipart",)),"s3",handler=AtomicMultipartHandler) as (worker,server):
                    server.events=[];server.creates=server.mpu_lists=0;server.fault=fault
                    result=run_cases(context(worker,temporary),[case])[0];self.assertEqual(result["status"],"fail",result)
                    aborted=[parse_qs(urlsplit(row[1]).query)["uploadId"][0] for row in server.requests if row[0]=="DELETE" and "uploadId=" in row[1]]
                    self.assertEqual(aborted,["dry-real-%+/?=" if fault=="part_missing_etag" else "orphan-real-%+/?="])
                    self.assertEqual(server.mpu_lists,1 if fault=="part_missing_etag" else 2);self.assertEqual(len(server.fixtures),0)

    def test_dual_conditional_b_and_c_use_actual_initial_etag_on_same_sdk_client(self):
        """Return an independently pinned valid A-body MD5 ETag; expect real B and C requests to consume its unquoted value in If-Match, actual outer412/PreconditionFailed and final fresh full-B verification without disabling official SDK checksum validation."""
        case=next(c for c in CASES if c.name=="ceph_atomic_stream_dual_conditional_write")
        for language in languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("testobj",)),"s3",handler=AtomicConditionalHandler) as (worker,server):
                server.gets=0;result=run_cases(context(worker,temporary),[case])[0];self.assertEqual(result["status"],"pass",result)
                puts=[row for row in server.requests if row[0]=="PUT" and urlsplit(row[1]).path.endswith("/testobj")]
                self.assertEqual([row[3][:1] for row in puts],[b"A",b"B",b"C"])
                for row in puts:
                    headers={name.lower():value for name,value in row[2].items()};self.assertEqual(headers.get("if-match"),None if row[3].startswith(b"A") else "e6065c4aa2ab1603008fc18410f579d4")
                    self.assertEqual(len(row[3]),1024*1024)
                self.assertEqual(server.gets,2);self.assertEqual(len(server.fixtures),0)

    def test_bucket_gone_preserves_actual_delete_or_put_error_origin_and_code(self):
        """Execute real same-client bucket deletion inside positive EOF callbacks; expect source404/NoSuchBucket with actual nested-versus-outer origin, while a literal wrong outer NoSuchKey response remains a case failure."""
        case=next(c for c in CASES if c.name=="ceph_atomic_stream_bucket_gone")
        for language in languages():
            for code in ("NoSuchBucket","NoSuchKey"):
                with self.subTest(language=language,code=code),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+[(404,{},b"")],"s3",handler=AtomicGoneHandler) as (worker,server):
                    server.delete_calls=server.aborts=0;server.put_error_code=code
                    result=run_cases(context(worker,temporary),[case])[0]
                    self.assertEqual(result["status"],"pass" if code=="NoSuchBucket" or server.delete_calls>1 else "fail",result)
                    self.assertGreaterEqual(server.delete_calls,1);self.assertEqual(len(server.fixtures),0)
                    if result["status"]=="pass":self.assertEqual(result["evidence"]["error_origin"],"nested_callback" if server.delete_calls>1 else "outer_put")

    def test_real_same_client_version_jobs_overlap_list_and_consume_actual_ids(self):
        """Start original three rounds of three actual shared-client PUTs without phase joins; expect a coordinator list before held PUT replies, nine original bodies, four real listed opaque VersionIds fed into DELETE and actual final Versions absence; background SDK403 stays recorded evidence rather than a new source failure."""
        case=next(c for c in CASES if c.name=="ceph_atomic_versions_overlap")
        for language in languages():
            for background_error in (False,True):
                with self.subTest(language=language,background_error=background_error),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin(True)+scope_end(rows=[]),"s3",handler=AtomicVersionsHandler,overrides={"OSV_CONNECTIONS":"16"}) as (worker,server):
                    version_setup(server);server.background_error=background_error
                    result=run_cases(context(worker,temporary),[case])[0];self.assertEqual(result["status"],"pass",result)
                    self.assertEqual(server.barrier_timeouts,0);self.assertLess(server.events.index("coordinator_list"),server.events.index("put_reply"))
                    puts=[row for row in server.requests if row[0]=="PUT" and urlsplit(row[1]).path.endswith("/myobj")]
                    self.assertEqual(sorted(row[3] for row in puts),sorted([b"data 0",b"data 1",b"data 2"]*3))
                    deletes=[row for row in server.requests if row[0]=="DELETE" and urlsplit(row[1]).path.endswith("/myobj")]
                    self.assertEqual(sorted(parse_qs(urlsplit(row[1]).query)["versionId"][0] for row in deletes),sorted("listed-real-"+str(i)+"%+/?=" for i in range(4)))
                    errors=result["evidence"]["thread_errors"]
                    self.assertEqual(len(errors),1 if background_error else 0)
                    if errors:self.assertEqual(errors[0]["status_code"],403);self.assertIn("independent background PUT denied",str(errors))
                    self.assertEqual(len(server.fixtures),0)

    def test_version_coordinator_errors_join_real_jobs_before_owned_cleanup(self):
        """Deny a coordinator list, omit an actual listed VersionId, or retain a final Version; expect the original source failure, all started actual jobs joined before cleanup, real version-specific cleanup IDs and no unversioned-delete fallback."""
        case=next(c for c in CASES if c.name=="ceph_atomic_versions_overlap")
        for language in languages():
            for fault in ("list_denied","missing_id","remaining_version"):
                rows=[("myobj","unexpected-still-present")] if fault=="remaining_version" else [("myobj","put-id-"+str(i)) for i in range(3)]
                with self.subTest(language=language,fault=fault),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin(True)+scope_end(rows=rows),"s3",handler=AtomicVersionsHandler,overrides={"OSV_CONNECTIONS":"16"}) as (worker,server):
                    version_setup(server);server.version_fault=fault
                    result=run_cases(context(worker,temporary),[case])[0];self.assertEqual(result["status"],"fail",result)
                    self.assertEqual(server.barrier_timeouts,0);self.assertEqual(len(server.fixtures),0)
                    first_cleanup=server.events.index("owned_cleanup_list")
                    self.assertTrue(all(index<first_cleanup for index,event in enumerate(server.events) if event=="put_reply"))
                    deletes=[row for row in server.requests if row[0]=="DELETE" and urlsplit(row[1]).path.endswith("/myobj")]
                    self.assertTrue(all("versionId" in parse_qs(urlsplit(row[1]).query) for row in deletes))

    def test_noncanonical_initial_etag_keeps_real_sdk_validation_difference(self):
        """Return an opaque ETag containing internal quotes; expect Java's actual MD5-validation rejection before nested writes, while C++ consumes all-quotes-removed value in genuine B/C requests and still verifies final B; never disable official validation to manufacture agreement."""
        case=next(c for c in CASES if c.name=="ceph_atomic_stream_dual_conditional_write")
        for language in languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,scope_begin()+scope_end(("testobj",)),"s3",handler=AtomicConditionalHandler) as (worker,server):
                server.gets=0;server.initial_etag='"initial"internal"etag"'
                result=run_cases(context(worker,temporary),[case])[0]
                self.assertEqual(result["status"],"fail" if language=="java" else "pass",result)
                puts=[row for row in server.requests if row[0]=="PUT" and urlsplit(row[1]).path.endswith("/testobj")]
                if language=="java":self.assertIn("multiple of 2",str(result));self.assertEqual(len(puts),1)
                else:
                    for row in puts[1:]:self.assertEqual({name.lower():value for name,value in row[2].items()}["if-match"],"initialinternaletag")
                self.assertEqual(len(server.fixtures),0)
