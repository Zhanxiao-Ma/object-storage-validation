"""Translate Ceph canned and explicit ACL vectors through actual SDK models."""
from copy import deepcopy
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

ALL="http://acs.amazonaws.com/groups/global/AllUsers"
AUTHENTICATED="http://acs.amazonaws.com/groups/global/AuthenticatedUsers"

def actor(p,name="main",email=False):
    """Require independent expected identity metadata; never derive expected canonical IDs from the service under test."""
    configured=p.ctx.workload.get("identities",{}).get(name,{})
    required=("canonical_id","display_name")+(("email",) if email else ())
    missing=["workload.identities."+name+"."+key for key in required if configured.get(key) is None]
    if missing:raise CaseNotRun("Ceph ACL source requires configured identity metadata",*missing)
    return configured

def grant(permission,identity=None,uri=None):
    return {"Permission":permission,"ID":identity["canonical_id"] if identity else None,
        "DisplayName":identity["display_name"] if identity else None,"URI":uri,"EmailAddress":None,
        "Type":"CanonicalUser" if identity else "Group"}

def check_grants(actual,expected):
    """Retain the pinned source helper's conditional sort, field absence handling and rejection of extra grant fields, including its original ordering limitations."""
    actual=deepcopy(actual);expected=deepcopy(expected)
    assert_equal(len(actual),len(expected))
    if actual[0]["Grantee"].get("DisplayName"):
        actual.sort(key=lambda value:value["Grantee"].get("DisplayName"))
        expected.sort(key=lambda value:value["DisplayName"])
    for got,want in zip(actual,expected):
        assert_equal(got.pop("Permission",None),want["Permission"])
        for field in ("DisplayName","ID","Type","URI","EmailAddress"):
            assert_equal(got["Grantee"].pop(field,None),want[field])
        assert_equal(got,{"Grantee":{}})

def wanted(identity,acl):
    public=[]
    if acl in ("public-read","public-read-write"):public.append(grant("READ",uri=ALL))
    if acl=="public-read-write":public.append(grant("WRITE",uri=ALL))
    if acl=="authenticated-read":public.append(grant("READ",uri=AUTHENTICATED))
    return public+[grant("FULL_CONTROL",identity)]

def canned(p,kind,mode):
    """Preserve creation-time canned ACLs and source private transitions, literal foo/bar data, and independently configured canonical-user expectations."""
    identity=actor(p)
    acl={"default":"private","public":"public-read","cycle":"public-read","readwrite":"public-read-write","authenticated":"authenticated-read"}[mode]
    scope=BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":acl}) if kind=="bucket" and mode!="default" else BucketScope(p)
    with scope as bucket:
        if kind=="object":
            p.ctx.body.write_bytes(b"bar")
            bucket.call("put" if mode=="default" else "put_acl",key="foo",body_path=str(p.ctx.body),**({"acl":acl} if mode!="default" else {}))
        options={"Key":"foo"} if kind=="object" else {}
        result=api(bucket,"GetBucketAcl" if kind=="bucket" else "GetObjectAcl",**options)["data"]
        if kind=="bucket" and mode=="default":
            assert_equal(result["Owner"]["DisplayName"],identity["display_name"]);assert_equal(result["Owner"]["ID"],identity["canonical_id"])
        check_grants(result["Grants"],wanted(identity,acl))
        if mode=="cycle":
            api(bucket,"PutBucketAcl" if kind=="bucket" else "PutObjectAcl",ACL="private",**options)
            check_grants(api(bucket,"GetBucketAcl" if kind=="bucket" else "GetObjectAcl",**options)["data"]["Grants"],wanted(identity,"private"))
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def private_to_private(p):
    with BucketScope(p) as bucket:assert_equal(api(bucket,"PutBucketAcl",ACL="private")["status_code"],200)

