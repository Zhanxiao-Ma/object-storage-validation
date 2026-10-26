"""Optional actual-SDK disconnect regression; no S3 response is simulated."""
import os
from pathlib import Path
import signal
import socket
import struct
import tempfile
import threading
import unittest
from osv.worker import Worker


@unittest.skipUnless(os.environ.get('OSV_MINIO_CPP_BINARY'),'set OSV_MINIO_CPP_BINARY to a built MinIO C++ worker')
class MinioDisconnectTest(unittest.TestCase):
    """Exercise real socket failures and signal handling through the production JSONL client."""
    def test_reset_peer_and_sigpipe_do_not_kill_driver(self):
        """Purpose: reset real SDK uploads and deliver SIGPIPE; expect explicit failed requests, a live driver, and a successful later handshake."""
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(.1)
        stopped=threading.Event()
        def reset():
            while not stopped.is_set():
                try:connection,_=listener.accept()
                except socket.timeout:continue
                except OSError:return
                with connection:
                    connection.settimeout(2)
                    try:connection.recv(128)
                    except OSError:pass
                    connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
        thread=threading.Thread(target=reset,daemon=True);thread.start()
        target={'command':[os.environ['OSV_MINIO_CPP_BINARY']],'env':{'OSV_ENDPOINT':f'http://127.0.0.1:{listener.getsockname()[1]}',
            'OSV_ACCESS_KEY':'local','OSV_SECRET_KEY':'local-secret','OSV_BUCKET':'disconnect-test','OSV_REGION':'us-east-1'}}
        worker=Worker(target,5)
        try:
            with tempfile.TemporaryDirectory() as directory:
                body=Path(directory)/'body';body.write_bytes(b'x'*(8*1024**2))
                for _ in range(20):
                    result=worker.call({'op':'put','key':'x','body_path':str(body)})
                    self.assertFalse(result['ok']);self.assertEqual(result['error_kind'],'sdk_error')
                os.kill(worker.process.pid,signal.SIGPIPE)
                self.assertTrue(worker.call({'op':'info'})['ok'])
                self.assertIsNone(worker.process.poll())
        finally:
            worker.close();stopped.set();listener.close();thread.join(timeout=3)
