"""Translate standard Ceph bucket-logging configuration and permission source contracts."""
from contextlib import contextmanager,ExitStack
from copy import deepcopy
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw
from .ceph_acl import actor
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH

PREFIX="log/"

def identity(p):
    value=p.ctx.workload.get("identities",{}).get("main",{}).get("user_id")
    if not isinstance(value,str) or not value:raise CaseNotRun("Ceph logging policies require configured main user ID","workload.identities.main.user_id")
    return value

def logging_status(bucket,target=None,**extra):
    return {"LoggingEnabled":{"TargetBucket":target.name,"TargetPrefix":PREFIX,**extra}} if target is not None else {}

def set_policy(bucket,document):
    assert_equal(api(bucket,"PutBucketPolicy",Policy=json.dumps(document))["status_code"],204)

def document(p,source,target):
    return {"Version":"2012-10-17","Statement":[{"Sid":"S3ServerAccessLogsPolicy","Effect":"Allow","Principal":{"Service":"logging.s3.amazonaws.com"},"Action":["s3:PutObject"],"Resource":"arn:aws:s3:::"+target.name+"/"+PREFIX,"Condition":{"ArnLike":{"aws:SourceArn":"arn:aws:s3:::"+source.name},"StringEquals":{"aws:SourceAccount":identity(p)}}}]}

@contextmanager
def resources(p,count):
    """Disable logging on every owned bucket before deleting policies/objects so asynchronous log production cannot race destination cleanup."""
    with ExitStack() as stack:
        buckets=[stack.enter_context(BucketScope(p)) for _ in range(count)]
        try:yield buckets
        finally:
            failures=[]
            for bucket in buckets:
                result=raw(bucket,"PutBucketLogging",BucketLoggingStatus={})
                if not result.get("ok"):failures.append(result)
            for bucket in buckets:
                result=raw(bucket,"DeleteBucketPolicy")
                if not result.get("ok") and result.get("status_code")!=404:failures.append(result)
            if failures:raise RuntimeError("logging/policy cleanup failed: "+str(failures))

def rejected_code(response,code):
    """Retain source ClientError-code-only checks without adding an HTTP status restriction."""
    if response.get("ok") or not isinstance(response.get("status_code"),int) or not 400<=response["status_code"]<600 or response.get("error_code")!=code:raise AssertionError("expected source service ClientError "+code+": "+str(response))

def enabled(p,configuration):
    result=deepcopy(configuration)
    if "ceph_bucket_logging_extensions" in p.ctx.worker.capabilities:result["ObjectRollTime"]=5
    return result

def expected(p,configuration,default_format=False):
    result=deepcopy(configuration)
    if "ceph_bucket_logging_extensions" in p.ctx.worker.capabilities:result.update({"LoggingType":"Standard","RecordsBatchSize":0})
    if default_format:result["TargetObjectKeyFormat"]={"SimplePrefix":{}}
    return result

def roundtrip(p):
    """Keep all source configuration vectors and strict full-object equality, including source removal of TargetGrants and its always-true key-format probe defect."""
    metadata=actor(p)
    with resources(p,2) as (source,target):
        set_policy(target,document(p,source,target))
        configurations=[({"TargetBucket":target.name,"TargetPrefix":PREFIX},True),({"TargetBucket":target.name,"TargetPrefix":PREFIX,"TargetObjectKeyFormat":{"SimplePrefix":{}}},False),({"TargetBucket":target.name,"TargetPrefix":PREFIX,"TargetObjectKeyFormat":{"PartitionedPrefix":{"PartitionDateSource":"DeliveryTime"}}},False),({"TargetBucket":target.name,"TargetPrefix":PREFIX,"TargetGrants":[{"Grantee":{"DisplayName":metadata["display_name"],"ID":metadata["canonical_id"],"Type":"CanonicalUser"},"Permission":"FULL_CONTROL"}]},True)]
        # Pinned _has_target_object_key_format() returns True in finally even when
        # validation fails. Preserve that source branch; suite capability gating rejects
        # SDKs unable to represent the required model before executing the source.
        for configuration,default_format in configurations:
            request=enabled(p,configuration)
            assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus={"LoggingEnabled":request})["status_code"],200)
            result=api(source,"GetBucketLogging");assert_equal(result["status_code"],200)
            wanted=expected(p,request,default_format);wanted.pop("TargetGrants",None)
            assert_equal(result["data"]["LoggingEnabled"],wanted)

def remove(p):
    """Keep source logging enable/disable HTTP 200, final Get HTTP 200 and literal absence of LoggingEnabled rather than synthesizing an empty configuration."""
    identity(p)
    with resources(p,2) as (source,target):
        set_policy(target,document(p,source,target))
        assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,target))["status_code"],200)
        assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus={})["status_code"],200)
        result=api(source,"GetBucketLogging");assert_equal(result["status_code"],200);assert_equal("LoggingEnabled" in result["data"],False)

