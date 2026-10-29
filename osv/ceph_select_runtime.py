"""Bind statically migrated Select bodies to official SDK requests and genuine event payloads."""
from contextvars import ContextVar
from contextlib import ExitStack
import io,uuid
from .bucket_scope import BucketScope
from .ceph_bucket_names import NamingScope
from .ceph_request_headers import recover_creation
from .suites import CaseNotRun
from .scenarios import SDKOperationError

_ACTIVE=ContextVar('ceph_select_runtime')
LITERAL_SOURCES={'test_json_column_sum_min_max':('testbuck2',),'test_column_sum_min_max':('testbuck2',)}

class ClientError(SDKOperationError):
    """Preserve native service code/message and source call-phase exception handling."""
    def __init__(self,result,operation="SelectObjectContent"):
        SDKOperationError.__init__(self,result)
        self.evidence["operation"]=operation
        self.response={'Error':{'Code':result.get('error_code'),'Message':result.get('error_message',result.get('error',''))},'ResponseMetadata':{'HTTPStatusCode':result.get('status_code')}}
        self.args=('An error occurred ('+str(self.response['Error']['Code'])+') when calling the '+operation+' operation: '+str(self.response['Error']['Message']),)

class EventStreamError(ClientError):
    """Expose the actual official decoder's service event failure separately from HTTP/transport errors."""
    def __init__(self, result):
        super().__init__(result)
        self.failure_category = "service_error"
        self.evidence["failure_phase"] = "event_stream"



def checked(result,operation="SelectObjectContent"):
    if not result.get('ok'):
        if result.get('error_code') and isinstance(result.get('status_code'),int) and result['status_code']>0:raise ClientError(result,operation)
        raise SDKOperationError(result)
    return result


def payload(result,path):
    """Restore each genuine SDK Records chunk separately; never join before the source UTF-8 decode."""
    offset=0
    with path.open('rb') as stream:
        for event in result['events']:
            kind=event['type']
            if kind=='Records':
                start=event['offset'];length=event['length']
                if type(start) is not int or type(length) is not int or start!=offset or length<0:raise RuntimeError('Actual Records event boundaries are invalid')
                data=stream.read(length)
                if len(data)!=length:raise RuntimeError('Actual Records payload is truncated')
                offset+=length;yield {'Records':{'Payload':data}}
            elif kind in ('Progress','Stats'):yield {kind:{'Details':event['Details']}}
            elif kind in ('Cont','End'):yield {kind:{}}
            elif kind=='Error':
                if event.get('error_kind') in ('decoder','transport'):raise RuntimeError('Actual SDK stream '+event['error_kind']+' failure: '+str(event.get('error_message')))
                raise EventStreamError(event)
            else:raise RuntimeError('Unknown official Select callback event '+str(kind))
        if stream.read(1) or offset!=result['records_size']:raise RuntimeError('Actual Records size differs from callback boundaries')


class Client:
    """Translate only original source API arguments; all storage and Select behavior is executed by official SDK workers."""
    def __init__(self,runtime):self.r=runtime
    def create_bucket(self,**values):
        name=values['Bucket'];scope=self.r.buckets.get(name)
        if scope is None:
            if name not in self.r.literals:raise RuntimeError('Source bucket was not generated or authorized by this scenario')
            scope=NamingScope(self.r.p,name);self.r.buckets[name]=scope
        if scope.object_namespace:
            if not getattr(scope,'namespace_entered',False):
                scope.__enter__();scope.namespace_entered=True
            # Creation is fixture setup, not the Select oracle; check the real configured bucket instead.
            return checked(self.r.p.ctx.worker.call({'op':'bucket_head','bucket':scope.name}), 'HeadBucket')
        if not scope.created:
            head=scope.raw('bucket_head')
            if head.get('ok') or (head.get('error_kind'),head.get('status_code'))!=('not_found',404):
                if name in self.r.literals:raise CaseNotRun('Original literal Select bucket is not demonstrably absent','HEAD 404 for '+name)
                raise RuntimeError('Cannot establish generated Select bucket absence')
        try:result=scope.raw('bucket_create_raw')
        except Exception:
            if not scope.created:self.r.ambiguous.add(name)
            raise
        if result.get('ok'):scope.created=True
        elif not scope.created and not (isinstance(result.get('status_code'),int) and 400<=result['status_code']<500):self.r.ambiguous.add(name)
        return checked(result,"CreateBucket")
    def put_object(self,**values):
        scope=self.r.owned(values['Bucket']);body=values['Body'];self.r.p.ctx.body.write_bytes(body.encode() if isinstance(body,str) else body)
        return checked(scope.raw('put_http',key=values['Key'],body_path=str(self.r.p.ctx.body)), 'PutObject')
    def get_object(self,**values):
        scope=self.r.owned(values['Bucket']);path=self.r.p.ctx.output;checked(scope.raw('get_http',key=values['Key'],output_path=str(path)), 'GetObject')
        return {'Body':io.BytesIO(path.read_bytes())}
    def select_object_content(self,**values):
        scope=self.r.owned(values['Bucket']);path=self.r.p.ctx.output
        result=checked(scope.raw('select',input=values,output_path=str(path)))
        return {'Payload':payload(result,path)}


