"""Consume official Java/C++ presigned URLs using original Ceph raw HTTP assertions."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api
from .ceph_raw_http import SourceHttp
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

class PresignedBucket(BucketScope):
    """Keep every setup/read/cleanup request in the original real credential namespace; raw PUT outcomes are included in scoped object cleanup."""
    def __init__(self,p,identity,public):
        super().__init__(p,create_operation="bucket_create_acl" if public else "bucket_create_raw",create_options={"acl":"public-read"} if public else None)
        self.identity=identity
    def raw(self,op,**options):
        options.setdefault("identity",self.identity)
        return super().raw(op,**options)

def presign(bucket,method,expires=None,acl=None,version="v4"):
    options={"method":method,"key":"foo","signature_version":version}
    if expires is not None:options["expires_seconds"]=expires
    if acl is not None:options["acl"]=acl
    # Local SDK validation failures stay local failures: they cannot satisfy any
    # source server-403 assertion and must never manufacture an HTTP response.
    return bucket.call("presign",**options)["url"]

def vector(p,mode,identity="main",version="v4"):
    if identity=="tenant" and "identity_tenant" not in p.ctx.worker.capabilities:raise CaseNotRun("original tenant source requires real independent credentials","identity_tenant")
    if version=="v2" and "presign_v2" not in p.ctx.worker.capabilities:raise CaseNotRun("official selected SDK has no implemented V2 presigner; V4 cannot replace source V2","presign_v2")
    transport=SourceHttp(p)
    public=mode not in {"put_acl","expired_put"}
    with PresignedBucket(p,identity,public) as bucket:
        if mode!="put_acl":
            p.ctx.body.write_bytes(b"")
            bucket.call("put_acl" if public else "put",key="foo",body_path=str(p.ctx.body),**({"acl":"public-read"} if public else {}))
        if mode=="put_acl":
            url=presign(bucket,"PUT",acl="private",version=version)
            bucket.written_keys.add("foo")
            response=transport.request("PUT",url,data=b"hello world",headers={"x-amz-acl":"private"})
            assert_equal(response.status_code,200)
            url=presign(bucket,"GET",version=version)
            response=transport.request("GET",url)
            assert_equal(response.status_code,200);assert_equal(response.text,"hello world")
        elif mode.startswith("cors_"):
            method="GET" if mode=="cors_get" else "PUT"
            url=presign(bucket,method,100000,"private" if mode=="cors_put_acl" else None,version)
            assert_equal(transport.request("OPTIONS",url).status_code,400)
            api(bucket,"PutBucketCors",CORSConfiguration={"CORSRules":[{"AllowedMethods":[method],"AllowedOrigins":["example"]}]})
            response=transport.request("OPTIONS",url,headers={"Origin":"example","Access-Control-Request-Method":method})
            assert_equal(response.status_code,200)
            assert_equal(response.headers.get("access-control-allow-origin"),"example")
            assert_equal(response.headers.get("access-control-allow-methods"),method)
        else:
            expires={"not_expired":100000,"zero":0,"above_max":609901,"negative":-7,"expired_put":-1000}[mode]
            method="PUT" if mode=="expired_put" else "GET"
            url=presign(bucket,method,expires,version=version)
            if mode=="not_expired":assert_equal(transport.request("OPTIONS",url).status_code,400)
            response=transport.request(method,url,**({"data":"foo"} if method=="PUT" else {}))
            assert_equal(response.status_code,200 if mode=="not_expired" else 403)
    return {**SourceHttp.evidence(),"url_signer":"official_sdk","signature_version":version,"credential_identity":identity}

ROWS=(
("not_expired","main","v4","test_object_raw_get_x_amz_expires_not_expired"),
("not_expired","tenant","v4","test_object_raw_get_x_amz_expires_not_expired_tenant"),
("zero","main","v4","test_object_raw_get_x_amz_expires_out_range_zero"),
("above_max","main","v4","test_object_raw_get_x_amz_expires_out_max_range"),
("negative","main","v4","test_object_raw_get_x_amz_expires_out_positive_range"),
("put_acl","main","v4","test_object_presigned_put_object_with_acl"),
("put_acl","tenant","v4","test_object_presigned_put_object_with_acl_tenant"),
("expired_put","main","v4","test_object_raw_put_authenticated_expired"),
("cors_get","main","v4","test_cors_presigned_get_object"),
("cors_get","tenant","v4","test_cors_presigned_get_object_tenant"),
("cors_get","main","v2","test_cors_presigned_get_object_v2"),
("cors_get","tenant","v2","test_cors_presigned_get_object_tenant_v2"),
("cors_put","main","v4","test_cors_presigned_put_object"),
("cors_put_acl","main","v4","test_cors_presigned_put_object_with_acl"),
("cors_put","main","v2","test_cors_presigned_put_object_v2"),
("cors_put","tenant","v2","test_cors_presigned_put_object_tenant_v2"),
("cors_put","tenant","v4","test_cors_presigned_put_object_tenant"),
("cors_put_acl","tenant","v4","test_cors_presigned_put_object_tenant_with_acl"),
)
CASES=tuple(case("ceph_presigned_"+mode+"_"+identity+"_"+version,"Ceph 原生SDK预签名 "+source,"保留原始expires缺省/100000/0/609901/-7/-1000、真实tenant身份、ACL以及原OPTIONS/GET/PUT/CORS全部状态和响应头断言；SDK本地拒绝不冒充服务器403",SCOPE_OPERATIONS+" bucket_create_acl put put_acl presign presign_"+version+(" identity_tenant" if identity=="tenant" else "")+(" api.PutBucketCors" if mode.startswith("cors_") else ""),lambda p,m=mode,i=identity,v=version:vector(p,m,i,v),source,CEPH) for mode,identity,version,source in ROWS)

def register(registry):
    registry.add_suite(Suite("ceph_presigned_urls",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
