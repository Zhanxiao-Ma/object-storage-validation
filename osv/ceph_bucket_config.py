"""Translate concrete bucket tagging, CORS, lifecycle and default-encryption contracts through native SDK models."""
from datetime import datetime,timezone
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal,SDKOperationError
from .suites import case,run_cases,CaseNotRun,CEPH

def raw(bucket,name,**parameters):
    return bucket.raw("api."+name,input=parameters)

def api(bucket,name,**parameters):
    result=raw(bucket,name,**parameters)
    if not result["ok"]:raise SDKOperationError(result)
    return result

def rejected(result,status,code=None):
    if result["ok"] or result.get("status_code")!=status or (code is not None and result.get("error_code")!=code):
        error=AssertionError("expected service rejection "+str((status,code))+"; actual="+json.dumps(result,ensure_ascii=False))
        if not result["ok"]:
            observed=SDKOperationError(result);error.evidence=observed.evidence;error.failure_category=observed.failure_category
        raise error

def tags(p):
    with BucketScope(p) as bucket:
        rejected(raw(bucket,"GetBucketTagging"),404,"NoSuchTagSet")
        api(bucket,"PutBucketTagging",Tagging={"TagSet":[{"Key":"Hello","Value":"World"}]})
        actual=api(bucket,"GetBucketTagging")["data"]["TagSet"]
        assert_equal(len(actual),1);assert_equal(actual[0]["Key"],"Hello");assert_equal(actual[0]["Value"],"World")
        assert_equal(api(bucket,"DeleteBucketTagging")["status_code"],204)
        rejected(raw(bucket,"GetBucketTagging"),404,"NoSuchTagSet")

def cors(p):
    with BucketScope(p) as bucket:
        rejected(raw(bucket,"GetBucketCors"),404)
        api(bucket,"PutBucketCors",CORSConfiguration={"CORSRules":[{"AllowedMethods":["GET","PUT"],"AllowedOrigins":["*.get","*.put"]}]})
        actual=api(bucket,"GetBucketCors")["data"]["CORSRules"][0]
        assert_equal(actual["AllowedMethods"],["GET","PUT"]);assert_equal(actual["AllowedOrigins"],["*.get","*.put"])
        api(bucket,"DeleteBucketCors");rejected(raw(bucket,"GetBucketCors"),404)

def rules_get():
    return [{"ID":"test1/","Expiration":{"Days":31},"Prefix":"test1/","Status":"Enabled"},
            {"ID":"test2/","Expiration":{"Days":120},"Prefix":"test2/","Status":"Enabled"}]

