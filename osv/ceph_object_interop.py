"""Translate related Ceph object authorization, tagging, encryption conflict and cross-bucket vectors through existing SDK operations."""
import json
import random
import string
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_bucket_policy import document
from .ceph_tags import simple,check_tags
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH

def put_identity(p,mode):
    """Keep source empty foo initialization, creation-time public-read-write ACL, literal foo body, and anonymous denial or actual HTTP 200."""
    options={"create_operation":"bucket_create_acl","create_options":{"acl":"public-read-write"}} if mode=="anonymous_public" else {}
    with BucketScope(p,**options) as bucket:
        if mode!="authenticated":bucket.put("foo",b"")
        p.ctx.body.write_bytes(b"foo")
        result=bucket.raw("put_http",key="foo",body_path=str(p.ctx.body),**({"identity":"anonymous"} if mode!="authenticated" else {}))
        if mode=="anonymous_private":rejected(result,403,"AccessDenied")
        else:assert_equal(require_ok(result)["status_code"],200)

def conflict(p,mode):
    """Send the exact public source SSE-C bytes/MD5, AES256/aws:kms/aes:kms strings and fallback key on an empty object; require original 400/InvalidArgument."""
    with BucketScope(p) as bucket:
        headers={"x-amz-server-side-encryption":"aws:kms" if mode=="c_kms" else "aes:kms" if mode=="bad_enc_kms" else "AES256"}
        if mode in ("c_s3","c_kms"):
            headers.update({"x-amz-server-side-encryption-customer-algorithm":"AES256","x-amz-server-side-encryption-customer-key":"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=","x-amz-server-side-encryption-customer-key-md5":"DWygnHRtgiJ77HCm+1rvHw=="})
        if mode in ("c_kms","s3_kms"):headers["x-amz-server-side-encryption-aws-kms-key-id"]=p.ctx.workload.get("kms_key_id") or "fool-me-once"
        p.ctx.body.write_bytes(b"")
        rejected(bucket.raw("put_http",key="testobj",body_path=str(p.ctx.body),wire_headers=headers),400,"InvalidArgument")

def tags_on_upload(p):
    """Keep source foo=bar&bar header, original A*100 content and complete unordered bar-empty and foo-bar tags."""
    with BucketScope(p) as bucket:
        key="testtagobj1";p.ctx.body.write_bytes(b"A"*100)
        bucket.call("put_http",key=key,body_path=str(p.ctx.body),wire_headers={"x-amz-tagging":"foo=bar&bar"})
        bucket.call("get_http",key=key,output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),b"A"*100)
        check_tags(bucket.call("tags_get",key=key)["tags"],[{"key":"bar","value":""},{"key":"foo","value":"bar"}])

def tag_policy(p,mode):
    """Keep original seven-MiB repeated ASCII object, ten numeric tags and per-operation public policy; use a genuine alternate SDK identity for the authorized action."""
    with BucketScope(p) as bucket:
        key="testputtagsacl";rng=random.Random(6033)
        chunk=''.join(rng.choice(string.ascii_letters) for _ in range(1024)).encode()
        bucket.put(key,chunk*(7*1024))
        action={"get":"GetObjectTagging","put":"PutObjectTagging","delete":"DeleteObjectTagging"}[mode]
        policy=json.dumps({"Version":"2012-10-17","Statement":[{"Action":"s3:"+action,"Principal":{"AWS":"*"},"Effect":"Allow","Resource":"arn:aws:s3:::"+bucket.name+"/"+key}]})
        try:
            api(bucket,"PutBucketPolicy",Policy=policy)
            tags=simple(10)
            assert_equal(bucket.call("tags_put",key=key,tags=tags,**({"identity":"alt"} if mode=="put" else {}))["status_code"],200)
            if mode=="delete":assert_equal(bucket.call("tags_delete",key=key,identity="alt")["status_code"],204)
            observed=bucket.call("tags_get",key=key,**({"identity":"alt"} if mode=="get" else {}))["tags"]
            if mode=="delete":assert_equal(len(observed),0)
            else:check_tags(observed,tags)
        finally:
            result=raw(bucket,"DeleteBucketPolicy")
            if not result["ok"] and result.get("status_code")!=404:raise RuntimeError("policy cleanup failed: "+str(result))

