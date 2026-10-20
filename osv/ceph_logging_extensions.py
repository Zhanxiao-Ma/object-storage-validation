"""Retain complete Ceph-only journal/extended logging source paths behind real native SDK model/API prerequisites."""
from .bucket_scope import SCOPE_OPERATIONS
from .version_scope import VERSION_SCOPE_OPERATIONS
from .ceph_logging_controls import identity,document,set_policy
from .ceph_logging_records import flush,keys,read
from .ceph_logging_workloads import resources,verify
from .ceph_versioning import multipart
from .ceph_multipart import source_data,MIB
from .ceph_bucket_config import api
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

EXTENSION='ceph_bucket_logging_extensions'

def supported(p,post=False,formatting=False):
    """Require advertised native model fields and actual POST support before selecting vendor-only source paths; never encode unknown fields through an AWS model or silently remove them."""
    needed={EXTENSION}
    if post:needed.add('api.PostBucketLogging')
    if formatting:needed.add('logging_target_key_format')
    missing=needed-set(p.ctx.worker.capabilities)
    if missing:raise CaseNotRun('native SDK cannot represent complete Ceph logging extension source',*sorted(missing))

def journal(p,versioned):
    supported(p,post=True);identity(p)
    with resources(p,versioned) as (source,unused,target):
        set_policy(target,document(p,source,target))
        configuration={'LoggingEnabled':{'TargetBucket':target.name,'TargetPrefix':'log/','ObjectRollTime':5,'LoggingType':'Journal'}}
        assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus=configuration)['status_code'],200)
        payload=source_data(30*MIB)
        multipart(source,p,'myobject',payload)
        if versioned:multipart(source,p,'myobject',payload)
        flush(source);listed=keys(target);assert_equal(len(listed),1)
        key=listed[0];assert_equal(key.startswith('log/'),True)
        body=read(target,p,key)
        verify(body,source.name,'REST.POST.UPLOAD',['myobject','myobject'],2 if versioned else 1,'Journal')

def roundtrip(p):
    supported(p,formatting=True);identity(p)
    with resources(p,False) as (source,unused,target):
        set_policy(target,document(p,source,target))
        configuration={'TargetBucket':target.name,'TargetPrefix':'log/','LoggingType':'Standard','ObjectRollTime':5,'RecordsBatchSize':0}
        assert_equal(api(source,'PutBucketLogging',BucketLoggingStatus={'LoggingEnabled':configuration})['status_code'],200)
        result=api(source,'GetBucketLogging');assert_equal(result['status_code'],200)
        configuration['TargetObjectKeyFormat']={'SimplePrefix':{}}
        assert_equal(result['data']['LoggingEnabled'],configuration)

BASE=SCOPE_OPERATIONS+' '+EXTENSION+' api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy'
CASES=(case('ceph_logging_extensions_roundtrip','Ceph 扩展日志完整配置','保留LoggingType=Standard、ObjectRollTime=5、RecordsBatchSize=0及Get完整对象默认SimplePrefix；原生typed模型不支持时整源not_run，不丢字段',BASE+' api.GetBucketLogging logging_target_key_format',roundtrip,'test_put_bucket_logging_extensions',CEPH),)
for versioned,reference in ((False,'test_bucket_logging_mpu_j'),(True,'test_bucket_logging_mpu_versioned_j')):
    CASES+=(case('ceph_logging_extensions_mpu_j'+('_versions' if versioned else ''),'Ceph Journal分块日志'+('版本' if versioned else ''),'保留Journal/5秒、原30MiB六part、真实UploadId/ETag、真实POST日志flush、8字段与1/2条REST.POST.UPLOAD；缺完整原生模型/POST时not_run',BASE+' api.PostBucketLogging mp_create_metadata mp_part mp_complete_observed mp_abort_raw list_v2_observed get_http'+(' '+VERSION_SCOPE_OPERATIONS+' versioning_put versioning_get' if versioned else ''),lambda p,versioned=versioned:journal(p,versioned),reference,CEPH),)

def register(registry):
    registry.add_suite(Suite('ceph_logging_extensions',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
