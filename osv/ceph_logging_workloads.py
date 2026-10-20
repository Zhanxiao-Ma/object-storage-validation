"""Preserve standard SDK logging workload fallbacks and actual version/multipart producer-consumer chains."""
from contextlib import contextmanager,ExitStack
from copy import deepcopy
import shlex
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_versioning import configure,multipart
from .ceph_multipart import source_data,MIB
from .ceph_logging_controls import identity,document,set_policy,logging_status
from .ceph_logging_records import put,flush,keys,read,records
from .ceph_bucket_config import api,raw
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

@contextmanager
def resources(p,versioned,another=False):
    """Keep each bucket independently owned and stop every logger before purging actual versions and destination objects."""
    with ExitStack() as stack:
        source=stack.enter_context((VersionScope if versioned else BucketScope)(p))
        destination=stack.enter_context(BucketScope(p)) if another else source
        target=stack.enter_context(BucketScope(p))
        buckets=[source]+([destination] if another else [])+[target]
        try:
            if versioned:configure(source,'Enabled')
            yield source,destination,target
        finally:
            failures=[]
            for bucket in buckets:
                response=raw(bucket,'PutBucketLogging',BucketLoggingStatus={})
                if not response.get('ok'):failures.append(response)
            for bucket in buckets:
                response=raw(bucket,'DeleteBucketPolicy')
                if not response.get('ok') and response.get('status_code')!=404:failures.append(response)
            if failures:raise RuntimeError('workload logging/policy cleanup failed: '+str(failures))

def extensions(p):
    enabled='ceph_bucket_logging_extensions' in p.ctx.worker.capabilities
    if enabled and 'api.PostBucketLogging' not in p.ctx.worker.capabilities:raise CaseNotRun('Ceph logging branch requires actual POST logging SDK operation','api.PostBucketLogging')
    return enabled

def enable(p,source,destination,target,operation,extension):
    policy=document(p,source,target)
    if operation=='copy':
        # Source passes [source,destination] even for same-bucket copies.
        other=deepcopy(policy['Statement'][0]);other['Condition']['ArnLike']['aws:SourceArn']='arn:aws:s3:::'+destination.name
        policy['Statement'].append(other)
    set_policy(target,policy)
    value=logging_status(source,target)
    journal=operation not in ('get','head','mpu')
    if extension and operation!='mpu':value['LoggingEnabled'].update({'ObjectRollTime':5,'LoggingType':'Journal' if journal else 'Standard'})
    assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus=value)['status_code'],200)
    if destination is not source:assert_equal(api(destination,'PutBucketLogging',BucketLoggingStatus=value)['status_code'],200)
    return 'Journal' if extension and journal else 'Standard'

def verify(body,bucket,event,objects,expected,kind):
    if kind=='Standard':return records(body,bucket,event,objects,expected)
    matches=0
    for line in body.splitlines():
        chunks=shlex.split(line.replace('[','"').replace(']','"'));assert_equal(len(chunks),8)
        if bucket in line and event in line and any(key in line for key in objects):matches+=1
    assert_equal(matches,expected)

def consume(p,source,target,bucket,event,objects,count,kind,second_event=None,check_flushed=False):
    flushed=flush(source);listed=keys(target);assert_equal(len(listed),1)
    for key in listed:
        if check_flushed and flushed is not None:assert_equal(key,flushed)
        assert_equal(key.startswith('log/'),True)
        body=read(target,p,key);verify(body,bucket,event,objects,count,kind)
        if second_event is not None:verify(body,bucket,second_event[0],objects,second_event[1],kind)

