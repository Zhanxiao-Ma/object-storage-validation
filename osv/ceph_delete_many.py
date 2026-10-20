"""Translate actual Ceph multi-object deletion, idempotency and single-request 1001-key rejection."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_list_exact import OPS,populate
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def all_objects(bucket,version):
    """Use real V1/V2 pages and returned cursors, preserving the source paginator count and rejecting a non-advancing service."""
    cursor=None;seen=set();objects=[]
    while True:
        options={}
        if cursor is not None:options["marker" if version==1 else "token"]=cursor
        page=bucket.call(OPS[version],**options)
        assert_equal("Contents" in page["fields"],True)
        objects.extend(page["objects"])
        if not page["truncated"]:return objects
        cursor=page["next_marker"] if version==1 else page["next_token"]
        if version==1 and not cursor:cursor=page["keys"][-1] if page["keys"] else None
        if not isinstance(cursor,str) or not cursor or cursor in seen:raise AssertionError("listing cursor did not advance")
        seen.add(cursor)

def deleted(p,version,limit):
    """Store independent source keys, invoke exactly one bulk operation per attempt and preserve source acknowledgments and field-absence checks."""
    names=["key-"+str(i) for i in range(1001)] if limit else ["key0","key1","key2"]
    with BucketScope(p) as bucket:
        populate(bucket,names)
        if limit:
            assert_equal(len(all_objects(bucket,version)),1001)
            response=bucket.raw("delete_many",objects=[{"key":name} for name in names])
            assert_equal(response["ok"],False)
            assert_equal(response.get("error_kind"),"local_error" if p.ctx.worker.info["api"]=="local" else "sdk_error")
            assert_equal(response.get("status_code"),400)
            return
        assert_equal(len(bucket.call(OPS[version])["objects"]),3)
        for attempt in range(2):
            response=bucket.call("delete_many",objects=[{"key":name} for name in names])
            assert_equal(len(response["deleted"]),3)
            assert_equal(response["errors_present"],False)
            page=bucket.call(OPS[version])
            assert_equal(("Contents","Contents" in page["fields"]),("Contents",False))

CASES=tuple(case("ceph_delete_many_v"+str(version)+("_limit" if limit else "_repeat"),"Ceph 批量删除 V"+str(version)+(" 1001 键上限" if limit else " 重复删除"),"保留原始一次性请求、删除确认、Errors/Contents 字段有无和实际 HTTP 400 断言",SCOPE_OPERATIONS+" put delete_many "+OPS[version],lambda p,version=version,limit=limit:deleted(p,version,limit),"test_multi_object"+("v2" if version==2 else "")+"_delete"+("_key_limit" if limit else ""),CEPH) for version in (1,2) for limit in (False,True))

def register(registry):
    registry.add_suite(Suite("ceph_delete_many",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
