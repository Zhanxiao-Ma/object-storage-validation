"""Preserve pinned Ceph public-access and ownership contracts through genuine SDK requests."""
from contextlib import contextmanager
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_acl import actor
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH

@contextmanager
def cleanup_policy(bucket,document=None):
    """Delete only this owned bucket's policy, including unexpected policy acceptance or an SDK failure after server-side application."""
    try:
        if document is not None:api(bucket,"PutBucketPolicy",Policy=json.dumps(document))
        yield
    finally:
        result=raw(bucket,"DeleteBucketPolicy")
        if not result.get("ok") and result.get("status_code")!=404:raise RuntimeError("policy cleanup failed: "+str(result))

def document(bucket,action="s3:ListBucket",principal=None,condition=None,objects=False):
    statement={"Effect":"Allow","Principal":{"AWS":"*"} if principal is None else principal,"Action":action,"Resource":"arn:aws:s3:::"+bucket.name+"/*" if objects else ["arn:aws:s3:::"+bucket.name,"arn:aws:s3:::"+bucket.name+"/*"]}
    if condition is not None:statement["Condition"]=condition
    return {"Version":"2012-10-17","Statement":[statement]}

def policy_status(p,mode):
    """Keep source IsPublic expectations including ACL-derived public status, literal tenant principal and source-IP condition; no provider-specific oracle reconciliation."""
    with BucketScope(p) as bucket:
        if mode in ("public_acl","authenticated_acl"):api(bucket,"PutBucketAcl",ACL="public-read" if mode=="public_acl" else "authenticated-read")
        if mode in ("empty","public_acl","authenticated_acl"):
            assert_equal(api(bucket,"GetBucketPolicyStatus")["data"]["PolicyStatus"]["IsPublic"],mode!="empty");return
        if mode!="principal":assert_equal(api(bucket,"GetBucketPolicyStatus")["data"]["PolicyStatus"]["IsPublic"],False)
        configured=document(bucket,principal={"AWS":"arn:aws:iam::s3tenant1:root"} if mode=="principal" else None,condition={"IpAddress":{"aws:SourceIp":"10.0.0.0/32"}} if mode=="source_ip" else None)
        with cleanup_policy(bucket,configured):assert_equal(api(bucket,"GetBucketPolicyStatus")["data"]["PolicyStatus"]["IsPublic"],mode=="public_policy")

def access_configuration(**enabled):
    return {name:enabled.get(name,False) for name in ("BlockPublicAcls","IgnorePublicAcls","BlockPublicPolicy","RestrictPublicBuckets")}

def put_empty(bucket,key,acl=None,identity=None):
    bucket.probe.ctx.body.write_bytes(b"")
    options={"key":key,"body_path":str(bucket.probe.ctx.body)}
    if acl is not None:options["wire_headers"]={"x-amz-acl":acl}
    if identity is not None:options["identity"]=identity
    return bucket.raw("put_http",**options)

def public_block(p,mode):
    """Keep original boolean vectors, exact status/body oracles and true anonymous/alternate SDK identities without changing source ACL prerequisites."""
    with BucketScope(p) as bucket:
        if mode=="restrict":
            assert_equal(api(bucket,"DeletePublicAccessBlock")["status_code"],204)
            bucket.probe.ctx.body.write_bytes(b"bar");assert_equal(bucket.call("put_http",key="foo",body_path=str(p.ctx.body))["status_code"],200)
            with cleanup_policy(bucket):
                assert_equal(api(bucket,"PutBucketPolicy",Policy=json.dumps(document(bucket,"s3:GetObject",objects=True)))["status_code"],204)
                bucket.call("get_http",key="foo",identity="anonymous",output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),b"bar")
                assert_equal(api(bucket,"PutPublicAccessBlock",PublicAccessBlockConfiguration=access_configuration(RestrictPublicBuckets=True))["status_code"],200)
                rejected(bucket.raw("get_http",key="foo",identity="anonymous",output_path=str(p.ctx.output)),403)
                bucket.call("get_http",key="foo",output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),b"bar")
            return
        if mode=="ignore_acl":
            api(bucket,"PutBucketAcl",ACL="public-read");bucket.call("list_v1_page",identity="alt")
            p.ctx.body.write_bytes(b"abcde");bucket.call("put_http",key="key1",body_path=str(p.ctx.body),wire_headers={"x-amz-acl":"public-read"})
            bucket.call("get_http",key="key1",identity="alt",output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),b"abcde")
            api(bucket,"PutPublicAccessBlock",PublicAccessBlockConfiguration=access_configuration(IgnorePublicAcls=True))
            api(bucket,"PutBucketAcl",ACL="public-read")
            rejected(bucket.raw("list_v1_page",identity="alt"),403)
            rejected(bucket.raw("get_http",key="key1",identity="alt",output_path=str(p.ctx.output)),403);return
        configuration=access_configuration(BlockPublicAcls=mode in ("bucket_acl","object_acl"),BlockPublicPolicy=mode in ("bucket_acl","policy","principal"))
        api(bucket,"PutPublicAccessBlock",PublicAccessBlockConfiguration=configuration)
        if mode=="bucket_acl":
            observed=api(bucket,"GetPublicAccessBlock")["data"]["PublicAccessBlockConfiguration"]
            for field in ("BlockPublicAcls","BlockPublicPolicy"):assert_equal(observed[field],configuration[field])
            for acl in ("public-read","public-read-write","authenticated-read"):rejected(raw(bucket,"PutBucketAcl",ACL=acl),403)
        elif mode=="object_acl":
            for key,acl in (("foo1","public-read"),("foo2","public-read-write"),("foo3","authenticated-read")):rejected(put_empty(bucket,key,acl),403)
            require_ok(put_empty(bucket,"foo4","private"))
        else:
            configured=document(bucket,"s3:GetObject",principal={"AWS":"arn:aws:iam::s3tenant1:root"} if mode=="principal" else None,objects=True)
            with cleanup_policy(bucket):
                response=raw(bucket,"PutBucketPolicy",Policy=json.dumps(configured))
                if mode=="policy":rejected(response,403)
                else:require_ok(response)