class Runtime:
    """Own every generated source bucket and clean even after a real SDK event error or original assertion failure."""
    def __init__(self,p,literals=()):self.p=p;self.buckets={};self.literals=frozenset(literals);self.ambiguous=set()
    def __enter__(self):
        if self.literals and self.p.ctx.workload.get('ceph_literal_bucket_names_authorized') is not True:raise CaseNotRun('Original Select source uses fixed testbuck2; dedicated literal namespace authorization is required','workload.ceph_literal_bucket_names_authorized=true')
        # Establish literal isolation before running any source generator or
        # first-bucket write; recheck immediately before its actual create.
        for name in self.literals:
            scope=NamingScope(self.p,name);result=scope.raw('bucket_head')
            if result.get('ok') or (result.get('error_kind'),result.get('status_code'))!=('not_found',404):raise CaseNotRun('Original literal Select bucket is not demonstrably absent','HEAD 404 for '+name)
        self.token=_ACTIVE.set(self);return self
    def __exit__(self,*args):
        try:
            with ExitStack() as stack:
                for scope in self.buckets.values():stack.callback(self.cleanup,scope,args)
        finally:_ACTIVE.reset(self.token)
    def cleanup(self,scope,args):
        if scope.name not in self.ambiguous:return scope.__exit__(*args)
        try:
            result=scope.raw('bucket_head')
            if (result.get('error_kind'),result.get('status_code'))==('not_found',404):
                self.ambiguous.discard(scope.name);return False
            if not result.get('ok'):raise RuntimeError('actual HEAD cannot confirm candidate state')
            expected=self.p.ctx.workload.get('identities',{}).get('main',{}).get('canonical_id')
            if not isinstance(expected,str) or not expected:raise RuntimeError('missing independent workload.identities.main.canonical_id')
            # Existing recovery helper creates a separate proven-owned control
            # bucket, consumes its real ACL and the candidate's real ACL, and
            # deletes the candidate only if both match this independent ID.
            recover_creation(scope,expected_owner=expected)
            self.ambiguous.discard(scope.name)
        except Exception as error:
            original=str(args[1]) if args[1] is not None else 'original SDK create failed'
            raise RuntimeError(original+'; unknown bucket '+scope.name+'; cleanup_pending: '+str(error)) from error
        return False

    def owned(self,name):
        scope=self.buckets.get(name)
        if scope is None or not (scope.created or getattr(scope,'namespace_entered',False)):raise RuntimeError('Refusing operation outside a successfully established Select scope')
        return scope
    def name(self):
        scope=BucketScope(self.p)
        if scope.name in self.buckets:
            raise CaseNotRun('Original source needs another distinct bucket','second independently configured test bucket')
        self.buckets[scope.name]=scope;return scope.name


def get_client():return Client(_ACTIVE.get())
def get_new_bucket_name():return _ACTIVE.get().name()


def execute(p,name):
    if name in ('test_version','test_output_serial_expressions'):raise CaseNotRun('Original source immediately returns; no operation or oracle is executed','upstream_todo')
    literals=LITERAL_SOURCES.get(name,())
    if literals and p.ctx.workload.get('ceph_literal_bucket_names_authorized') is not True:raise CaseNotRun('Original Select source uses fixed testbuck2; dedicated literal namespace authorization is required','workload.ceph_literal_bucket_names_authorized=true')
    from . import ceph_select_sources as source
    with Runtime(p,literals):getattr(source,name)()
    return {'source_function':name,'oracle':'exact result equality; service and stream errors propagate','decoder':'official SDK event stream; per-Records-event UTF-8 handling retained'}
