"""Translate Ceph service-level bucket listing through actual SDK models and owned resources."""
from contextlib import ExitStack
from datetime import datetime,timezone,timedelta
from .ceph_bucket_config import rejected
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal,require_ok,SDKOperationError
from .suites import case,run_cases,CEPH

def inventory(p,mode):
    """Create exactly five owned buckets and retain source membership or UTC creation-time assertions without assuming other account buckets are absent."""
    before=int((datetime.now(timezone.utc)-timedelta(days=1)).timestamp()*1000)
    with ExitStack() as stack:
        buckets=[stack.enter_context(BucketScope(p)) for _ in range(5)]
        result=require_ok(p.ctx.worker.call({"op":"api.ListBuckets","input":{}}))["data"]
        names={bucket.name for bucket in buckets}
        if mode=="membership":
            actual={row["Name"] for row in result.get("Buckets",[])}
            for name in names:
                if name not in actual:raise AssertionError("source GET service omitted created bucket: "+name)
        else:
            for row in result.get("Buckets",[]):
                if row["Name"] in names:
                    if not before<=row["CreationDate"]:raise AssertionError("creation time predates the source's one-day lower bound: "+str(row))
    return {"isolated_buckets":sorted(names),"cleanup_confirmed":all(bucket.cleanup_completed for bucket in buckets)}

def anonymous(p):
    """Require unauthenticated ListBuckets to be denied; neither empty success nor leaked names prove authentication enforcement."""
    result=p.ctx.worker.call({"op":"api.ListBuckets","input":{},"identity":"anonymous"})
    if 300 <= (result.get("status_code") or 0) < 400:
        error=SDKOperationError(result)
        error.failure_category="transport_or_sdk"
        error.evidence.update(operation="ListBuckets",phase="anonymous_endpoint_redirect")
        error.args=("Anonymous ListBuckets was redirected before an S3 authorization verdict; inspect endpoint/Location: "+str(result),)
        raise error
    if not result.get("ok") and (result.get("error_code")=="Xml Parse Error" or any(text in result.get("error","") for text in ("Could not parse XML", "XML_ERROR_"))):
        error=SDKOperationError(result)
        error.failure_category="sdk_response_decoding"
        error.evidence.update(operation="ListBuckets",phase="anonymous_response_decode")
        raise error
    rejected(result,403,"AccessDenied")

CASES=tuple(case("ceph_inventory_"+mode,"Ceph 桶清单 "+mode,"保留原始五桶成员/一日前创建时间断言，使用独立桶并确认清理",SCOPE_OPERATIONS+" api.ListBuckets",lambda p,mode=mode:inventory(p,mode),reference,CEPH) for mode,reference in (("membership","test_buckets_create_then_list"),("ctime","test_buckets_list_ctime")))
CASES+=(case("ceph_inventory_anonymous","Ceph 匿名桶清单","真实匿名 ListBuckets 必须被403/AccessDenied拒绝，空成功也不能证明鉴权正确","api.ListBuckets identity_anonymous",anonymous,"test_list_buckets_anonymous",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_bucket_inventory",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