class OwnershipScope(BucketScope):
    """Attach only the source creation-time ownership header before real SDK signing; inherit fresh-name ownership checks and object/bucket cleanup unchanged."""
    def __init__(self,probe,mode):
        super().__init__(probe);self.ownership_mode=mode
    def raw(self,op,**options):
        if op=="bucket_create_raw":options["wire_headers"]={"x-amz-object-ownership":self.ownership_mode}
        return super().raw(op,**options)

def alternate_api(bucket,name,**parameters):
    return bucket.raw("api."+name,input=parameters,identity="alt")

def ownership_actions(p,bucket,mode,expected):
    """Run every original ownership helper PUT/multipart/COPY/ACL branch, compare raw owner ID/name tuples, and abort all returned multipart IDs even after source assertions fail."""
    uploads=[]
    def owner(data):return data["Owner"]["ID"],data["Owner"]["DisplayName"]
    def compare(data,equal):
        if equal:assert_equal(owner(data),expected)
        else:assert_equal(owner(data)!=expected,True)
    def wanted(acl):return mode=="BucketOwnerEnforced" or (mode=="BucketOwnerPreferred" and acl=="bucket-owner-full-control")
    try:
        for kind in ("put-object","create-multipart-upload","copy-object"):
            for suffix,acl in (("no-acl",None),("bucket-owner-full-control","bucket-owner-full-control"),("private","private")):
                key=kind+"-"+suffix;headers={"x-amz-acl":acl} if acl is not None else {}
                if kind=="put-object":result=put_empty(bucket,key,acl,"alt")
                elif kind=="create-multipart-upload":
                    result=bucket.raw("mp_create",key=key,identity="alt",**({"wire_headers":headers} if headers else {}))
                    if result.get("ok"):
                        ident=result.get("upload_id")
                        if not isinstance(ident,str) or not ident:raise RuntimeError("successful multipart creation omitted cleanup ID")
                        uploads.append((key,ident))
                else:result=bucket.raw("copy",key=key,source_key="put-object-no-acl",source_bucket=bucket.name,identity="alt",**({"wire_headers":headers} if headers else {}))
                if mode=="BucketOwnerEnforced" and acl=="private":
                    rejected(result,400,"AccessControlListNotSupported");continue
                require_ok(result)
                observed=require_ok(alternate_api(bucket,"ListParts",Key=key,UploadId=result["upload_id"]))["data"] if kind=="create-multipart-upload" else require_ok(alternate_api(bucket,"GetObjectAcl",Key=key))["data"]
                compare(observed,wanted(acl))
        for operation,arguments in (("PutBucketAcl",{"ACL":"private"}),("PutObjectAcl",{"Key":"put-object-no-acl","ACL":"private"})):
            result=alternate_api(bucket,operation,**arguments)
            if mode=="BucketOwnerEnforced":rejected(result,400,"AccessControlListNotSupported")
            else:require_ok(result)
    finally:
        failures=[]
        for key,ident in uploads:
            try:
                result=bucket.raw("mp_abort",key=key,identity="alt",upload_id=ident)
                if not result.get("ok") and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):failures.append(result)
            except Exception as error:failures.append(str(error))
        if failures:raise RuntimeError("ownership multipart cleanup failed: "+str(failures))

