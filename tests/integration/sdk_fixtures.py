"""Shared raw HTTP fixtures for real SDK adapter contract tests; no storage state machine."""
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
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

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.fixtures = deque(fixtures)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = (["java", "-Xmx256m", "-jar", str(ROOT/"drivers/java/target/sdk-worker-0.1.0.jar"), backend]
               if language == "java" else [str(ROOT/("drivers/cpp/build/osv-cpp-"+backend))])
    environment = {"OSV_ACCESS_KEY":"local-test", "OSV_SECRET_KEY":"local-test", "OSV_SESSION_TOKEN":"",
        "OSV_ENDPOINT":f"http://127.0.0.1:{server.server_port}", "OSV_BUCKET":"fixture-1250000000",
        "OSV_REGION":"ap-guangzhou", "OSV_RETRIES":"0", "OSV_CONNECTIONS":"2"}
    if backend == "obs":
        environment.update(OSV_PATH_STYLE="false", OSV_CNAME="true", OSV_RETRIES="sdk-default")
    environment.update(overrides or {})
    target = {"command":command, "language":language, "api":"native"}
    if backend == "obs" and os.environ.get("OSV_OBS_LIBRARY_PATH"):
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