def lifecycle(p,mode):
    classes=p.ctx.workload.get("storage_classes",["STANDARD"])
    if mode=="noncurrent_transition" and len(classes)<3:
        raise CaseNotRun("Ceph source requires three configured storage classes","workload.storage_classes: at least 3")
    with BucketScope(p) as bucket:
        if mode=="delete":
            rejected(raw(bucket,"GetBucketLifecycleConfiguration"),404,"NoSuchLifecycleConfiguration")
            assert_equal(api(bucket,"DeleteBucketLifecycle")["status_code"],204)
            api(bucket,"PutBucketLifecycleConfiguration",LifecycleConfiguration={"Rules":rules_get()})
            api(bucket,"GetBucketLifecycleConfiguration")
            assert_equal(api(bucket,"DeleteBucketLifecycle")["status_code"],204)
            rejected(raw(bucket,"GetBucketLifecycleConfiguration"),404,"NoSuchLifecycleConfiguration")
            assert_equal(api(bucket,"DeleteBucketLifecycle")["status_code"],204);return
        if mode=="invalid_status":
            for status in ("enabled","disabled","invalid"):
                rules=[{"ID":"rule1","Expiration":{"Days":2},"Prefix":"test1/","Status":status}]
                # The source's second call uses its legacy Boto alias. Both aliases
                # use the same S3 lifecycle URI/body; native SDK models bind this call.
                rejected(raw(bucket,"PutBucketLifecycleConfiguration",LifecycleConfiguration={"Rules":rules}),400,"MalformedXML")
            return
        if mode in ("get","no_id"):rules=rules_get()
        elif mode=="set":rules=[{"ID":"rule1","Expiration":{"Days":1},"Prefix":"test1/","Status":"Enabled"},{"ID":"rule2","Expiration":{"Days":2},"Prefix":"test2/","Status":"Disabled"}]
        elif mode=="id_long":rules=[{"ID":"a"*256,"Expiration":{"Days":2},"Prefix":"test1/","Status":"Enabled"}]
        elif mode=="same_id":rules=[{"ID":"rule1","Expiration":{"Days":1},"Prefix":"test1/","Status":"Enabled"},{"ID":"rule1","Expiration":{"Days":2},"Prefix":"test2/","Status":"Enabled"}]
        elif mode=="date":rules=[{"ID":"rule1","Expiration":{"Date":int(datetime(2017,9,27,tzinfo=timezone.utc).timestamp()*1000)},"Prefix":"test1/","Status":"Enabled"}]
        elif mode=="invalid_date":rules=[{"ID":"rule1","Expiration":{"Date":"20200101"},"Prefix":"test1/","Status":"Enabled"}]
        elif mode=="transition_invalid_date":rules=[{"ID":"rule1","Expiration":{"Date":int(datetime(2023,9,27,tzinfo=timezone.utc).timestamp()*1000)},"Transitions":[{"Date":"20220927","StorageClass":"GLACIER"}],"Prefix":"test1/","Status":"Enabled"}]
        elif mode=="noncurrent":
            for key in ("past/foo","future/bar"):bucket.put(key,key.encode())
            rules=[{"ID":"rule1","NoncurrentVersionExpiration":{"NoncurrentDays":2},"Prefix":"past/","Status":"Enabled"},{"ID":"rule2","NoncurrentVersionExpiration":{"NoncurrentDays":3},"Prefix":"future/","Status":"Enabled"}]
        elif mode=="marker":rules=[{"ID":"rule1","Expiration":{"ExpiredObjectDeleteMarker":True},"Prefix":"test1/","Status":"Enabled"}]
        elif mode in ("filter","empty_filter"):rules=[{"ID":"rule1","Expiration":{"ExpiredObjectDeleteMarker":True},"Filter":{"Prefix":"foo"} if mode=="filter" else {},"Status":"Enabled"}]
        elif mode=="multipart":rules=[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","AbortIncompleteMultipartUpload":{"DaysAfterInitiation":2}},{"ID":"rule2","Prefix":"test2/","Status":"Disabled","AbortIncompleteMultipartUpload":{"DaysAfterInitiation":3}}]
        elif mode=="noncurrent_transition":rules=[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","NoncurrentVersionTransitions":[{"NoncurrentDays":2,"StorageClass":classes[1]},{"NoncurrentDays":4,"StorageClass":classes[2]}],"NoncurrentVersionExpiration":{"NoncurrentDays":6}},{"ID":"rule2","Prefix":"test2/","Status":"Disabled","NoncurrentVersionExpiration":{"NoncurrentDays":3}}]
        else:raise ValueError(mode)
        if mode=="no_id":
            for rule in rules:rule.pop("ID")
        result=raw(bucket,"PutBucketLifecycleConfiguration",LifecycleConfiguration={"Rules":rules})
        if mode in ("id_long","same_id"):return rejected(result,400,"InvalidArgument")
        if mode in ("invalid_date","transition_invalid_date"):return rejected(result,400)
        if not result["ok"]:raise SDKOperationError(result)
        if mode in ("get","no_id"):
            actual=api(bucket,"GetBucketLifecycleConfiguration")["data"]["Rules"]
            if mode=="get":assert_equal(actual,rules)
            else:
                for rule in actual:
                    prefix=rule["Prefix"]
                    if prefix not in ("test1/","test2/"):raise AssertionError("rules not right")
                    assert_equal(rule["Expiration"]["Days"],31 if prefix=="test1/" else 120)
                    assert_equal(rule["Status"],"Enabled");assert_equal("ID" in rule,True)
        else:assert_equal(result["status_code"],200)

def encryption(p,mode,kms):
    key=p.ctx.workload.get("kms_key_id")
    if key is None:key="fool-me-again"
    rule={"SSEAlgorithm":"aws:kms" if kms else "AES256"}
    if kms:rule["KMSMasterKeyID"]=key
    with BucketScope(p) as bucket:
        if mode=="get":
            result=raw(bucket,"GetBucketEncryption")
            assert_equal("" if result["ok"] else result.get("error_code"),"ServerSideEncryptionConfigurationNotFoundError")
        if mode=="delete":assert_equal(api(bucket,"DeleteBucketEncryption")["status_code"],204)
        result=api(bucket,"PutBucketEncryption",ServerSideEncryptionConfiguration={"Rules":[{"ApplyServerSideEncryptionByDefault":rule}]})
        assert_equal(result["status_code"],200)
        if mode=="get":
            result=api(bucket,"GetBucketEncryption");assert_equal(result["status_code"],200)
            actual=result["data"]["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
            assert_equal(actual["SSEAlgorithm"],"aws:kms" if kms else "AES256")
            if kms:assert_equal(actual["KMSMasterKeyID"],key)
        if mode=="delete":
            assert_equal(api(bucket,"DeleteBucketEncryption")["status_code"],204)
            result=raw(bucket,"GetBucketEncryption")
            assert_equal("" if result["ok"] else result.get("error_code"),"ServerSideEncryptionConfigurationNotFoundError")

LIFECYCLE=(
 ("set","test_lifecycle_set"),("get","test_lifecycle_get"),("delete","test_lifecycle_delete"),("no_id","test_lifecycle_get_no_id"),
 ("id_long","test_lifecycle_id_too_long"),("same_id","test_lifecycle_same_id"),("invalid_status","test_lifecycle_invalid_status"),
 ("date","test_lifecycle_set_date"),("invalid_date","test_lifecycle_set_invalid_date"),("transition_invalid_date","test_lifecycle_transition_set_invalid_date"),
 ("noncurrent","test_lifecycle_set_noncurrent"),("marker","test_lifecycle_set_deletemarker"),("filter","test_lifecycle_set_filter"),
 ("empty_filter","test_lifecycle_set_empty_filter"),("multipart","test_lifecycle_set_multipart"),("noncurrent_transition","test_lifecycle_set_noncurrent_transition"))

CASES=(case("ceph_config_tags","Ceph 桶标签完整生命周期","保留 NoSuchTagSet、实际标签和删除 204",SCOPE_OPERATIONS+" api.GetBucketTagging api.PutBucketTagging api.DeleteBucketTagging",tags,"test_set_bucket_tagging",CEPH),
       case("ceph_config_cors","Ceph CORS 配置读写删除","原始方法/来源数组一致，创建前和删除后实际 404",SCOPE_OPERATIONS+" api.GetBucketCors api.PutBucketCors api.DeleteBucketCors",cors,"test_set_cors",CEPH))
for mode,reference in LIFECYCLE:
    extras=" api.GetBucketLifecycleConfiguration" if mode in ("get","no_id","delete") else ""
    if mode not in ("filter","empty_filter"):extras+=" lifecycle_legacy_prefix"
    if mode=="delete":extras+=" api.DeleteBucketLifecycle"
    if mode=="noncurrent":extras+=" put"
    CASES+=(case("ceph_config_lifecycle_"+mode,"Ceph 生命周期配置 "+mode,"保持原始配置、错误码、生成 ID、过滤条件和存储类型前提；无法表达的输入明确报告客户端错误",SCOPE_OPERATIONS+" api.PutBucketLifecycleConfiguration"+extras,lambda p,mode=mode:lifecycle(p,mode),reference,CEPH),)
for kms in (False,True):
    for mode in ("put","get","delete"):
        operations=SCOPE_OPERATIONS+" api.PutBucketEncryption"+(" api.GetBucketEncryption" if mode in ("get","delete") else "")+(" api.DeleteBucketEncryption" if mode=="delete" else "")
        name="kms" if kms else "s3"
        CASES+=(case("ceph_config_encryption_"+name+"_"+mode,"Ceph 桶默认加密 "+name+" "+mode,"保留源算法、KMS ID、原始无配置错误和删除状态预期",operations,lambda p,mode=mode,kms=kms:encryption(p,mode,kms),"test_"+mode+"_bucket_encryption_"+name,CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_bucket_config",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
