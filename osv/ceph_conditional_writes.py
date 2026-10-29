"""Preserve pinned Ceph conditional PUT and copy inputs, source ETags and strict rejection oracles."""
from .rejected_mutation import check_rejected_mutation
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH

def read(bucket,key="foo"):
    """Consume actual SDK GET output and its ETag; expected bytes remain independent literals from the source."""
    path=bucket.probe.ctx.output;path.unlink(missing_ok=True)
    response=bucket.call("get_http",key=key,output_path=str(path))
    return response,path.read_bytes()

def put_condition(p,mode):
    """Keep all original bar/zar reads, bare versus quoted ETags, wildcard headers and the original 404 NoSuchKey expectation for missing If-Match."""
    if mode in ("none_bad","none_etag"):
        raise CaseNotRun("PutObject If-None-Match supports only '*'; this source vector has no general S3 acceptance contract", "nonstandard If-None-Match contract")
    with BucketScope(p) as bucket:
        missing=mode in ("match_absent","none_absent")
        if not missing:
            bucket.put("foo",b"bar")
            previous,body=read(bucket);assert_equal(body,b"bar")
        if mode in ("match_etag","none_etag"):value=previous["etag"]
        elif mode in ("match_bad","match_absent"):value='"00000000000000000000000000000000"'
        elif mode=="match_existing":value=previous["etag"]
        elif mode=="none_bad":value="ABCORZ"
        else:value="*"
        header="If-Match" if mode.startswith("match_") else "If-None-Match"
        p.ctx.body.write_bytes(b"bar" if missing else b"zar")
        response=bucket.raw("put_http",key="foo",body_path=str(p.ctx.body),wire_headers={header:value})
        negative=mode in ("match_bad","match_absent","none_etag","none_existing")
        if negative:
            check_rejected_mutation(bucket,"foo",response,404 if mode=="match_absent" else 412,
                                    "NoSuchKey" if mode=="match_absent" else "PreconditionFailed",
                                    None if missing else b"bar",operation="PutObject")
        else:
            require_ok(response)
            _,body=read(bucket)
            assert_equal(body,b"bar" if missing else b"zar")

def copy_condition(p,mode):
    """Send the source PUT's actual quoted ETag or literal ABCORZ to the real CopyObject condition header, retaining exact 412 and successful destination-body assertions."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        previous=bucket.call("put_http",key="foo",body_path=str(p.ctx.body))
        value=previous["etag"] if mode.endswith("etag") else "ABCORZ"
        header="x-amz-copy-source-if-match" if mode.startswith("match_") else "x-amz-copy-source-if-none-match"
        response=bucket.raw("copy",key="bar",source_key="foo",wire_headers={header:value})
        if mode in ("match_bad","none_etag"):check_rejected_mutation(bucket,"bar",response,412,"PreconditionFailed",operation="CopyObject")
        else:
            require_ok(response)
            _,body=read(bucket,"bar");assert_equal(body,b"bar")

PUTS=(("match_etag","test_put_object_ifmatch_good"),("match_bad","test_put_object_ifmatch_failed"),("match_existing","test_put_object_ifmatch_overwrite_existed_good"),("match_absent","test_put_object_ifmatch_nonexisted_failed"),("none_bad","test_put_object_ifnonmatch_good"),("none_etag","test_put_object_ifnonmatch_failed"),("none_absent","test_put_object_ifnonmatch_nonexisted_good"),("none_existing","test_put_object_ifnonmatch_overwrite_existed_failed"))
COPIES=(("match_etag","test_copy_object_ifmatch_good"),("match_bad","test_copy_object_ifmatch_failed"),("none_etag","test_copy_object_ifnonematch_good"),("none_bad","test_copy_object_ifnonematch_failed"))
CASES=tuple(case("ceph_put_condition_"+mode,"Ceph 条件写入 "+mode,"保留原始 ETag 引号、bar/zar、通配符、精确错误码和后续读取；不协调原始缺对象 404 预期",SCOPE_OPERATIONS+" put get_http put_http wire_headers",lambda p,mode=mode:put_condition(p,mode),reference,CEPH) for mode,reference in PUTS)
CASES+=tuple(case("ceph_copy_condition_"+mode,"Ceph 条件复制 "+mode,"实际源 PUT ETag 进入 CopyObject 条件头，保留精确 412 和目标对象完整读取",SCOPE_OPERATIONS+" put_http copy get_http wire_headers",lambda p,mode=mode:copy_condition(p,mode),reference,CEPH) for mode,reference in COPIES)

def register(registry):
    registry.add_suite(Suite("ceph_conditional_writes",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
