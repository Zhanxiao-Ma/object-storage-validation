"""Shared raw HTTP fixtures for real SDK adapter contract tests; no storage state machine."""
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import io
from pathlib import Path
import threading
from unittest.mock import patch

from osv.worker import Worker

ROOT = Path(__file__).resolve().parents[2]


class FixtureHandler(BaseHTTPRequestHandler):
    """Record real requests and replay supplied raw responses without implementing storage behavior."""
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # SDK may abort the connection after a deliberate output failure.

    def log_message(self, *args):
        pass

    @staticmethod
    def chunks(stream):
        """Decode HTTP chunk framing/extensions without implementing storage, signatures or SDK response logic."""
        parts=[]
        while True:
            line=stream.readline(4096)
            if not line:raise ValueError("missing chunk size")
            size=int(line.split(b";",1)[0].strip(),16)
            if size==0:
                while stream.readline(4096) not in (b"\r\n",b"\n",b""):pass
                return b"".join(parts)
            if size<0 or size>16*1024*1024:raise ValueError("fixture chunk too large")
            data=stream.read(size)
            if len(data)!=size or stream.read(2)!=b"\r\n":raise ValueError("invalid chunk framing")
            parts.append(data)

    def respond(self):
        if "chunked" in self.headers.get("Transfer-Encoding","").lower():
            body=self.chunks(self.rfile)
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        decoded=self.headers.get("x-amz-decoded-content-length")
        signed_stream=self.headers.get("x-amz-content-sha256","").startswith("STREAMING-")
        if ("aws-chunked" in self.headers.get("Content-Encoding","") or signed_stream) and (decoded is None or len(body)!=int(decoded)):
            body=self.chunks(io.BytesIO(body))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        status, headers, payload = self.server.fixtures.popleft()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_PUT = do_HEAD = do_POST = do_DELETE = respond


@contextmanager
def fixture_worker(language, fixtures, backend="cos", overrides=None):
    # Pinned TOS C++ SDK rejects every endpoint containing a port. A loopback HTTP proxy
    # routes its standard-port endpoint to our ephemeral fixture without privileges or SDK substitution.
    proxy_for_tos = backend=="tos" and language=="cpp"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.fixtures = deque(fixtures)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = (["java", "-Xmx256m", "-jar", str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"), backend]
               if language == "java" else [str(ROOT/("drivers/cpp/build/osv-cpp-"+backend))])
    environment = {"OSV_ACCESS_KEY":"local-test", "OSV_SECRET_KEY":"local-test", "OSV_SESSION_TOKEN":"",
        "OSV_ENDPOINT":f"http://127.0.0.1:{server.server_port}", "OSV_BUCKET":"fixture-1250000000",
        "OSV_REGION":"ap-guangzhou", "OSV_RETRIES":"0", "OSV_CONNECTIONS":"2", "OSV_PATH_STYLE":"true", "OSV_CNAME":"true",
        "http_proxy":"","HTTP_PROXY":"","https_proxy":"","HTTPS_PROXY":"","ALL_PROXY":"","all_proxy":"",
        "no_proxy":"127.0.0.1,localhost","NO_PROXY":"127.0.0.1,localhost"}
    if backend == "obs":
        environment.update(OSV_PATH_STYLE="false", OSV_CNAME="true")
        if language=="cpp":environment["OSV_RETRIES"]="sdk-default"
        else:
            # OBS Java rewrites OBS auth to AWS V2 for IP/CNAME modes. Keep its native DNS mode;
            # the SDK's explicit HTTP proxy sends the genuine request to our loopback fixture.
            environment.update(OSV_ENDPOINT="http://obs.fixture.invalid", OSV_CNAME="false",
                               OSV_HTTP_PROXY=f"http://127.0.0.1:{server.server_port}")
    # All SDK operations still run against the real client; only the loopback DNS endpoint differs.
    if proxy_for_tos:
        environment.update(OSV_ENDPOINT="http://localhost",http_proxy=f"http://127.0.0.1:{server.server_port}",
                           HTTP_PROXY=f"http://127.0.0.1:{server.server_port}",no_proxy="",NO_PROXY="")
    environment.update(overrides or {})
    target = {"command":command, "language":language, "api":"s3" if backend=="s3" else "native"}
    if backend == "obs" and language == "cpp" and os.environ.get("OSV_OBS_LIBRARY_PATH"):
        target["env"] = {"LD_LIBRARY_PATH":os.environ["OSV_OBS_LIBRARY_PATH"]}
    try:
        with patch.dict(os.environ, environment):
            worker = Worker(target, 15)
            try:
                yield worker, server
            finally:
                worker.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