def ownership(p,mode,creation):
    """Keep creation-time vs later ownership changes, public ACL rejection before enforcement, raw one-rule GetOwnership assertion and full alternate-user helper oracles."""
    configured=actor(p);expected=(configured["canonical_id"],configured["display_name"])
    scope=OwnershipScope(p,mode) if creation else BucketScope(p,**({"create_operation":"bucket_create_acl","create_options":{"acl":"public-read"}} if mode=="BucketOwnerEnforced" else {}))
    with scope as bucket:
        if not creation:
            configuration={"Rules":[{"ObjectOwnership":mode}]}
            if mode=="BucketOwnerEnforced":
                rejected(raw(bucket,"PutBucketOwnershipControls",OwnershipControls=configuration),400,"InvalidBucketAclWithObjectOwnership")
                api(bucket,"PutBucketAcl",ACL="private")
            api(bucket,"PutBucketOwnershipControls",OwnershipControls=configuration)
        rules=api(bucket,"GetBucketOwnershipControls")["data"]["OwnershipControls"]["Rules"]
        assert_equal(len(rules),1);assert_equal(rules[0]["ObjectOwnership"],mode)
        with cleanup_policy(bucket,document(bucket,"*")):ownership_actions(p,bucket,mode,expected)

BASE=SCOPE_OPERATIONS
STATUS=(("empty","test_get_bucket_policy_status"),("public_acl","test_get_public_acl_bucket_policy_status"),("authenticated_acl","test_get_authpublic_acl_bucket_policy_status"),("public_policy","test_get_publicpolicy_acl_bucket_policy_status"),("source_ip","test_get_nonpublicpolicy_acl_bucket_policy_status"),("principal","test_get_nonpublicpolicy_principal_bucket_policy_status"))
CASES=tuple(case("ceph_public_status_"+mode,"Ceph 公共策略状态 "+mode,"保留原始 IsPublic 布尔值、ACL、固定租户 Principal 和 10.0.0.0/32 条件，不协调厂商预期",BASE+" api.GetBucketPolicyStatus"+(" api.PutBucketAcl" if mode in ("public_acl","authenticated_acl") else "")+(" api.PutBucketPolicy api.DeleteBucketPolicy" if mode in ("public_policy","source_ip","principal") else ""),lambda p,mode=mode:policy_status(p,mode),reference,CEPH) for mode,reference in STATUS)
BLOCK=(("bucket_acl","test_block_public_put_bucket_acls"),("object_acl","test_block_public_object_canned_acls"),("policy","test_block_public_policy"),("principal","test_block_public_policy_with_principal"),("restrict","test_block_public_restrict_public_buckets"),("ignore_acl","test_ignore_public_acls"))
for mode,reference in BLOCK:
    operations=BASE+" api.PutPublicAccessBlock"
    if mode=="bucket_acl":operations+=" api.GetPublicAccessBlock api.PutBucketAcl"
    if mode=="object_acl":operations+=" put_http wire_headers"
    if mode in ("policy","principal","restrict"):operations+=" api.PutBucketPolicy api.DeleteBucketPolicy"
    if mode=="restrict":operations+=" api.DeletePublicAccessBlock put_http get_http identity_anonymous"
    if mode=="ignore_acl":operations+=" api.PutBucketAcl list_v1_page identity_alt put_http get_http wire_headers"
    CASES+=(case("ceph_public_controls_"+mode,"Ceph 公共访问阻止 "+mode,"保留原始四布尔值、全部状态/正文断言、实际匿名/另用户请求；策略 finally 删除",operations,lambda p,mode=mode:public_block(p,mode),reference,CEPH),)
for mode,suffix in (("BucketOwnerEnforced","bucket_owner_enforced"),("BucketOwnerPreferred","bucket_owner_preferred"),("ObjectWriter","object_writer")):
    for creation in (True,False):
        operations=BASE+" wire_headers put_http copy mp_create mp_abort api.ListParts api.GetObjectAcl api.PutBucketAcl api.PutObjectAcl api.PutBucketPolicy api.DeleteBucketPolicy api.GetBucketOwnershipControls identity_alt"
        if not creation:operations+=" api.PutBucketOwnershipControls"+(" bucket_create_acl" if mode=="BucketOwnerEnforced" else "")
        CASES+=(case("ceph_public_ownership_"+("create_" if creation else "put_")+suffix,"Ceph 对象所有权 "+("创建时 " if creation else "设置 ")+mode,"完整保留源 PUT/分块/COPY/ACL 的每个所有者 ID+名称断言及错误码；配置期望身份，清理所有实际 multipart ID",operations,lambda p,mode=mode,creation=creation:ownership(p,mode,creation),("test_create_bucket_" if creation else "test_put_bucket_ownership_")+suffix,CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_public_controls",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
