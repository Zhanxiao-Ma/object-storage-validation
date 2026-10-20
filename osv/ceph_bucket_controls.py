"""Translate literal Ceph bucket controls without creating retained objects."""
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_versioning import configure
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

PUBLIC={"BlockPublicAcls":True,"IgnorePublicAcls":True,"BlockPublicPolicy":True,"RestrictPublicBuckets":False}

def public_block(p,mode):
    """Use independent owned buckets and source boolean vectors; retain service errors and clean even policy failures."""
    with BucketScope(p) as bucket:
        if mode=="undefined":assert_equal(api(bucket,"DeletePublicAccessBlock")["status_code"],204)
        else:
            api(bucket,"PutPublicAccessBlock",PublicAccessBlockConfiguration=PUBLIC)
            observed=api(bucket,"GetPublicAccessBlock")["data"]["PublicAccessBlockConfiguration"]
            for key,value in PUBLIC.items():assert_equal(observed[key],value)
            if mode=="deny":
                policy={"Version":"2012-10-17","Statement":[{"Effect":"Deny","Principal":{"AWS":"*"},"Action":"s3:GetBucketPublicAccessBlock","Resource":"arn:aws:s3:::"+bucket.name}]}
                api(bucket,"PutBucketPolicy",Policy=json.dumps(policy))
                try:rejected(raw(bucket,"GetPublicAccessBlock"),403)
                finally:api(bucket,"DeleteBucketPolicy")
            elif mode=="delete":assert_equal(api(bucket,"DeletePublicAccessBlock")["status_code"],204)
        if mode in ("undefined","delete"):
            observed=raw(bucket,"GetPublicAccessBlock")
            assert_equal(observed["ok"],False);assert_equal(observed.get("error_code"),"NoSuchPublicAccessBlockConfiguration")
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def ownership(p,mode):
    """Preserve absent-ownership and enable/read/delete/idempotent-delete source oracles through typed SDK requests."""
    with BucketScope(p) as bucket:
        if mode=="delete":
            api(bucket,"PutBucketOwnershipControls",OwnershipControls={"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]})
            rules=api(bucket,"GetBucketOwnershipControls")["data"]["OwnershipControls"]["Rules"]
            assert_equal(len(rules),1);assert_equal(rules[0]["ObjectOwnership"],"BucketOwnerEnforced")
            api(bucket,"DeleteBucketOwnershipControls")
        rejected(raw(bucket,"GetBucketOwnershipControls"),404,"OwnershipControlsNotFoundError")
        if mode=="delete":api(bucket,"DeleteBucketOwnershipControls")
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def lock_config(mode="GOVERNANCE",**period):
    return {"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":mode,**(period or {"Days":1})}}}

def object_lock(p,mode):
    """Exercise only bucket configuration, never retained object writes; preserve exact invalid values, versioning requirements and service error codes."""
    ordinary=mode in ("invalid_bucket","enable_after","get_invalid_bucket")
    operation="bucket_create_raw" if ordinary else "bucket_create_locked"
    with BucketScope(p,create_operation=operation) as bucket:
        if mode=="put":
            for configuration in (lock_config(),lock_config("COMPLIANCE",Years=1)):
                assert_equal(api(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=configuration)["status_code"],200)
            assert_equal(bucket.call("versioning_get")["versioning_status"],"Enabled")
        elif mode=="invalid_bucket":rejected(raw(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=lock_config()),409,"InvalidBucketState")
        elif mode=="enable_after":
            rejected(raw(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=lock_config()),409,"InvalidBucketState")
            configure(bucket,"Suspended")
            rejected(raw(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=lock_config()),409,"InvalidBucketState")
            configure(bucket,"Enabled")
            assert_equal(api(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=lock_config())["status_code"],200)
        elif mode=="get":
            configuration=lock_config();api(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=configuration)
            assert_equal(api(bucket,"GetObjectLockConfiguration")["data"]["ObjectLockConfiguration"],configuration)
        elif mode=="get_invalid_bucket":rejected(raw(bucket,"GetObjectLockConfiguration"),404,"ObjectLockConfigurationNotFoundError")
        elif mode=="suspend":rejected(bucket.raw("versioning_put",status="Suspended"),409,"InvalidBucketState")
        else:
            vectors={"both":[(lock_config(Days=1,Years=1),"MalformedXML")],
                     "invalid_days":[(lock_config(Days=0),"InvalidRetentionPeriod")],
                     "invalid_years":[(lock_config(Years=-1),"InvalidRetentionPeriod")],
                     "invalid_mode":[(lock_config(value,Years=1),"MalformedXML") for value in ("abc","governance")],
                     "invalid_status":[({**lock_config(Years=1),"ObjectLockEnabled":"Disabled"},"MalformedXML")]}
            for configuration,code in vectors[mode]:rejected(raw(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration=configuration),400,code)
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

PUBLIC_VECTORS=(("undefined","test_get_undefined_public_block"),("deny","test_get_public_block_deny_bucket_policy"),("put","test_put_public_block"),("delete","test_put_get_delete_public_block"))
OWNERSHIP_VECTORS=(("undefined","test_create_bucket_no_ownership_controls"),("delete","test_bucket_create_delete_bucket_ownership"))
LOCK_VECTORS=(("put","test_object_lock_put_obj_lock"),("invalid_bucket","test_object_lock_put_obj_lock_invalid_bucket"),("enable_after","test_object_lock_put_obj_lock_enable_after_create"),("both","test_object_lock_put_obj_lock_with_days_and_years"),("invalid_days","test_object_lock_put_obj_lock_invalid_days"),("invalid_years","test_object_lock_put_obj_lock_invalid_years"),("invalid_mode","test_object_lock_put_obj_lock_invalid_mode"),("invalid_status","test_object_lock_put_obj_lock_invalid_status"),("suspend","test_object_lock_suspend_versioning"),("get","test_object_lock_get_obj_lock"),("get_invalid_bucket","test_object_lock_get_obj_lock_invalid_bucket"))
CASES=tuple(case("ceph_public_block_"+mode,"Ceph 公共访问阻止 "+mode,"保留四个原始布尔值、删除状态及缺失/拒绝错误",SCOPE_OPERATIONS+" api.GetPublicAccessBlock api.PutPublicAccessBlock api.DeletePublicAccessBlock"+(" api.PutBucketPolicy api.DeleteBucketPolicy" if mode=="deny" else ""),lambda p,mode=mode:public_block(p,mode),reference,CEPH) for mode,reference in PUBLIC_VECTORS)
CASES+=tuple(case("ceph_ownership_"+mode,"Ceph 桶所有权控制 "+mode,"保留未配置 404、设置读回、删除及幂等删除",SCOPE_OPERATIONS+" api.GetBucketOwnershipControls"+(" api.PutBucketOwnershipControls api.DeleteBucketOwnershipControls" if mode=="delete" else ""),lambda p,mode=mode:ownership(p,mode),reference,CEPH) for mode,reference in OWNERSHIP_VECTORS)
CASES+=tuple(case("ceph_lock_config_"+mode,"Ceph Object Lock 桶配置 "+mode,"保留原始保留周期、非法枚举、版本控制和服务端错误断言；独立桶不写入保留对象",SCOPE_OPERATIONS+" api.PutObjectLockConfiguration api.GetObjectLockConfiguration versioning_get versioning_put"+(" bucket_create_locked" if mode not in ("invalid_bucket","enable_after","get_invalid_bucket") else ""),lambda p,mode=mode:object_lock(p,mode),reference,CEPH) for mode,reference in LOCK_VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_bucket_controls",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