def policy_roundtrip(p):
    """Preserve byte-exact policy GET comparison and the source's conditional NoSuchBucketPolicy assertion after deletion; do not strengthen its missing-exception oracle yet."""
    with BucketScope(p) as bucket:
        bucket.put("asdf",b"asdf");policy=document(bucket.name)
        try:
            api(bucket,"PutBucketPolicy",Policy=policy)
            assert_equal(api(bucket,"GetBucketPolicy")["data"]["Policy"],policy)
            api(bucket,"DeleteBucketPolicy")
            result=raw(bucket,"GetBucketPolicy")
            if not result["ok"]:assert_equal(result.get("error_code"),"NoSuchBucketPolicy")
        finally:
            result=raw(bucket,"DeleteBucketPolicy")
            if not result["ok"] and result.get("status_code")!=404:raise RuntimeError("policy cleanup failed: "+str(result))

def cross_copy(p,missing):
    """Keep source foo123bar/bar321foo, literal foo, two buckets or the absent -fake source bucket, with actual source HEAD and SDK CopyObject."""
    with BucketScope(p) as destination:
        if missing:
            source=destination.generated_name+"-fake"
            before=p.ctx.worker.call({"op":"bucket_head","bucket":source})
            if (before.get("error_kind"),before.get("status_code"))!=("not_found",404):raise RuntimeError("cannot establish absent copy source bucket")
            # The source client.copy is a managed transfer: an absent source fails
            # its HeadObject preflight, before any CopyObject is transmitted.
            rejected(p.ctx.worker.call({"op":"head_http","bucket":source,"key":"foo123bar"}),404)
        else:
            with BucketScope(p) as source:
                source.put("foo123bar",b"foo");source.call("head_http",key="foo123bar")
                destination.call("copy",key="bar321foo",source_bucket=source.name,source_key="foo123bar")
                destination.call("get_http",key="bar321foo",output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),b"foo")

CASES=tuple(case("ceph_interop_put_"+mode,"Ceph 写入身份 "+mode,"保留原始空对象、创建 ACL、匿名/认证身份与精确状态",SCOPE_OPERATIONS+" put put_http"+(" identity_anonymous" if mode!="authenticated" else "")+(" bucket_create_acl" if mode=="anonymous_public" else ""),lambda p,mode=mode:put_identity(p,mode),reference,CEPH) for mode,reference in (("anonymous_private","test_object_anon_put"),("anonymous_public","test_object_anon_put_write_access"),("authenticated","test_object_put_authenticated")))
CASES+=tuple(case("ceph_interop_conflict_"+mode,"Ceph 加密声明冲突 "+mode,"原始公开测试密钥/MD5、算法组合与空对象，精确 400/InvalidArgument；缺 KMS 配置按源 fool-me-once",SCOPE_OPERATIONS+" put_http wire_headers",lambda p,mode=mode:conflict(p,mode),"test_put_obj_enc_conflict_"+mode,CEPH) for mode in ("c_s3","c_kms","s3_kms","bad_enc_kms"))
CASES+=(case("ceph_interop_upload_tags","Ceph 上传时标签头","原始 foo=bar&bar、A*100 和完整标签集合",SCOPE_OPERATIONS+" put_http get_http tags_get wire_headers",tags_on_upload,"test_put_obj_with_tags",CEPH),)
CASES+=tuple(case("ceph_interop_tag_policy_"+mode,"Ceph 公开标签策略 "+mode,"另一真实身份执行仅授权的标签操作，十个原始标签及实际状态/完整列表",SCOPE_OPERATIONS+" put identity_alt tags_put tags_get api.PutBucketPolicy api.DeleteBucketPolicy"+(" tags_delete" if mode=="delete" else ""),lambda p,mode=mode:tag_policy(p,mode),reference,CEPH) for mode,reference in (("get","test_get_tags_acl_public"),("put","test_put_tags_acl_public"),("delete","test_delete_tags_obj_public")))
CASES+=(case("ceph_interop_policy_roundtrip","Ceph 策略精确往返","实际策略字节一致，保留源删除后有异常时才检查错误码的规则",SCOPE_OPERATIONS+" put api.PutBucketPolicy api.GetBucketPolicy api.DeleteBucketPolicy",policy_roundtrip,"test_set_get_del_bucket_policy",CEPH),)
CASES+=tuple(case("ceph_interop_copy_"+("missing_bucket" if missing else "cross_bucket"),"Ceph 跨桶复制"+(" 缺源桶" if missing else " 正常"),"保留托管复制的源 HEAD 预检；正常路径实际 SDK CopyObject 与 foo 完整读取，缺源路径预检 HTTP 404",SCOPE_OPERATIONS+" head_http"+(" put copy get_http" if not missing else ""),lambda p,missing=missing:cross_copy(p,missing),reference,CEPH) for missing,reference in ((False,"test_object_copy_diff_bucket"),(True,"test_object_copy_bucket_not_found")))

def register(registry):
    registry.add_suite(Suite("ceph_object_interop",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
