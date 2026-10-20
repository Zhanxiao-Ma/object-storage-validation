"""Translate bucket lifecycle assertions in fresh owned buckets through genuine SDK operations."""
import uuid
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def rejected(p,response,status,code=None):
    """Require the source service status/code; client validation or local adapter crashes cannot satisfy the negative case."""
    assert_equal(response["ok"],False)
    assert_equal(response.get("error_kind"),"not_found" if status==404 else "local_error" if p.ctx.worker.info["api"]=="local" else "sdk_error")
    assert_equal(response.get("status_code"),status)
    if code is not None:assert_equal(response.get("error_code"),code)

def lifecycle(p,mode):
    """Provision a fresh bucket and exercise actual head, deletion or missing-key responses; always clean the owned scope."""
    with BucketScope(p) as bucket:
        if mode=="head":assert_equal(bucket.call("bucket_head")["status_code"],200)
        elif mode=="delete_nonempty":
            bucket.put("foo",b"foo")
            rejected(p,bucket.raw("bucket_delete"),409,"BucketNotEmpty")
        elif mode=="create_delete":
            bucket.call("bucket_delete")
            rejected(p,bucket.raw("bucket_delete"),404,"NoSuchBucket")
        elif mode=="missing_key":rejected(p,bucket.raw("get",key="bar",output_path=str(p.ctx.output)),404,"NoSuchKey")
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def nonexistent(p,operation):
    """Use a fresh UUID bucket name without creating it; expect an actual 404 instead of a locally manufactured not-found result."""
    name="osv-"+uuid.uuid4().hex
    head=p.ctx.worker.call({"op":"bucket_head","bucket":name})
    rejected(p,head,404)
    response=p.ctx.worker.call({"op":operation,"bucket":name,"prefix":""})
    rejected(p,response,404,None if operation=="bucket_head" else "NoSuchBucket")

CASES=tuple(case("ceph_bucket_"+mode,"Ceph 独立桶生命周期 "+mode,"按源用例验证实际状态/错误，最终清理独立测试桶",SCOPE_OPERATIONS+extra,lambda p,mode=mode:lifecycle(p,mode),reference,CEPH)
    for mode,extra,reference in (("head","","test_bucket_head"),("delete_nonempty"," put","test_bucket_delete_nonempty"),("create_delete","","test_bucket_create_delete"),("missing_key"," get","test_object_read_not_exist")))
CASES+=tuple(case("ceph_bucket_missing_"+mode,"Ceph 不存在桶 "+mode,"真实服务端返回 404；删除和列举还要求 NoSuchBucket","bucket_head "+operation,lambda p,operation=operation:nonexistent(p,operation),reference,CEPH)
    for mode,operation,reference in (("head","bucket_head","test_bucket_head_notexist"),("delete","bucket_delete","test_bucket_delete_notexist"),("list_v2","list_page","test_bucketv2_notexist")))

def register(registry):
    registry.add_suite(Suite("ceph_buckets",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
