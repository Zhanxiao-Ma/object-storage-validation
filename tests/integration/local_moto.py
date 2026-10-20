"""Run genuine SDK integration against a serialized local Moto WSGI service."""
from threading import Thread
from moto.server import DomainDispatcherApplication,create_backend_app
from werkzeug.serving import make_server

class LocalMotoServer:
    """Avoid Moto's concurrent-overwrite buffer race while preserving genuine SDK and production harness requests.

    Moto 5.1.15 can close a previous object's buffer while another request is still
    calculating that buffer's ETag. This loopback contract fixture serializes only
    the service handler. Client concurrency and the tested workload remain intact;
    local timing is never evidence of a cloud service's performance.
    """
    def __init__(self):
        self.server=make_server("127.0.0.1",0,DomainDispatcherApplication(create_backend_app),threaded=False)
        self.thread=Thread(target=self.server.serve_forever,daemon=True)

    def start(self):
        self.thread.start()

    def get_host_and_port(self):
        host,port=self.server.server_address[:2]
        return str(host),port

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=10)
        if self.thread.is_alive():raise RuntimeError("local Moto server did not stop")
