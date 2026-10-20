"""Prove real SDK atomic sink callbacks using a held-open HTTP response, not request batches."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
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
