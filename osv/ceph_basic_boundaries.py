"""Preserve basic missing-bucket, expected-owner and server-side concurrent-version source invariants."""
from pathlib import Path
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .ceph_versioning import configure
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH


def missing_list(p):
    """List a genuinely absent generated bucket; preserve the original V1 404/NoSuchBucket oracle without provisioning it."""
    bucket=BucketScope(p)
    rejected(bucket.raw("bucket_head"),404)
    rejected(bucket.raw("list_v1_page"),404,"NoSuchBucket")


def literal_missing_write(p):
    """Keep the source literal bucket/key/body; refuse to write if that global name is not demonstrably absent."""
    name="whatchutalkinboutwillis"
    absent=p.ctx.worker.call({"op":"bucket_head","bucket":name})
    if (absent.get("status_code"),absent.get("error_kind"))!=(404,"not_found"):
        raise CaseNotRun("source literal missing bucket is not demonstrably absent","absent source bucket "+name)
    p.ctx.body.write_bytes(b"foo")
    rejected(p.ctx.worker.call({"op":"put_http","bucket":name,"key":"foo","body_path":str(p.ctx.body)}),404,"NoSuchBucket")


def expected_owner(p):
    """Retain the source public-read-write transition, empty initial LIST, bar/coffee writes and wrong canonical-owner header on genuine anonymous SDK clients."""
    owner=p.ctx.workload.get("identities",{}).get("main",{}).get("canonical_id")
    if owner is None:raise CaseNotRun("source requires independently configured main canonical ID","workload.identities.main.canonical_id")
    with BucketScope(p) as bucket:
        api(bucket,"PutBucketAcl",ACL="public-read-write")
        bucket.call("list_v1_page");bucket.put("foo",b"bar")
        headers={"x-amz-expected-bucket-owner":owner+"foo"}
        rejected(bucket.raw("list_v1_page",identity="anonymous",wire_headers=headers),403,"AccessDenied")
        p.ctx.body.write_bytes(b"coffee")
        rejected(bucket.raw("put_http",key="bar",body_path=str(p.ctx.body),identity="anonymous",wire_headers=headers),403,"AccessDenied")


def batch(bucket,requests):
    """Submit literal source requests through one worker's shared default SDK client; retain ordered actual per-request outcomes."""
    if not requests:return []
    result=bucket.call("concurrent_requests",requests=requests)
    assert_equal(len(result["results"]),len(requests))
    return result["results"]


def concurrent_acl(p):
    """Keep all 50 original concurrent writes on the same native SDK client and require every original per-thread success flag."""
    with BucketScope(p) as bucket:
        requests=[{"op":"api.PutBucketAcl","input":{"Bucket":bucket.name,"ACL":"public-read"}} for index in range(50)]
        results=batch(bucket,requests)
        for result in results:assert_equal(result["ok"],True)
    return {"sdk_clients":1,"concurrent_requests":50,"cleanup_confirmed":bucket.cleanup_completed}


def version_phases(p):
    """Keep original five-by-five shared-client create/remove phases, actual listed IDs and final Versions absence; retain source thread-error propagation limitations."""
    errors=[];request_count=0
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled");bucket.written_keys.add("myobj")
        bodies=[]
        for index in range(5):
            body=p.ctx.directory/("concurrent-body-"+str(index));body.write_bytes(("data "+str(index)).encode());bodies.append(body)
        for iteration in range(5):
            requests=[{"op":"put_version","bucket":bucket.name,"key":"myobj","body_path":str(body)} for body in bodies]
            results=batch(bucket,requests);request_count+=len(requests)
            errors.extend(result.get("error","actual SDK request failed") for result in results if not result["ok"])
            listed=bucket.call("versions_list");assert_equal("Versions" in listed["fields"],True);assert_equal(len(listed["versions"]),5)
            # The original clear helper issues a fresh listing even though its
            # caller just checked a listing. Preserve both producer calls.
            listed=bucket.call("versions_list")
            requests=[{"op":"delete_version","bucket":bucket.name,"key":row["key"],"version_id":row["version_id"]} for row in listed["versions"]]
            results=batch(bucket,requests);request_count+=len(requests)
            errors.extend(result.get("error","actual SDK request failed") for result in results if not result["ok"])
            assert_equal("Versions" in bucket.call("versions_list")["fields"],False)
    return {"sdk_clients":1,"concurrent_requests_per_phase":5,"requests":request_count,
        "source_thread_errors":errors,"cleanup_confirmed":bucket.cleanup_completed}


V=VERSION_SCOPE_OPERATIONS+" versioning_put versioning_get put_version"
CASES=(
 case("ceph_basic_missing_list","Ceph 不存在桶 V1 列举","独立不存在桶返回精确404/NoSuchBucket","bucket_head list_v1_page",missing_list,"test_bucket_notexist",CEPH),
 case("ceph_basic_literal_missing_put","Ceph 原始字面不存在桶写入","保留whatchutalkinboutwillis/foo/foo；确认不存在后要求404/NoSuchBucket","bucket_head put_http",literal_missing_write,"test_object_write_to_nonexist_bucket",CEPH),
 case("ceph_basic_expected_owner","Ceph 错误预期桶拥有者","原始独立main ID+foo匿名条件头，LIST与coffee写入均403/AccessDenied",SCOPE_OPERATIONS+" api.PutBucketAcl list_v1_page put put_http wire_headers identity_anonymous",expected_owner,"test_expected_bucket_owner",CEPH),
 case("ceph_basic_concurrent_acl","Ceph 同client五十路ACL写入","原始50个线程操作同一真实SDK client，全部public-read请求成功且清理确认",SCOPE_OPERATIONS+" api.PutBucketAcl concurrent_requests",concurrent_acl,"test_bucket_concurrent_set_canned_acl",CEPH),
 case("ceph_basic_versions_phased","Ceph 五轮同client并发版本创建删除","保留5x5版本计数、实际列表ID传入并发删除、最终Versions字段缺失，使用同一个官方SDK client",V+" concurrent_requests",version_phases,"test_versioned_concurrent_object_create_concurrent_remove",CEPH),
)


def register(registry):
    registry.add_suite(Suite("ceph_basic_boundaries",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
