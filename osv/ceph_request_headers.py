"""Translate pinned Ceph V4 before-call header vectors without replacing SDK signing or transport."""
from contextlib import contextmanager
from email.utils import formatdate
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import rejected
from .scenarios import Suite,require_ok
from .suites import case,run_cases,CEPH

SOURCE=CEPH.rsplit("/",1)[0]+"/test_headers.py"

def recover_creation(bucket,expected_owner=None):
    """Clean an ambiguous candidate only after its real ACL owner matches a separately confirmed owned control bucket and an optional independent expected owner; never use cleanup proof as a compatibility oracle."""
    exists=bucket.raw("bucket_head")
    if (exists.get("error_kind"),exists.get("status_code"))==("not_found",404):return
    if not exists.get("ok"):raise RuntimeError("cannot verify ambiguous header-test bucket ownership: "+bucket.name)
    with BucketScope(bucket.probe) as control:
        owner=control.call("api.GetBucketAcl",input={}).get("data",{}).get("Owner",{}).get("ID")
        candidate=bucket.raw("api.GetBucketAcl",input={})
        if not isinstance(owner,str) or not owner or not candidate.get("ok") or candidate.get("data",{}).get("Owner",{}).get("ID")!=owner or (expected_owner is not None and owner!=expected_owner):
            raise RuntimeError("ambiguous header-test bucket owner is unconfirmed; manual inspection required: "+bucket.name)
        bucket.created=True
        bucket.__exit__(None,None,None)

@contextmanager
def creation(p,headers):
    """Test literal Bucket-only creation; clean confirmed creations and report ambiguous outcomes without deleting an unconfirmed bucket."""
    bucket=BucketScope(p)
    missing=bucket.raw("bucket_head")
    if missing.get("ok") or (missing.get("error_kind"),missing.get("status_code"))!=("not_found",404):raise RuntimeError("cannot establish fresh test bucket absence: "+str(missing))
    response=bucket.raw("bucket_create_raw",wire_headers=headers)
    bucket.created=response["ok"]
    try:yield bucket,response
    finally:
        if bucket.created:bucket.__exit__(None,None,None)
        else:recover_creation(bucket)

def object_headers(p,headers,status=None,code=None,typed_length=False):
    """Use original empty successful helper PUT followed by plain bar PUT, or original bad bar PUT; retain genuine SDK/service errors."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"" if status is None or typed_length else b"bar")
        options={"key":"foo","body_path":str(p.ctx.body),"wire_headers":headers}
        if typed_length:options.update(content_length=-1,checksum_when_required=True)
        response=bucket.raw("put_http",**options)
        if status is None:
            require_ok(response)
            bucket.put("foo",b"bar")
        else:rejected(response,status,code)

def ordinary_content_type(p,empty):
    """Preserve the original single bar PUT with an explicit empty ContentType or no ContentType argument."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        bucket.call("put_metadata" if empty else "put",key="foo",body_path=str(p.ctx.body),**({"content_type":"","metadata":{}} if empty else {}))

def bucket_headers(p,headers,status=None,code=None):
    with creation(p,headers) as (bucket,response):
        if status is None:require_ok(response)
        else:rejected(response,status,code)

def acl_headers(p,bad):
    """Keep source PutObjectAcl missing length or PutBucketAcl bad header override, with actual typed SDK ACL APIs."""
    with BucketScope(p) as bucket:
        if bad:
            response=bucket.raw("api.PutBucketAcl",input={"ACL":"public-read"},wire_headers={"x-amz-acl":"public-ready"})
            rejected(response,400)
        else:
            bucket.put("foo",b"bar")
            bucket.call("api.PutObjectAcl",input={"Key":"foo","ACL":"public-read"},wire_headers={"Content-Length":None})

def dates(p,empty):
    """Use the source RFC-2822 current date for both requested headers; let the real SDK signer retain or rewrite them before transport."""
    date=formatdate(usegmt=True)
    object_headers(p,{"Date":"" if empty else date,"X-Amz-Date":date})

