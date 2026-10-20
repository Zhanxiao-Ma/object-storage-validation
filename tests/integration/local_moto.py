"""Run genuine SDK integration against process-isolated serialized Moto services."""
import logging
import multiprocessing
from threading import Thread


def _serve(ready,stopped):
    """Own a fresh Moto backend in a spawned process and report its actual ephemeral HTTP endpoint."""
    server=None;thread=None
    try:
        from moto.server import DomainDispatcherApplication,create_backend_app
        from werkzeug.serving import make_server
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        server=make_server("127.0.0.1",0,DomainDispatcherApplication(create_backend_app),threaded=False)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        ready.send(("ready",server.server_address[:2]));ready.close()
        stopped.wait()
    except BaseException as error:
        try:ready.send(("error",repr(error)))
        except (OSError,EOFError):pass
        raise
    finally:
        ready.close()
        if server is not None:
            if thread is not None:server.shutdown()
            server.server_close()
        if thread is not None:thread.join(timeout=10)


class LocalMotoServer:
    """Isolate module-global Moto state and avoid its concurrent-overwrite buffer race.

    Each spawned process owns a fresh backend, so concurrent or sequential services
    cannot inherit another test's objects. Its serialized HTTP handler still receives
    genuine SDK and production-runner requests; client concurrency is preserved and
    local timings never provide evidence of cloud performance.
    """
    def __init__(self):
        self.process=None;self.address=None;self.stopped=None

    def start(self):
        if self.process is not None:raise RuntimeError("local Moto service already started")
        context=multiprocessing.get_context("spawn")
        receive,send=context.Pipe(duplex=False);self.stopped=context.Event()
        self.process=context.Process(target=_serve,args=(send,self.stopped),daemon=True)
        self.process.start();send.close()
        try:
            if not receive.poll(30):raise RuntimeError("local Moto service startup timed out")
            status,value=receive.recv()
            if status!="ready":raise RuntimeError("local Moto service startup failed: "+str(value))
            self.address=value
        except BaseException:
            self.stop();raise
        finally:receive.close()

    def get_host_and_port(self):
        if self.address is None:raise RuntimeError("local Moto service is not running")
        return str(self.address[0]),self.address[1]

    def stop(self):
        process=self.process
        if process is None:return
        self.stopped.set();process.join(timeout=10)
        if process.is_alive():
            process.terminate();process.join(timeout=5)
            self.process=None;self.address=None
            raise RuntimeError("local Moto service did not stop")
        self.process=None;self.address=None
        if process.exitcode!=0:raise RuntimeError("local Moto service exited with code "+str(process.exitcode))