def change_permission(p,permission):
    """Feed actual owner/grants into the real SDK ACL update with only the source permission mutation, then compare against independent canonical metadata."""
    identity=actor(p)
    with BucketScope(p) as bucket:
        bucket.put("foo",b"bar")
        result=api(bucket,"GetObjectAcl",Key="foo")["data"]
        policy={"Owner":result["Owner"],"Grants":result["Grants"]}
        policy["Grants"][0]["Permission"]=permission
        api(bucket,"PutObjectAcl",Key="foo",AccessControlPolicy=policy)
        check_grants(api(bucket,"GetObjectAcl",Key="foo")["data"]["Grants"],[grant(permission,identity)])

def append_grant(bucket,identity,value):
    """Use actual SDK grants as the producer output, appending only the original source input and its independently configured owner."""
    observed=api(bucket,"GetBucketAcl")["data"]["Grants"]
    observed.append(value)
    return {"Grants":observed,"Owner":{"DisplayName":identity["display_name"],"ID":identity["canonical_id"]}}

def explicit_bucket(p,mode):
    identity=actor(p)
    other=actor(p,"alt",email=True) if mode=="email" else None
    with BucketScope(p) as bucket:
        if mode=="group":value={"Grantee":{"Type":"Group","URI":ALL},"Permission":"READ"}
        elif mode=="nonexistent":value={"Grantee":{"Type":"CanonicalUser","ID":"_foo"},"Permission":"FULL_CONTROL"}
        else:value={"Grantee":{"Type":"AmazonCustomerByEmail","EmailAddress":other["email"] if other else "doesnotexist@dreamhost.com.invalid"},"Permission":"FULL_CONTROL"}
        policy=append_grant(bucket,identity,value)
        if mode in ("nonexistent","email_nonexistent"):
            rejected(raw(bucket,"PutBucketAcl",AccessControlPolicy=policy),400,"InvalidArgument" if mode=="nonexistent" else "UnresolvableGrantByEmailAddress")
        else:
            api(bucket,"PutBucketAcl",AccessControlPolicy=policy)
            check_grants(api(bucket,"GetBucketAcl")["data"]["Grants"],([grant("READ",uri=ALL)] if mode=="group" else [grant("FULL_CONTROL",other)])+[grant("FULL_CONTROL",identity)])

def revoke(p):
    """Preserve the zero-grants source assertion and always restore the exact observed owner/grants for safe cleanup, including assertion failures."""
    with BucketScope(p) as bucket:
        bucket.put("foo",b"bar")
        observed=api(bucket,"GetBucketAcl")["data"]
        old={"Owner":observed["Owner"],"Grants":observed["Grants"]}
        try:
            api(bucket,"PutBucketAcl",AccessControlPolicy={"Owner":old["Owner"],"Grants":[]})
            assert_equal(len(api(bucket,"GetBucketAcl")["data"]["Grants"]),0)
        finally:api(bucket,"PutBucketAcl",AccessControlPolicy=old)


def user_grant(p,permission):
    """Retain independent main/alt metadata and the exact source READ/READ_ACP/WRITE/WRITE_ACP authorization vector through distinct SDK clients."""
    identity=actor(p);other=actor(p,"alt")
    with BucketScope(p) as bucket:
        value={"Grantee":{"ID":other["canonical_id"],"Type":"CanonicalUser"},"Permission":permission}
        api(bucket,"PutBucketAcl",AccessControlPolicy=append_grant(bucket,identity,value))
        check_grants(api(bucket,"GetBucketAcl")["data"]["Grants"],[grant(permission,other),grant("FULL_CONTROL",identity)])
        allowed={"FULL_CONTROL":(True,True,True,True),"READ":(True,False,False,False),"READ_ACP":(False,True,False,False),"WRITE":(False,False,True,False),"WRITE_ACP":(False,False,False,True)}[permission]
        requests=[("bucket_head",{}),("api.GetBucketAcl",{"input":{}}),("put",{"key":"foo-write","body_path":str(p.ctx.body)}),("api.PutBucketAcl",{"input":{"ACL":"public-read"}})]
        p.ctx.body.write_bytes(b"bar")
        for can,(operation,options) in zip(allowed,requests):
            observed=bucket.raw(operation,identity="alt",**options)
            if can:
                from .scenarios import require_ok
                require_ok(observed)
            else:rejected(observed,403)
        if permission=="FULL_CONTROL":
            observed=api(bucket,"GetBucketAcl")["data"]["Owner"]
            assert_equal(observed["ID"],identity["canonical_id"]);assert_equal(observed["DisplayName"],identity["display_name"])

