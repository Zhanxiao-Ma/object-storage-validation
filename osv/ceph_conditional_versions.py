"""Preserve original Ceph conditional mutation matrices over current objects and historical versions."""
from datetime import datetime,timezone
from email.utils import format_datetime
from .rejected_mutation import check_rejected_mutation
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import rejected
from .ceph_multipart import uploading
from .ceph_versioning import configure
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH

BAD_MTIME_MS=1420070400000


def date_header(milliseconds):
    """Serialize actual source HEAD time as UTC HTTP-date without rounding up fractional milliseconds."""
    return format_datetime(datetime.fromtimestamp(milliseconds//1000,timezone.utc),usegmt=True)


def write(bucket,p,data=None,match=None,none=None,multipart=False):
    """Pass actual SDK part ETags to completion and preserve source default abc versus empty PUT bodies; abort every actual upload on all paths."""
    if match is not None and none is not None:raise ValueError("source conditional write selects one header")
    headers={"If-Match":match} if match is not None else {"If-None-Match":none} if none is not None else {}
    data=(b"abc" if multipart else b"") if data is None else data
    p.ctx.body.write_bytes(data)
    if not multipart:
        return bucket.raw("put_version",key="obj",body_path=str(p.ctx.body),**({"wire_headers":headers} if headers else {}))
    with uploading(bucket,p,key="obj",create_operation="mp_create_metadata") as ident:
        part=bucket.call("mp_part",key="obj",upload_id=ident,part_number=1,body_path=str(p.ctx.body))
        return bucket.raw("mp_complete_observed",key="obj",upload_id=ident,
            parts=[{"part_number":1,"etag":part["etag"].strip('"')}],**({"wire_headers":headers} if headers else {}))


def accepted(result,status=False):
    result=require_ok(result)
    if status:assert_equal(result["status_code"],200)
    return result


def failed(result,missing=False):
    rejected(result,404 if missing else 412,"NoSuchKey" if missing else "PreconditionFailed")


def delete(bucket,match=None,mtime=None,size=None,version=None):
    """Keep all three condition headers literal and independently observable; zero size is a real condition rather than an omitted value."""
    headers={}
    if match is not None:headers["If-Match"]=match
    if mtime is not None:headers["x-amz-if-match-last-modified-time"]=date_header(mtime)
    if size is not None:headers["x-amz-if-match-size"]=str(size)
    return bucket.raw("delete_version",key="obj",**({"version_id":version} if version is not None else {}),**({"wire_headers":headers} if headers else {}))


def put_matrix(p,mode,multipart=False):
    """Replay each source request in order, retaining historical ETags, default bodies, missing-current errors and the original delete-marker checks."""
    versioned=mode!="basic"
    with (VersionScope(p) if versioned else BucketScope(p)) as bucket:
        if versioned:configure(bucket,"Enabled")
        if mode=="current_none":
            first=accepted(write(bucket,p,b"data1",none="*",multipart=multipart))["etag"]
            current=accepted(write(bucket,p,b"data2",multipart=multipart))["etag"]
            failed(write(bucket,p,none="*",multipart=multipart))
            failed(write(bucket,p,none="*",multipart=multipart))
            accepted(delete(bucket));return
        if mode=="current_match":
            failed(write(bucket,p,match='"00000000000000000000000000000000"',multipart=multipart),True)
            failed(write(bucket,p,match='"00000000000000000000000000000000"',multipart=multipart),True)
            first=accepted(write(bucket,p,b"data1",none="*",multipart=multipart),True)
            version=first["version_id"]
            current=accepted(write(bucket,p,b"data2",match=first["etag"],multipart=multipart),True)["etag"]
            failed(write(bucket,p,match='"00000000000000000000000000000000"',multipart=multipart))
            failed(write(bucket,p,match=first["etag"],multipart=multipart))
            accepted(write(bucket,p,match=current,multipart=multipart),True);return
        def reject_current_write(missing=False,**options):
            if versioned:
                failed(write(bucket,p,multipart=multipart,**options),missing)
            else:
                response=write(bucket,p,b"must-not-commit",multipart=multipart,**options)
                expected=None if missing else b"abc" if multipart else b""
                check_rejected_mutation(bucket,"obj",response,404 if missing else 412,"NoSuchKey" if missing else "PreconditionFailed",expected,operation="CompleteMultipartUpload" if multipart else "PutObject")
        etag=accepted(write(bucket,p,none="*",multipart=multipart))["etag"]
        reject_current_write(none="*")
        result=accepted(write(bucket,p,match=etag,multipart=multipart))
        if multipart:etag=result["etag"]
        removed=accepted(delete(bucket))
        if mode=="marker":assert_equal(bool(removed["delete_marker"]),True)
        reject_current_write(missing=True,match='"00000000000000000000000000000000"')
        reject_current_write(missing=True,match='"00000000000000000000000000000000"')
        result=accepted(write(bucket,p,none="*",multipart=multipart),mode=="basic")
        if mode=="marker":return
        if multipart:etag=result["etag"]
        result=accepted(write(bucket,p,match=etag,multipart=multipart),True)
        if multipart:etag=result["etag"]
        reject_current_write(match='"00000000000000000000000000000000"')
        accepted(write(bucket,p,match=etag,multipart=multipart),True)


def delete_match_plain(p):
    """Verify conditional deletion separately from ordinary idempotent DELETE, including actual state after both accepted and rejected requests."""
    with BucketScope(p) as bucket:
        written=accepted(write(bucket,p,b"conditional-delete-content"))
        result=delete(bucket,match='"00000000000000000000000000000000"')
        check_rejected_mutation(bucket,"obj",result,412,"PreconditionFailed",b"conditional-delete-content",operation="DeleteObject")
        removed=accepted(delete(bucket,match=written["etag"]))
        assert_equal(removed["status_code"],204)
        rejected(bucket.raw("get_http",key="obj",output_path=str(p.ctx.output)),404,"NoSuchKey")
        for value in ("*",'"00000000000000000000000000000000"'):
            result=delete(bucket,match=value)
            check_rejected_mutation(bucket,"obj",result,404,"NoSuchKey",operation="DeleteObject")
        accepted(write(bucket,p,b"wildcard-delete-content"))
        assert_equal(accepted(delete(bucket,match="*"))["status_code"],204)
        rejected(bucket.raw("get_http",key="obj",output_path=str(p.ctx.output)),404,"NoSuchKey")


def delete_match(p,mode):
    """Keep source current-version marker semantics and its saved-version deletion even where the source comment incorrectly calls that ID a marker."""
    if mode=="plain":return delete_match_plain(p)
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        if mode=="current":
            for value in ("*","badetag"):assert_equal(accepted(delete(bucket,match=value))["status_code"],204)
        written=accepted(write(bucket,p));etag=written["etag"]
        version=written["version_id"] if mode!="plain" else None
        failed(delete(bucket,match="badetag",version=version if mode=="version" else None))
        removed=accepted(delete(bucket,match=etag,version=version if mode=="version" else None))
        if mode=="current":assert_equal(bool(removed["delete_marker"]),True)
        if mode=="version":assert_equal("delete_marker" in removed,False)
        if mode=="current":
            accepted(delete(bucket,match="*"));failed(delete(bucket,match="badetag"))
            accepted(delete(bucket,version=version));accepted(delete(bucket,match="*"));return
        for value in ("*","badetag"):
            result=accepted(delete(bucket,match=value,version=version if mode=="version" else None))
            if mode=="plain":assert_equal(result["status_code"],204)
        written=accepted(write(bucket,p))
        accepted(delete(bucket,match="*",version=written["version_id"] if mode=="version" else None))


def conditional_value(bucket,kind,version=None):
    if kind=="size":return 0,9999
    head=bucket.call("head_version" if version is not None else "head_http",key="obj",**({"version_id":version} if version is not None else {}))
    return head["last_modified_ms"],BAD_MTIME_MS


def delete_attribute(p,kind,mode):
    """Preserve exact 2015 UTC/9999 bad conditions, zero-size good conditions and source marker truth versus field-presence assertions."""
    raise CaseNotRun("Size/time delete conditions require directory-bucket semantics; this runner has no verified directory-bucket fixture", "directory-bucket fixture and endpoint")

def delete_many(bucket,kind,value,version=None):
    """Send genuine typed per-object conditions, with actual version IDs kept inside the SDK's DeleteObjects XML model."""
    row={"key":"obj",{"match":"etag","mtime":"last_modified_ms","size":"size"}[kind]:value}
    if version is not None:row["version_id"]=version
    return bucket.call("delete_many_versions",objects=[row])


def check_bulk_condition_result(result, key, rejected):
    """Require exactly the requested per-object outcome; an empty Error array is a semantic failure, never IndexError."""
    errors, deleted = result.get("errors"), result.get("deleted")
    expected = {"key": key, "code": "PreconditionFailed"} if rejected else {"key": key}
    actual = errors if rejected else deleted
    other = deleted if rejected else errors
    valid = (isinstance(actual, list) and len(actual) == 1 and other == []
             and isinstance(actual[0], dict) and all(actual[0].get(k) == v for k, v in expected.items()))
    if not valid:
        error = AssertionError("conditional DeleteObjects must return exactly one " + ("PreconditionFailed error" if rejected else "deleted object") + "; actual errors=" + repr(errors) + ", deleted=" + repr(deleted))
        error.failure_category = "assertion_mismatch"
        error.evidence = {"operation": "DeleteObjects", "expected_object_result": expected,
                          "errors": errors, "deleted": deleted, "status_code": result.get("status_code")}
        raise error
    return actual[0]


def delete_many_attribute(p,kind,mode):
    """Keep original error-array and deleted-array semantics, historical version time checks and the single source extra current version for LastModifiedTime only."""
    if kind!="match":raise CaseNotRun("Size/time bulk-delete conditions require a verified directory-bucket fixture", "directory-bucket fixture and endpoint")
    with (BucketScope(p) if mode=="plain" else VersionScope(p)) as bucket:
        if mode!="plain":configure(bucket,"Enabled")
        written=accepted(write(bucket,p));version=written["version_id"] if mode=="version" else None
        if kind=="match":good,bad=written["etag"],"badetag"
        else:good,bad=conditional_value(bucket,kind,version)
        if kind=="mtime" and mode=="version":accepted(write(bucket,p))
        result=delete_many(bucket,kind,bad,version);check_bulk_condition_result(result,bucket.key("obj"),True)
        result=delete_many(bucket,kind,good,version);check_bulk_condition_result(result,bucket.key("obj"),False)
        if mode=="current":
            assert_equal(bool(result["deleted"][0]["delete_marker"]),True)
            result=delete_many(bucket,kind,bad);check_bulk_condition_result(result,bucket.key("obj"),True)
        else:
            if mode=="version" and kind=="match":assert_equal("delete_marker" in result["deleted"][0],False)
            assert_equal(delete_many(bucket,kind,bad,version)["status_code"],200)


V=VERSION_SCOPE_OPERATIONS+" versioning_put versioning_get"
S=SCOPE_OPERATIONS
CASES=()
for multipart in (False,True):
    for mode,reference in (("basic","test_"+("multipart_" if multipart else "")+"put_object_if_match"),
        ("current_none","test_"+("multipart_" if multipart else "")+"put_current_object_if_none_match"),
        ("current_match","test_"+("multipart_" if multipart else "")+"put_current_object_if_match")):
        operations=(S+" get_http" if mode=="basic" else V)+" delete_version wire_headers"+(" mp_create_metadata mp_part mp_complete_observed mp_abort_raw" if multipart else " put_version")
        CASES+=(case("ceph_conditional_versions_"+("multipart_" if multipart else "put_")+mode,"Ceph 条件写入版本矩阵 "+mode,"验证实际ETag/版本、标准If-None-Match:*、404/412和当前版本规则",operations,lambda p,mode=mode,multipart=multipart:put_matrix(p,mode,multipart),reference,CEPH),)
CASES+=(case("ceph_conditional_versions_put_marker","Ceph 删除标记后的条件写入","真实DeleteMarker及标记后缺对象404，历史ETag继续用于IfNoneMatch",V+" put_version delete_version wire_headers",lambda p:put_matrix(p,"marker"),"test_put_object_current_if_match",CEPH),)
for mode in ("plain","current","version"):
    suffix="_"+mode if mode!="plain" else ""
    scope=S if mode=="plain" else V
    refmode=mode+"_" if mode!="plain" else ""
    CASES+=(case("ceph_conditional_versions_delete_match"+suffix,"Ceph 条件单对象删除 ETag "+mode,"普通对象条件删除验证拒绝/成功与读回；版本场景独立验证标记和版本字段",scope+" put_version delete_version wire_headers"+(" get_http" if mode=="plain" else ""),lambda p,mode=mode:delete_match(p,mode),"test_delete_object_"+refmode+"if_match",CEPH),)
    for kind,refkind in (("mtime","last_modified_time"),("size","size")):
        operations=scope+" put_version delete_version wire_headers"+((" head_version" if mode=="version" else " head_http") if kind=="mtime" else "")
        CASES+=(case("ceph_conditional_versions_delete_"+kind+suffix,"Ceph 条件单对象删除 "+kind+" "+mode,"原始UTC时间/零大小与9999、精确412/204、历史版本和标记断言",operations,lambda p,kind=kind,mode=mode:delete_attribute(p,kind,mode),"test_delete_object_"+refmode+"if_match_"+refkind,CEPH),)
    for kind,refkind in (("match",""),("mtime","_last_modified_time"),("size","_size")):
        operations=scope+" put_version delete_many_versions delete_many_conditions"+((" head_version" if mode=="version" else " head_http") if kind=="mtime" else "")
        CASES+=(case("ceph_conditional_versions_bulk_"+kind+suffix,"Ceph 条件批量删除 "+kind+" "+mode,"原始对象XML条件、Errors与Deleted精确条目、历史版本/标记和服务状态",operations,lambda p,kind=kind,mode=mode:delete_many_attribute(p,kind,mode),"test_delete_objects_"+refmode+"if_match"+refkind,CEPH),)


def register(registry):
    registry.add_suite(Suite("ceph_conditional_versions",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
