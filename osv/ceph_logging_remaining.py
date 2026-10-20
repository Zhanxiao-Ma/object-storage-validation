"""Additional complete standard-SDK logging sources; unsupported extension groups remain explicit future work."""
from . import ceph_logging_remaining_sources as sources
from .ceph_logging_remaining_runtime import Runtime
from .scenarios import Suite
from .suites import case,run_cases,CEPH


def execute(p,mode):
    with Runtime(p,mode) as actual:
        if mode=='account':sources.test_put_bucket_logging_account_s()
        elif mode=='requester':sources.test_bucket_logging_requester_assumed_role()
        else:raise ValueError('unknown logging source mode')
    return {'request_transport':'official_sdk','actual_model_rejections':actual.actual_extension_rejections,'fallback':'original rolltime wait and actual dummy PUT when official SDK rejects LoggingType'}


BASE='bucket_head bucket_create_raw bucket_delete list delete put_http list_v2_observed get_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy'
CASES=(case('ceph_logging_remaining_account_s','Ceph标准跨IAM账户日志','完整三个main→root/root→root/root→main向量、两次实际GetUser ARN生成SourceAccount、每向量原5次PUT/5.5秒wait/dummy与完整日志记录断言',BASE+' identity_iam_root api.iam.GetUser',lambda p:execute(p,'account'),'test_put_bucket_logging_account_s',CEPH),case('ceph_logging_remaining_assumed_requester','Ceph实际STS日志Requester','原IAM trust/inline政策、实际alt STS AssumeRole返回凭据→官方S3两桶、原5.5秒fallback、实际日志Requester STS ARN原substring断言；先清理暂时身份桶再撤销Role权限',BASE+' identity_iam identity_alt api.iam.GetRole api.iam.CreateRole api.iam.PutRolePolicy api.iam.DeleteRolePolicy api.iam.DeleteRole api.sts.AssumeRole',lambda p:execute(p,'requester'),'test_bucket_logging_requester_assumed_role',CEPH))


def register(registry):registry.add_suite(Suite('ceph_logging_remaining',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