def owner(p):
    """Preserve source policy with no Resource element, genuine alternate-client enable/disable denial and owner's successful logging configuration."""
    identity(p)
    with resources(p,2) as (source,target):
        set_policy(target,document(p,source,target))
        set_policy(source,{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":["s3:PutBucketLogging"]}]})
        rejected_code(source.raw("api.PutBucketLogging",input={"BucketLoggingStatus":logging_status(source,target)},identity="alt"),"AccessDenied")
        assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,target))["status_code"],200)
        rejected_code(source.raw("api.PutBucketLogging",input={"BucketLoggingStatus":{}},identity="alt"),"AccessDenied")

def permissions(p):
    """Retain every service-principal/action/resource/source-ARN/account mutation and the source's Boto resource repr bug; never replace it with the bucket name."""
    ident=identity(p)
    with resources(p,2) as (source,target):
        def deny():rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,target)),"AccessDenied")
        deny();configured=document(p,source,target);item=configured["Statement"][0]
        item["Principal"]={"AWS":"*"};set_policy(target,configured);deny()
        item["Principal"]={"Service":"logging.s3.amazonaws.comkaboom"};set_policy(target,configured);deny()
        item["Principal"]={"Service":"logging.s3.amazonaws.com"};item["Action"]=["s3:GetObject"];set_policy(target,configured);deny()
        item["Action"]=["s3:PutObject"];item["Resource"]="arn:aws:s3:::"+target.name+"/kaboom";set_policy(target,configured);deny()
        item["Resource"]="arn:aws:s3:::"+target.name+"/"+PREFIX;item["Condition"]["ArnLike"]["aws:SourceArn"]="arn:aws:s3:::kaboom";set_policy(target,configured);deny()
        # Both assignments exist in the source; only the latter reaches its PUT.
        resource_repr="s3.Bucket(name="+repr(source.name)+")"
        item["Condition"]["ArnLike"]["aws:SourceArn"]="arn:aws:s3::kaboom:"+resource_repr
        item["Condition"]["ArnLike"]["aws:SourceArn"]="arn:aws:s3:::"+resource_repr
        item["Condition"]["StringEquals"]["aws:SourceAccount"]="kaboom";set_policy(target,configured);deny()
        item["Condition"]["StringEquals"]["aws:SourceAccount"]="kaboom$"+ident;set_policy(target,configured);deny()

def errors(p):
    """Keep source absent source/target, nested target, malformed partition enum, same-bucket, encrypted-target and requester-pays-target errors; extension-only invalid LoggingType runs only when independently advertised."""
    identity(p)
    with resources(p,4) as (source,first,second,third):
        absent_source=source.name+"kaboom";before=p.ctx.worker.call({"op":"bucket_head","bucket":absent_source})
        if (before.get("error_kind"),before.get("status_code"))!=("not_found",404):raise RuntimeError("cannot establish absent logging source for negative request")
        rejected_code(p.ctx.worker.call({"op":"api.PutBucketLogging","input":{"Bucket":absent_source,"BucketLoggingStatus":logging_status(source,first)}}),"NoSuchBucket")
        rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus={"LoggingEnabled":{"TargetBucket":first.name+"kaboom","TargetPrefix":PREFIX}}),"NoSuchKey")
        set_policy(first,document(p,second,first));assert_equal(api(second,"PutBucketLogging",BucketLoggingStatus=logging_status(second,first))["status_code"],200)
        set_policy(second,document(p,source,second));rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,second)),"InvalidArgument")
        set_policy(first,document(p,source,first));rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,first,TargetObjectKeyFormat={"PartitionedPrefix":{"PartitionDateSource":"kaboom"}})),"MalformedXML")
        set_policy(source,document(p,source,source));rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,source)),"InvalidArgument")
        api(first,"PutBucketEncryption",ServerSideEncryptionConfiguration={"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]})
        rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,first)),"InvalidArgument")
        set_policy(third,document(p,source,third));assert_equal(api(third,"PutBucketRequestPayment",RequestPaymentConfiguration={"Payer":"Requester"})["status_code"],200)
        rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,third)),"InvalidArgument")
        if "ceph_bucket_logging_extensions" in p.ctx.worker.capabilities:rejected_code(raw(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,first,LoggingType="kaboom")),"MalformedXML")

BASE=SCOPE_OPERATIONS+" api.GetBucketLogging api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy"
CASES=tuple(case("ceph_logging_controls_"+mode,"Ceph 日志配置/权限 "+mode,"保持原始配置、严格状态/错误码、所有策略变更和源码缺陷；停用所有源日志后清理目标桶",BASE+extras,execute,reference,CEPH) for mode,execute,reference,extras in (("roundtrip",roundtrip,"test_put_bucket_logging"," logging_target_key_format"),("errors",errors,"test_put_bucket_logging_errors"," api.PutBucketEncryption api.PutBucketRequestPayment logging_target_key_format"),("owner",owner,"test_bucket_logging_owner"," identity_alt"),("permissions",permissions,"test_put_bucket_logging_permissions",""),("remove",remove,"test_rm_bucket_logging","")))

def register(registry):
    registry.add_suite(Suite("ceph_logging_controls",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
