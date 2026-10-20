"""Preserve all seven original cross-tenant standard logging address/identity vectors."""
from contextlib import contextmanager,ExitStack
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_logging_controls import identity,set_policy,rejected_code
from .ceph_logging_records import put,flush,keys,read,records
from .ceph_logging_workloads import extensions
from .ceph_bucket_config import api,raw
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

class NamespaceScope(BucketScope):
    """Prove a fresh UUID bucket separately in each real credential namespace, including identical main/tenant bare names; clean only through its original owner."""
    def __init__(self,p,identity,name=None):super().__init__(p,name);self.identity=identity
    def raw(self,op,**options):options.setdefault('identity',self.identity);return super().raw(op,**options)

@contextmanager
def pair(p,source_identity,target_identity,same):
    with ExitStack() as stack:
        source=stack.enter_context(NamespaceScope(p,source_identity));target=stack.enter_context(NamespaceScope(p,target_identity,source.name if same else None))
        try:yield source,target
        finally:
            failures=[]
            for bucket in (source,target):
                result=raw(bucket,'PutBucketLogging',BucketLoggingStatus={})
                if not result.get('ok'):failures.append(result)
            for bucket in (source,target):
                result=raw(bucket,'DeleteBucketPolicy')
                if not result.get('ok') and result.get('status_code')!=404:failures.append(result)
            if failures:raise RuntimeError('tenant logging cleanup failed: '+str(failures))

VECTORS=(('main','tenant',False,'qualified',True),('main','tenant',True,'qualified',True),('main','tenant',False,'bare',False),('tenant','tenant',False,'qualified',True),('tenant','tenant',False,'bare',True),('tenant','main',False,'default',True),('tenant','main',False,'bare',False))

def tenant_standard(p):
    main=identity(p);tenant=p.ctx.workload.get('tenant_name');tenant_user=p.ctx.workload.get('identities',{}).get('tenant',{}).get('user_id')
    if not isinstance(tenant,str) or not tenant:raise CaseNotRun('original namespace vectors require an actual configured tenant name','workload.tenant_name')
    if not isinstance(tenant_user,str) or not tenant_user:raise CaseNotRun('original source policy requires actual tenant user ID','workload.identities.tenant.user_id')
    if 'identity_tenant' not in p.ctx.worker.capabilities:raise CaseNotRun('tenant owner cannot fall back to main credentials','identity_tenant')
    extensions(p)
    for source_identity,target_identity,same,address,valid in VECTORS:
        with pair(p,source_identity,target_identity,same) as (source,target):
            src_tenant=tenant if source_identity=='tenant' else '';dst_tenant=tenant if target_identity=='tenant' else ''
            policy={'Version':'2012-10-17','Statement':[{'Sid':'S3ServerAccessLogsPolicy','Effect':'Allow','Principal':{'Service':'logging.s3.amazonaws.com'},'Action':['s3:PutObject'],'Resource':'arn:aws:s3::'+dst_tenant+':'+target.name+'/log/','Condition':{'ArnLike':{'aws:SourceArn':'arn:aws:s3::'+src_tenant+':'+source.name},'StringEquals':{'aws:SourceAccount':tenant_user if source_identity=='tenant' else main}}}]}
            set_policy(target,policy)
            target_name=(tenant+':' if address=='qualified' else ':' if address=='default' else '')+target.name
            configuration={'LoggingEnabled':{'TargetBucket':target_name,'TargetPrefix':'log/'}}
            if not valid:rejected_code(raw(source,'PutBucketLogging',BucketLoggingStatus=configuration),'NoSuchKey');continue
            assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus=configuration)['status_code'],200)
            for index in range(5):put(source,p,'myobject'+str(index))
            source_keys=keys(source);flushed=flush(source);listed=keys(target);assert_equal(len(listed),1)
            for key in listed:
                if flushed is not None:assert_equal(key,flushed)
                assert_equal(key.startswith('log/'),True);records(read(target,p,key),source.name,'REST.PUT.OBJECT',source_keys,5)
    return {'source_vectors':7,'successful_log_vectors':5,'negative_no_such_key_vectors':2}

CASES=(case('ceph_logging_tenant_standard','Ceph 完整七组标准跨tenant日志','保留不同名/同名跨namespace、显式tenant:/同tenant bare/:默认namespace以及两次bare目标NoSuchKey；五组完整五写/List/真实flush/单日志/5记录，始终原身份创建和清理',SCOPE_OPERATIONS+' identity_tenant put put_http list_v2_observed get_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy',tenant_standard,'test_put_bucket_logging_tenant_s',CEPH),)

def register(registry):
    registry.add_suite(Suite('ceph_logging_tenants',CASES[0].operations,lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
