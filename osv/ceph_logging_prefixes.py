"""Preserve five related source buckets, shared/separate log prefixes and the source's final-bucket-only single-prefix oracle."""
from contextlib import contextmanager,ExitStack
from copy import deepcopy
import re
import uuid
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_logging_controls import identity,document,set_policy
from .ceph_logging_records import put,flush,keys,read,records,parse
from .ceph_logging_workloads import extensions
from .ceph_bucket_config import api,raw
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

class RelatedBucketScope(BucketScope):
    """Preserve source sibling names base+0..4 while retaining fresh absence proof and owned cleanup for an internally generated UUID namespace."""
    def __init__(self,p,base,index):
        if not isinstance(base,str) or not re.fullmatch('osv-[0-9a-f]{32}',base) or type(index) is not int or not 0<=index<5:raise ValueError('related logging bucket requires fresh UUID base and source index 0..4')
        super().__init__(p,base);self.name=base+str(index)

@contextmanager
def scopes(p):
    """Stop every logger before deleting policies, source objects and target logs so asynchronous writes cannot race owned cleanup."""
    with ExitStack() as stack:
        target=stack.enter_context(BucketScope(p));base='osv-'+uuid.uuid4().hex
        sources=[stack.enter_context(RelatedBucketScope(p,base,index)) for index in range(5)];all_buckets=[target]+sources
        try:yield target,sources
        finally:
            failures=[]
            for bucket in all_buckets:
                result=raw(bucket,'PutBucketLogging',BucketLoggingStatus={})
                if not result.get('ok'):failures.append(result)
            for bucket in all_buckets:
                result=raw(bucket,'DeleteBucketPolicy')
                if not result.get('ok') and result.get('status_code')!=404:failures.append(result)
            if failures:raise RuntimeError('prefix logger cleanup failed: '+str(failures))

def matches(body,bucket,objects):
    """Return the original helper's boolean count result; do not silently assert each intermediate single-prefix bucket."""
    count=0
    for line in body.splitlines():
        parse(line)
        if bucket in line and 'REST.PUT.OBJECT' in line and any(key in line for key in objects):count+=1
    return count==5

def prefixes(p,single):
    identity(p);extension=extensions(p)
    with scopes(p) as (target,sources):
        policy={'Version':'2012-10-17','Statement':[]}
        for source in sources:
            statement=deepcopy(document(p,source,target)['Statement'][0]);statement['Resource']='arn:aws:s3:::'+target.name+'/'+('log/' if single else source.name+'/');policy['Statement'].append(statement)
        set_policy(target,policy)
        for source in sources:
            configuration={'LoggingEnabled':{'TargetBucket':target.name,'TargetPrefix':'log/' if single else source.name+'/'}}
            if extension:configuration['LoggingEnabled']['ObjectRollTime']=5
            assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus=configuration)['status_code'],200)
        for number,source in enumerate(sources,1):
            for index in range(5):put(source,p,'myobject'+(str(number) if single else '')+str(index))
        if single:flush(sources[-1])
        else:
            for source in sources:flush(source)
        listed=keys(target)
        if single:
            assert_equal(len(listed),1);body=read(target,p,listed[0]);found=False
            for source in sources:found=matches(body,source.name,keys(source))
            # Pinned source overwrites found on each iteration and asserts only
            # after the loop. Preserve its weaker final-source oracle as-is.
            assert_equal(found,True)
        else:
            assert_equal(len(listed)>=5,True)
            for key in listed:
                body=read(target,p,key);found=False
                for source in sources:
                    if key.startswith(source.name):
                        found=True;records(body,source.name,'REST.PUT.OBJECT',keys(source),5)
                assert_equal(found,True)

BASE=SCOPE_OPERATIONS+' put put_http list_v2_observed get_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy'
CASES=(case('ceph_logging_prefixes_multiple','Ceph 五源独立日志前缀','保留同一随机base+0..4源桶、五statement/prefix、25随机写、五次真实5.5秒flush、日志数>=5及每key所属源的5条记录；先停日志再清理',BASE,lambda p:prefixes(p,False),'test_bucket_logging_multiple_prefixes',CEPH),case('ceph_logging_prefixes_single','Ceph 五源共享日志前缀','保留五源log/共享prefix、25写、只flush最后源、目标单日志；保留源found循环覆盖仅assert最后桶的缺陷，不强化期望',BASE,lambda p:prefixes(p,True),'test_bucket_logging_single_prefix',CEPH))

def register(registry):
    registry.add_suite(Suite('ceph_logging_prefixes',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
