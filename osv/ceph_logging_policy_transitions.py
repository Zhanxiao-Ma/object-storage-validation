"""Preserve logging wildcard account policies and live permission-change source oracles across actual SDK identities."""
from contextlib import contextmanager,ExitStack
from copy import deepcopy
import time
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_policy_remaining import IdentityScope
from .ceph_logging_controls import identity,document,set_policy,logging_status,rejected_code
from .ceph_logging_records import put,content,flush,keys,read,records
from .ceph_logging_extensions import supported
from .ceph_bucket_config import api,raw
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

@contextmanager
def buckets(p,alternate=False):
    """Create alternate-owned scope with real alt credentials and shut down every logger/policy before per-owner object cleanup."""
    with ExitStack() as stack:
        source=stack.enter_context(BucketScope(p));target=stack.enter_context(BucketScope(p))
        alt=stack.enter_context(IdentityScope(p,'alt')) if alternate else None
        all_buckets=[source,target]+([alt] if alt is not None else [])
        try:yield source,target,alt
        finally:
            failures=[]
            for bucket in all_buckets:
                result=raw(bucket,'PutBucketLogging',BucketLoggingStatus={})
                if not result.get('ok'):failures.append(result)
            for bucket in all_buckets:
                result=raw(bucket,'DeleteBucketPolicy')
                if not result.get('ok') and result.get('status_code')!=404:failures.append(result)
            if failures:raise RuntimeError('logging transition cleanup failed: '+str(failures))

def flush_denied(bucket):
    """Expose the real service error from the source's actual POST-or-5.5-second-dummy fallback rather than catching assertion text as a ClientError."""
    if 'ceph_bucket_logging_extensions' in bucket.probe.ctx.worker.capabilities:
        if 'api.PostBucketLogging' not in bucket.probe.ctx.worker.capabilities:raise CaseNotRun('actual POST logging operation is unavailable','api.PostBucketLogging')
        response=raw(bucket,'PostBucketLogging')
    else:
        time.sleep(5.5);bucket.probe.ctx.body.write_bytes(b'dummy');response=bucket.raw('put',key='dummy',body_path=str(bucket.probe.ctx.body))
    rejected_code(response,'AccessDenied')

def wildcard(p,objects):
    main=identity(p);alt_user=p.ctx.workload.get('identities',{}).get('alt',{}).get('user_id')
    if not isinstance(alt_user,str) or not alt_user:raise CaseNotRun('source wildcard account policy needs actual alternate user ID','workload.identities.alt.user_id')
    if 'identity_alt' not in p.ctx.worker.capabilities:raise CaseNotRun('alternate owner must use actual alternate SDK credentials','identity_alt')
    with buckets(p,True) as (source,target,alt):
        policy=document(p,source,target);statement=policy['Statement'][0];statement['Condition']['StringLike']=statement['Condition'].pop('StringEquals')
        other=deepcopy(statement);other['Condition']['ArnLike']['aws:SourceArn']='arn:aws:s3:::'+alt.name;other['Condition']['StringLike']['aws:SourceAccount']=alt_user;policy['Statement'].append(other)
        set_policy(target,policy)
        for bucket in (source,alt):assert_equal(api(bucket,'PutBucketLogging',BucketLoggingStatus=logging_status(bucket,target))['status_code'],200)
        policy['Statement'].pop();set_policy(target,policy)
        rejected_code(raw(alt,'PutBucketLogging',BucketLoggingStatus=logging_status(alt,target)),'AccessDenied')
        statement['Condition']['StringLike']['aws:SourceAccount']='*';statement['Condition']['ArnLike']['aws:SourceArn']='arn:aws:s3:::*';set_policy(target,policy)
        for bucket in (source,alt):assert_equal(api(bucket,'PutBucketLogging',BucketLoggingStatus=logging_status(bucket,target))['status_code'],200)
        if not objects:return
        source_keys=[]
        for index in range(5):
            for bucket,part in ((source,1),(alt,2)):
                key='myobject_'+str(part)+'_'+str(index);source_keys.append(key);put(bucket,p,key)
        flush(source);listed=keys(target);assert_equal(len(listed),1)
        for key in listed:
            assert_equal(key.startswith('log/'),True);body=read(target,p,key);records(body,' ','REST.PUT.OBJECT',source_keys,10)
        statement['Condition']['StringLike']['aws:SourceAccount']=main;set_policy(target,policy)
        for index in range(5):
            key='myobject_2_'+str(index);source_keys.append(key);put(alt,p,key)
        flush_denied(alt);assert_equal(len(keys(target)),1)

def permission_change(p,journal=False):
    if journal:supported(p,post=True)
    identity(p)
    with buckets(p) as (source,target,unused):
        policy=document(p,source,target);set_policy(target,policy)
        configuration=logging_status(source,target)
        if journal:configuration['LoggingEnabled']['LoggingType']='Journal'
        assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus=configuration)['status_code'],200)
        for index in range(5):put(source,p,'myobject'+str(index))
        policy['Statement'][0]['Condition']['ArnLike']['aws:SourceArn']='arn:aws:s3:::kaboom';set_policy(target,policy)
        for index in range(5,10):
            key='myobject'+str(index)
            if journal:
                p.ctx.body.write_bytes(content());response=source.raw('put_http',key=key,body_path=str(p.ctx.body));rejected_code(response,'AccessDenied')
                message=response.get('error_message');assert_equal(isinstance(message,str) and message.startswith('Logging bucket'),True)
            else:put(source,p,key)
        flush_denied(source);assert_equal(len(keys(target)),0)

BASE=SCOPE_OPERATIONS+' put put_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy list_v2_observed'
CASES=(case('ceph_logging_transition_wildcard','Ceph 日志双账号通配策略','原始双statement、StringLike、删alt授权后AccessDenied、账号/桶*放开；真实alt建桶与清理，不回退main',BASE+' identity_alt',lambda p:wildcard(p,False),'test_put_bucket_logging_policy_wildcard',CEPH),case('ceph_logging_transition_wildcard_objects','Ceph 日志双账号通配写入','保留通配策略完整前半、两账号10个交替写、真实flush与10日志、收紧账号后alt五写及flush AccessDenied且目标仍单日志',BASE+' identity_alt get_http',lambda p:wildcard(p,True),'test_put_bucket_logging_policy_wildcard_objects',CEPH),case('ceph_logging_transition_permission_s','Ceph 标准日志实时权限变化','保留初五写、改源ARN=kaboom后五写成功、真实5.5秒dummy或POST flush AccessDenied、目标零日志；不改变原始异常预期',BASE,lambda p:permission_change(p,False),'test_bucket_logging_permission_change_s',CEPH),case('ceph_logging_transition_permission_j','Ceph Journal日志实时权限变化','保留Journal类型、初五写、改源ARN后五写逐一AccessDenied且Message以Logging bucket开头、flush拒绝和零日志；缺原生扩展模型/POST整源not_run',BASE+' ceph_bucket_logging_extensions api.PostBucketLogging',lambda p:permission_change(p,True),'test_bucket_logging_permission_change_j',CEPH))

def register(registry):
    registry.add_suite(Suite('ceph_logging_policy_transitions',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
