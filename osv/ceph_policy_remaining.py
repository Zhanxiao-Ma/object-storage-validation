"""Preserve remaining pinned Ceph tenant, multipart-policy and invalid-authentication scenarios."""
import json
import os
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .ceph_bucket_policy import document
from .ceph_policy_conditions import policy
from .ceph_multipart import source_data,MIB
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH
from .worker import Worker

class IdentityScope(BucketScope):
    """Create and clean a uniquely owned bucket through the source's tenant or alternate SDK client, preserving explicit identity overrides for cross-user requests."""
    def __init__(self,probe,identity):
        super().__init__(probe);self.identity=identity
    def raw(self,op,**options):
        options.setdefault("identity",self.identity)
        return super().raw(op,**options)

def tenant(p,tenanted_bucket):
    """Keep bare bucket ARN policy resources and source :bucket or tenant:bucket listing syntax without replacing its real tenant/global credentials."""
    name=p.ctx.workload.get("tenant_name")
    if tenanted_bucket and (not isinstance(name,str) or not name):raise CaseNotRun("Ceph source needs the configured tenant namespace for tenant:bucket listing","workload.tenant_name")
    with IdentityScope(p,"tenant") if tenanted_bucket else BucketScope(p) as bucket:
        bucket.put("asdf",b"asdf")
        configured=json.loads(document(bucket.name))
        with policy(bucket,*configured["Statement"]):
            result=require_ok(p.ctx.worker.call({"op":"list_v1_page","bucket":(name+":" if tenanted_bucket else ":")+bucket.name,"identity":"main" if tenanted_bucket else "tenant"}))
            assert_equal(len(result["objects"]),1)

def abort(bucket,key,ident):
    result=bucket.raw("mp_abort_raw",key=key,upload_id=ident)
    if not result.get("ok") and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):raise RuntimeError("policy multipart cleanup failed: "+str(result))

def copy_parts(p):
    """Keep three source key/body pairs, a public/* read policy, a genuinely alt-owned destination, two copied-object body assertions and the forbidden private source part copy."""
    with BucketScope(p) as source:
        for key in ("public/foo","public/bar","private/foo"):source.put(key,key.encode())
        configured={"Action":"s3:GetObject","Principal":{"AWS":"*"},"Effect":"Allow","Resource":"arn:aws:s3:::"+source.name+"/public/*"}
        with policy(source,configured):
            with IdentityScope(p,"alt") as destination:
                for key,target,denied in (("public/foo","new_foo",False),("public/bar","new_foo2",False),("private/foo","new_foo2",True)):
                    ident=destination.call("mp_create",key=target)["upload_id"]
                    try:
                        result=destination.raw("mp_copy",key=target,source_bucket=source.name,source_key=key,part_number=1,upload_id=ident)
                        if denied:rejected(result,403)
                        else:
                            etag=require_ok(result)["etag"]
                            destination.call("mp_complete_observed",key=target,upload_id=ident,parts=[{"part_number":1,"etag":etag}])
                            destination.call("get_http",key=target,output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),key.encode())
                    finally:abort(destination,target,ident)

def public_multipart(p):
    """Preserve source Principal=* and Action=* on both ARNs, the full fifty-MiB ten-part random ASCII workload and original completion HTTP 200 oracle."""
    with BucketScope(p) as bucket:
        configured={"Effect":"Allow","Principal":"*","Action":"*","Resource":["arn:aws:s3:::"+bucket.name,"arn:aws:s3:::"+bucket.name+"/*"]}
        with policy(bucket,configured):
            ident=bucket.call("mp_create",key="foo")["upload_id"]
            try:
                data=source_data(50*MIB);parts=[]
                for index,start in enumerate(range(0,len(data),5*MIB),1):
                    p.ctx.body.write_bytes(data[start:start+5*MIB])
                    etag=bucket.call("mp_part",key="foo",upload_id=ident,part_number=index,body_path=str(p.ctx.body))["etag"]
                    parts.append({"part_number":index,"etag":etag.strip(chr(34))})
                assert_equal(bucket.call("mp_complete_observed",key="foo",upload_id=ident,parts=parts)["status_code"],200)
            finally:abort(bucket,"foo",ident)

def bad_auth(p,bad_signature):
    """Use the original public badauth/roflmao credentials or actual main access key plus roflmao secret in an isolated real SDK client; require original ListBuckets HTTP 403."""
    target=p.ctx.worker.target.copy();environment=dict(target.get("env",{}))
    access=environment.get("OSV_ACCESS_KEY",os.environ.get("OSV_ACCESS_KEY")) if bad_signature else "badauth"
    if not isinstance(access,str) or not access:raise CaseNotRun("Ceph wrong-secret case requires the configured main access key","worker environment OSV_ACCESS_KEY")
    environment.update({"OSV_BAD_AUTH_ACCESS_KEY":access,"OSV_BAD_AUTH_SECRET_KEY":"roflmao","OSV_BAD_AUTH_SESSION_TOKEN":""})
    target["env"]=environment;worker=Worker(target,p.ctx.worker.timeout)
    try:rejected(worker.call({"op":"api.ListBuckets","input":{},"identity":"bad_auth"}),403)
    finally:worker.close()

CASES=tuple(case("ceph_remaining_policy_tenant_"+("owned" if owned else "global"),"Ceph 跨租户桶策略 "+("租户桶" if owned else "全局桶"),"原始 asdf、裸桶 ARN、:bucket/tenant:bucket 语法、真实租户/主身份和 Contents 长度1",SCOPE_OPERATIONS+" put api.PutBucketPolicy api.DeleteBucketPolicy list_v1_page identity_tenant",lambda p,owned=owned:tenant(p,owned),reference,CEPH) for owned,reference in ((False,"test_bucket_policy_different_tenant"),(True,"test_bucket_policy_tenanted_bucket")))
CASES+=(case("ceph_remaining_policy_copy_parts","Ceph 另用户桶的策略分块复制","保留 public/* 策略、真正由 alt 创建目标桶、两个读取正文和 private/foo UploadPartCopy 403，finally 清理真实ID",SCOPE_OPERATIONS+" put api.PutBucketPolicy api.DeleteBucketPolicy identity_alt mp_create mp_copy mp_complete_observed mp_abort_raw get_http",copy_parts,"test_bucket_policy_upload_part_copy",CEPH),case("ceph_remaining_policy_multipart_50mib","Ceph 公开策略50MiB分块上传","原始 Principal/Action *、桶/对象ARN、50MiB和十个5MiB分块、精确完成状态200",SCOPE_OPERATIONS+" api.PutBucketPolicy api.DeleteBucketPolicy mp_create mp_part mp_complete_observed mp_abort_raw",public_multipart,"test_multipart_upload_on_a_bucket_with_policy",CEPH))
CASES+=tuple(case("ceph_remaining_policy_bad_"+("signature" if signature else "access_key"),"Ceph 桶列举错误认证 "+("签名" if signature else "访问键"),"原始 badauth/roflmao 或真实主访问键+roflmao 独立SDK进程，保留403，不打印凭证", "api.ListBuckets",lambda p,signature=signature:bad_auth(p,signature),reference,CEPH) for signature,reference in ((False,"test_list_buckets_invalid_auth"),(True,"test_list_buckets_bad_auth")))

def register(registry):
    registry.add_suite(Suite("ceph_policy_remaining",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
