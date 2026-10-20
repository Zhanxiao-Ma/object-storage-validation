"""Independent HTTP documents exercise remaining original S3 requests through genuine SDKs."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
from osv.ceph_s3_tail import chunked_write
from osv.scenarios import Context
from osv.suites import Probe
from sdk_fixtures import FixtureHandler,fixture_worker

EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'


class ChunkedDocuments(FixtureHandler):
    """Replay independent method-specific responses while recording even malformed actual transport framing."""
    def respond(self):
        self.server.observed.append((self.command,self.path,dict(self.headers)))
        if self.command=='HEAD':status=self.server.head_statuses.pop(0);document=(status,{},b'')
        elif self.command=='GET':document=(200,{'Content-Type':'application/xml'},EMPTY)
        elif self.command=='DELETE':document=(204,{},b'')
        elif self.path.endswith('/foo'):document=(self.server.put_status,{'Content-Type':'application/xml'},b'' if self.server.put_status==200 else b'<Error><Code>InvalidRequest</Code><Message>independent PUT rejection</Message></Error>')
        else:document=(200,{},b'')
        self.server.fixtures.appendleft(document)
        self.connection.settimeout(2)
        try:super().respond()
        except (socket.timeout,ValueError):
            self.close_connection=True
            self.server.framing_errors.append(self.path)


def independent_server(language,status):
    class Documents(ChunkedDocuments):
        def respond(self):
            if not hasattr(self.server,'observed'):
                self.server.observed=[];self.server.head_statuses=[404,200,404];self.server.put_status=status;self.server.framing_errors=[]
            super().respond()
        do_GET=do_HEAD=do_PUT=do_POST=do_DELETE=respond
    return fixture_worker(language,[],'s3',handler=Documents)


@unittest.skipUnless(os.environ.get('OSV_RUN_S3_TAIL_TESTS')=='1','enable genuine SDK S3 tail fixtures')
class S3TailSdkTest(unittest.TestCase):
    """Retain the source status oracle and actual SDK transfer framing, including failure cleanup."""
    def probe(self,worker,directory):
        return Probe(Context(worker,'unused/',directory,b'',directory/'body',directory/'output',{}))

    def test_chunked_header_uses_actual_sdk_transport_and_always_cleans_owned_bucket(self):
        """Apply the original chunked header to a three-byte file; require C++ source success and preserve Java's actual pre-transport SdkClientException, with no header leaking into cleanup and final bucket deletion confirmed."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,independent_server(language,200) as (worker,server):
                if language=='java':
                    with self.assertRaisesRegex(RuntimeError,'SdkClientException: Unable to execute HTTP request'):
                        chunked_write(self.probe(worker,Path(temporary)))
                    self.assertFalse(any(method=='PUT' and path.endswith('/foo') for method,path,_ in server.observed))
                else:
                    result=chunked_write(self.probe(worker,Path(temporary)));self.assertTrue(result['cleanup_confirmed'])
                self.assertEqual(server.head_statuses,[])
                self.assertEqual(sum(method=='DELETE' and not path.endswith('/foo') for method,path,_ in server.observed),1)
                for method,path,headers in server.observed:
                    lowered={k.lower():v for k,v in headers.items()}
                    if method=='PUT' and path.endswith('/foo'):self.assertEqual(lowered.get('transfer-encoding'),'chunked')
                    else:self.assertNotIn('transfer-encoding',lowered)
                puts=[request for request in server.requests if request[0]=='PUT' and request[1].endswith('/foo')]
                for _,_,_,body in puts:self.assertEqual(body,b'bar')

    def test_actual_put_rejection_remains_failure_and_cleanup_is_outside_header_scope(self):
        """Return a genuine HTTP400 to the actual C++ chunked PUT, and retain Java's earlier transport failure; expect source failure and scoped bucket cleanup without changing source's required200 oracle."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,independent_server(language,400) as (worker,server):
                expected='SdkClientException: Unable to execute HTTP request' if language=='java' else 'independent PUT rejection'
                with self.assertRaisesRegex(RuntimeError,expected):chunked_write(self.probe(worker,Path(temporary)))
                self.assertEqual(server.head_statuses,[])
                self.assertEqual(sum(method=='DELETE' and not path.endswith('/foo') for method,path,_ in server.observed),1)
                for method,path,headers in server.observed:
                    lowered={k.lower():v for k,v in headers.items()}
                    if method!='PUT' or not path.endswith('/foo'):self.assertNotIn('transfer-encoding',lowered)
                if language=='cpp':
                    put=next(row for row in server.requests if row[0]=='PUT' and row[1].endswith('/foo'))
                    self.assertEqual(put[3],b'bar');self.assertEqual({k.lower():v for k,v in put[2].items()}['transfer-encoding'],'chunked')
