"""Preserve original Python-thread workloads on one persistent official SDK client using actual bounded asynchronous RPC jobs."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
from .ceph_logging_remaining_extension_runtime import ExtensionRuntime,ExtensionClient
from .ceph_logging_remaining_runtime import current,response
from .suites import Probe,CaseNotRun


class SharedAsyncWorker:
    """Forward every scoped operation to the same real worker's asynchronous SDK job API, without constructing another SDK session or matching responses by arrival order."""
    def __init__(self,worker):self.worker=worker
    def __getattr__(self,name):return getattr(self.worker,name)
    def call(self,request):return self.worker.async_call(request)


class AsyncClient(ExtensionClient):
    """Avoid parallel callers overwriting one body file: retain each real source body in an independent temporary file until its genuine SDK future completes."""
    def put_object(self,**values):
        body=values['Body'];data=body.encode() if isinstance(body,str) else body
        with tempfile.NamedTemporaryFile(prefix='logging-put-',dir=self.p.ctx.directory) as stream:
            stream.write(data);stream.flush()
            return response(self.call('put_http',values['Bucket'],key=values['Key'],body_path=stream.name))


class AsyncRuntime(ExtensionRuntime):
    """Require genuine shared-client async support and original explicit pool size, then retain normal UUID ownership/cleanup while source threads interleave actual requests."""
    def __init__(self,p,require_pool100=False):
        self.original=p;self.require_pool100=require_pool100
        super().__init__(Probe(replace(p.ctx,worker=SharedAsyncWorker(p.ctx.worker))),'parallel')
    def __enter__(self):
        if 'async_requests' not in self.original.ctx.worker.capabilities or not callable(getattr(self.original.ctx.worker,'async_call',None)):raise CaseNotRun('Original source threads require actual same-client asynchronous SDK requests','async_requests')
        if self.require_pool100:
            size=self.original.ctx.worker.info.get('connections_per_worker')
            try:valid=not isinstance(size,bool) and int(size)>=100
            except (ValueError,TypeError):valid=False
            if not valid:raise CaseNotRun('Original 300-thread source explicitly configures SDK max_pool_connections=100','OSV_CONNECTIONS>=100 actual SDK handshake')
            limit=self.original.ctx.worker.info.get('async_jobs_limit',64)
            if type(limit) is not int or limit<330:
                raise CaseNotRun('Original source starts 300 PUT plus30 flush tasks; the harness must not add a smaller admission bound','async_jobs_limit>=330 actual SDK handshake')
        super().__enter__();self.main=AsyncClient(self,self.p);return self


class SourceClientConfig:
    """Retain the original source's explicit connection-pool request and validate it against the actual already-configured official SDK client."""
    def __init__(self,**values):
        if set(values)!={'max_pool_connections'} or type(values['max_pool_connections']) is not int:raise ValueError('unsupported original client configuration')
        self.max_pool_connections=values['max_pool_connections']


def get_client(client_config=None):
    runtime=current()
    if client_config is not None:
        size=runtime.original.ctx.worker.info.get('connections_per_worker')
        if not isinstance(client_config,SourceClientConfig) or int(size)<client_config.max_pool_connections:raise CaseNotRun('Actual SDK connection pool cannot satisfy original source configuration','OSV_CONNECTIONS')
    return runtime.main

botocore=SimpleNamespace(config=SimpleNamespace(Config=SourceClientConfig))