CANNED=(("default","default"),("public","canned_during_create"),("cycle","canned"),("readwrite","canned_publicreadwrite"),("authenticated","canned_authenticatedread"))
CASES=tuple(case("ceph_acl_"+kind+"_"+mode,"Ceph 预置 ACL "+kind+" "+mode,"保留源创建时 ACL、转换、完整授权字段及原始 check_grants 规则",SCOPE_OPERATIONS+" api.Get"+kind.title()+"Acl"+(" api.Put"+kind.title()+"Acl" if mode=="cycle" else "")+(" bucket_create_acl" if kind=="bucket" and mode!="default" else " put" if kind=="object" and mode=="default" else " put_acl" if kind=="object" else ""),lambda p,kind=kind,mode=mode:canned(p,kind,mode),"test_"+kind+"_acl_"+suffix,CEPH) for kind in ("bucket","object") for mode,suffix in CANNED)
CASES+=tuple(case("ceph_acl_permission_"+permission.lower(),"Ceph 对象显式 ACL "+permission,"保留原始 Owner/Grants 到实际更新调用的传递及完整授权字段断言",SCOPE_OPERATIONS+" put api.GetObjectAcl api.PutObjectAcl",lambda p,permission=permission:change_permission(p,permission),reference,CEPH) for permission,reference in (("FULL_CONTROL","test_object_acl"),("WRITE","test_object_acl_write"),("WRITE_ACP","test_object_acl_writeacp"),("READ","test_object_acl_read"),("READ_ACP","test_object_acl_readacp")))
CASES+=tuple(case("ceph_acl_explicit_"+mode,"Ceph 桶显式授权 "+mode,"保留组/电子邮件/不存在身份的原始输入和授权或精确服务端错误",SCOPE_OPERATIONS+" api.GetBucketAcl api.PutBucketAcl",lambda p,mode=mode:explicit_bucket(p,mode),reference,CEPH) for mode,reference in (("group","test_put_bucket_acl_grant_group_read"),("nonexistent","test_bucket_acl_grant_nonexist_user"),("email","test_bucket_acl_grant_email"),("email_nonexistent","test_bucket_acl_grant_email_not_exist")))
CASES+=(case("ceph_acl_private_to_private","Ceph private ACL 幂等设置","源断言实际 HTTP 200",SCOPE_OPERATIONS+" api.PutBucketAcl",private_to_private,"test_bucket_acl_canned_private_to_private",CEPH),case("ceph_acl_revoke_all","Ceph 移除全部授权","原始 Grants 清零断言，最终恢复实际原始策略",SCOPE_OPERATIONS+" put api.GetBucketAcl api.PutBucketAcl",revoke,"test_bucket_acl_revoke_all",CEPH))

CASES+=tuple(case("ceph_acl_user_"+permission.lower(),"Ceph CanonicalUser 授权 "+permission,"保留独立用户 ID/DisplayName、原始授权列表及另一身份的读/读 ACL/写/写 ACL 许可矩阵",SCOPE_OPERATIONS+" put identity_alt api.GetBucketAcl api.PutBucketAcl",lambda p,permission=permission:user_grant(p,permission),reference,CEPH) for permission,reference in (("FULL_CONTROL","test_bucket_acl_grant_userid_fullcontrol"),("READ","test_bucket_acl_grant_userid_read"),("READ_ACP","test_bucket_acl_grant_userid_readacp"),("WRITE","test_bucket_acl_grant_userid_write"),("WRITE_ACP","test_bucket_acl_grant_userid_writeacp")))

def register(registry):
    registry.add_suite(Suite("ceph_acl",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