def workload(p,operation,versioned=False,another=False):
    identity(p);extension=extensions(p)
    with resources(p,versioned,another) as (source,destination,target):
        if operation in ('put','mpu'):kind=enable(p,source,destination,target,operation,extension)
        if operation in ('mpu','mpu_copy'):
            data=source_data(30*MIB)
            multipart(source,p,'myobject',data)
            if versioned:multipart(source,p,'myobject',data)
        else:
            for index in range(5):
                put(source,p,'myobject'+str(index))
                if versioned and operation!='head':put(source,p,'myobject'+str(index))
        if operation not in ('put','mpu'):kind=enable(p,source,destination,target,'copy' if operation=='copy' else operation,extension)
        source_keys=keys(source) if operation not in ('mpu','mpu_copy') else ['myobject']
        expected=10 if versioned else 5;event={'put':'REST.PUT.OBJECT','delete':'REST.DELETE.OBJECT','get':'REST.GET.OBJECT','head':'REST.HEAD.OBJECT','copy':'REST.PUT.OBJECT','multi_delete':'REST.POST.DELETE_MULTI_OBJECT','mpu':'REST.POST.UPLOAD','mpu_copy':'REST.PUT.OBJECT'}[operation]
        output_keys=source_keys;output_bucket=source.name;second=None
        if operation in ('get','delete','head'):
            for key in source_keys:
                if versioned:
                    response=source.call('head_version',key=key)
                    if operation=='get':source.call('get_version',key=key,version_id=response['version_id'],output_path=str(p.ctx.output))
                    elif operation=='delete':source.call('delete_version',key=key,version_id=response['version_id'])
                    else:source.call('head_version',key=key,version_id=response['version_id'])
                if operation=='get':source.call('get_http',key=key,output_path=str(p.ctx.output))
                elif operation=='delete':source.call('delete',key=key)
                elif not versioned:source.call('head_version',key=key)
        elif operation in ('copy','mpu_copy'):
            output_keys=['copy_of_'+key for key in source_keys]
            for key,out in zip(source_keys,output_keys):destination.call('copy',key=out,source_bucket=source.name,source_key=key)
            expected=5 if operation=='copy' else 1;output_bucket=destination.name
        elif operation=='multi_delete':
            if versioned:
                rows=source.call('versions_list')['versions'];source.call('delete_many_versions',objects=[{'key':row['key'],'version_id':row['version_id']} for row in rows])
            else:source.call('delete_many',objects=[{'key':key} for key in source_keys])
        elif operation=='mpu':expected=4 if versioned else 2;second=('REST.PUT.PART',12 if versioned else 6)
        consume(p,source,target,output_bucket,event,output_keys,expected,kind,second,operation=='put')

BASE=SCOPE_OPERATIONS+' put put_http list_v2_observed get_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy'
CASES=()
for operation,refs in (
 ('put',('test_bucket_logging_put_objects','test_bucket_logging_put_objects_versioned')),
 ('delete',('test_bucket_logging_delete_objects','test_bucket_logging_delete_objects_versioned')),
 ('get',('test_bucket_logging_get_objects','test_bucket_logging_get_objects_versioned')),
 ('head',('test_bucket_logging_head_objects','test_bucket_logging_head_objects_versioned')),
 ('mpu',('test_bucket_logging_mpu_s','test_bucket_logging_mpu_versioned_s')),
 ('mpu_copy',('test_bucket_logging_mpu_copy','test_bucket_logging_mpu_copy_versioned')),
 ('multi_delete',('test_bucket_logging_multi_delete','test_bucket_logging_multi_delete_versioned'))):
    for versioned,reference in enumerate(refs):
        operations=BASE+(' '+VERSION_SCOPE_OPERATIONS+' versioning_put versioning_get' if versioned else '')
        if operation in ('get','delete','head'):operations+=' head_version'+(' get_version delete_version' if versioned else '')
        if operation in ('mpu','mpu_copy'):operations+=' mp_create_metadata mp_part mp_complete_observed mp_abort_raw'
        if operation=='mpu_copy':operations+=' copy'
        if operation=='multi_delete':operations+=' delete_many_versions' if versioned else ' delete_many'
        CASES+=(case('ceph_logging_workload_'+operation+('_versions' if versioned else ''),'Ceph 日志负载 '+operation+(' 版本' if versioned else ''),('保留30MiB六个5MiB part与真实UploadId/ETag' if operation in ('mpu','mpu_copy') else '保留5个原始随机小对象')+'、真实版本ID、标准5.5秒flush与dummy、完整单日志/前缀/事件计数；显式扩展需真实SDK能力，finally停日志并清理实际版本',operations,lambda p,operation=operation,versioned=bool(versioned):workload(p,operation,versioned),reference,CEPH),)
for versioned,another,reference in ((False,False,'test_bucket_logging_copy_objects'),(True,False,'test_bucket_logging_copy_objects_versioned'),(False,True,'test_bucket_logging_copy_objects_bucket'),(True,True,'test_bucket_logging_copy_objects_bucket_versioned')):
    CASES+=(case('ceph_logging_workload_copy'+('_versions' if versioned else '')+('_bucket' if another else ''),'Ceph 日志复制负载','保留源五对象、源版本、同桶/跨桶copy_of_副本、两个原始日志策略statement、单日志和5条REST.PUT.OBJECT；两个桶停日志再清理',BASE+' copy'+(' '+VERSION_SCOPE_OPERATIONS+' versioning_put versioning_get' if versioned else ''),lambda p,versioned=versioned,another=another:workload(p,'copy',versioned,another),reference,CEPH),)

def register(registry):
    registry.add_suite(Suite('ceph_logging_workloads',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