# HTTP header APIs are textual: preserve the source Expect=200 wire value as "200".
OBJECT=(
 ("md5_invalid_short",{"Content-MD5":"YWJyYWNhZGFicmE="},400,"InvalidDigest","test_object_create_bad_md5_invalid_short"),
 ("md5_bad",{"Content-MD5":"rL0Y20xC+Fzt72VPzMSk2A=="},400,"BadDigest","test_object_create_bad_md5_bad"),
 ("md5_empty",{"Content-MD5":""},400,"InvalidDigest","test_object_create_bad_md5_empty"),
 ("md5_none",{"Content-MD5":None},None,None,"test_object_create_bad_md5_none"),
 ("expect_mismatch",{"Expect":"200"},None,None,"test_object_create_bad_expect_mismatch"),
 ("expect_empty",{"Expect":""},None,None,"test_object_create_bad_expect_empty"),
 ("expect_none",{"Expect":None},None,None,"test_object_create_bad_expect_none"),
 ("length_empty",{"Content-Length":""},400,None,"test_object_create_bad_contentlength_empty"),
 ("length_negative",{},400,None,"test_object_create_bad_contentlength_negative"),
 ("length_none",{"Content-Length":None},411,"MissingContentLength","test_object_create_bad_contentlength_none"),
 ("type_text",{"Content-Type":"text/plain"},None,None,"test_object_create_bad_contenttype_invalid"),
 ("authorization_empty",{"Authorization":""},403,None,"test_object_create_bad_authorization_empty"),
 ("authorization_none",{"Authorization":None},403,None,"test_object_create_bad_authorization_none"),
)
BUCKET=(
 ("length_none",{"Content-Length":None},None,None,"test_bucket_create_contentlength_none"),
 ("expect_mismatch",{"Expect":"200"},None,None,"test_bucket_create_bad_expect_mismatch"),
 ("expect_empty",{"Expect":""},None,None,"test_bucket_create_bad_expect_empty"),
 ("length_empty",{"Content-Length":""},400,None,"test_bucket_create_bad_contentlength_empty"),
 ("length_negative",{"Content-Length":"-1"},400,None,"test_bucket_create_bad_contentlength_negative"),
 ("bad_length_none",{"Content-Length":None},None,None,"test_bucket_create_bad_contentlength_none"),
 ("authorization_empty",{"Authorization":""},403,"AccessDenied","test_bucket_create_bad_authorization_empty"),
 ("authorization_none",{"Authorization":None},403,"AccessDenied","test_bucket_create_bad_authorization_none"),
)
CASES=tuple(case("ceph_header_object_"+name,"Ceph 原始对象请求头 "+name,"保留原始空对象/bar、请求头修改时机及实际 HTTP/错误码；SDK 后续重写保持可见",SCOPE_OPERATIONS+" put put_http wire_headers"+(" checksum_when_required" if name=="length_negative" else ""),lambda p,headers=headers,status=status,code=code,name=name:object_headers(p,headers,status,code,name=="length_negative"),reference,SOURCE) for name,headers,status,code,reference in OBJECT)
CASES+=tuple(case("ceph_header_bucket_"+name,"Ceph 原始建桶请求头 "+name,"保留 Bucket-only 建桶、原始请求头及服务端预期，明确区分不确定创建结果",SCOPE_OPERATIONS+" api.GetBucketAcl wire_headers",lambda p,headers=headers,status=status,code=code:bucket_headers(p,headers,status,code),reference,SOURCE) for name,headers,status,code,reference in BUCKET)
CASES+=tuple(case("ceph_header_type_"+mode,"Ceph ContentType "+mode,"保留原始单次 bar 写入及显式空字符串/未设置参数",SCOPE_OPERATIONS+(" put_metadata" if empty else " put"),lambda p,empty=empty:ordinary_content_type(p,empty),reference,SOURCE) for mode,empty,reference in (("empty",True,"test_object_create_bad_contenttype_empty"),("none",False,"test_object_create_bad_contenttype_none")))
CASES+=tuple(case("ceph_header_dates_"+mode,"Ceph Date/X-Amz-Date "+mode,"保留源动态 HTTP 日期和空 Date 组合，仍使用官方 SDK 签名",SCOPE_OPERATIONS+" put put_http wire_headers",lambda p,empty=empty:dates(p,empty),reference,SOURCE) for mode,empty,reference in (("both",False,"test_object_create_date_and_amz_date"),("empty_date",True,"test_object_create_amz_date_and_no_date")))
CASES+=tuple(case("ceph_header_acl_"+mode,"Ceph ACL 请求头 "+mode,"保留 public-read 模型参数和源头覆盖/删除及实际服务预期",SCOPE_OPERATIONS+" put wire_headers "+("api.PutBucketAcl" if bad else "api.PutObjectAcl"),lambda p,bad=bad:acl_headers(p,bad),reference,SOURCE) for mode,bad,reference in (("bad_canned",True,"test_bucket_put_bad_canned_acl"),("length_none",False,"test_object_acl_create_contentlength_none")))

def register(registry):
    registry.add_suite(Suite("ceph_request_headers",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
