"""Original atomic stream callbacks execute within actual SDK transfer, never as a completed-request batch."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_versioning import configure
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def read(p,size):
    with BucketScope(p) as bucket:
        bucket.written_keys.add("testobj")
        result=bucket.call("atomic_stream",mode="read",key="testobj",size=size)
        assert_equal(result["read_size"],size);assert_equal(result["final_size"],size)
        assert_equal(result["callbacks"],1)
        return {"source_size":size,"real_sdk_sink_writes":result["read_writes"],"real_first_write_callbacks":result["callbacks"],"read_client":"independent","callback_put_client":"original"}

CASES=tuple(case("ceph_atomic_stream_read_"+str(mib)+"mb","Ceph 原子流读取 "+str(mib)+"MiB","保留A首个正sink.write内原client写B、下载未结束时每chunk仍A、close全size和fresh客户端最终B；使用真实SDK流回调，非完成下载后补写",SCOPE_OPERATIONS+" atomic_stream_read",lambda p,size=mib*1024*1024:read(p,size),"test_atomic_read_"+str(mib)+"mb",CEPH) for mib in (1,4,8))

def upload(p,size,mode):
    with BucketScope(p) as bucket:
        bucket.written_keys.add("testobj")
        result=bucket.call("atomic_stream",mode=mode,key="testobj",size=size)
        assert_equal(result["final_size"],size)
        return {"source_size":size,"actual_positive_eof_callbacks":result["eof_callbacks"],"callback_timing":"before returning final positive bytes, including real checksum/signing rereads","nested_client":"same original client" if mode=="dual_write" else "fresh verifier per callback"}

CASES+=tuple(case("ceph_atomic_stream_"+mode+"_"+str(mib)+"mb","Ceph 原子EOF回调 "+mode+" "+str(mib)+"MiB","保留原A/空对象初始化、每次真实正EOF read返回前回调、同client nested A写入或fresh client A读校验、最终fresh全B校验；conditional保留If-Match:*且不添加原文没有的初次A读",SCOPE_OPERATIONS+" atomic_stream_"+mode,lambda p,size=mib*1024*1024,mode=mode:upload(p,size,mode),"test_atomic_"+mode+"_"+str(mib)+"mb",CEPH) for mode in ("write","dual_write","conditional_write") for mib in ((1,) if mode=="conditional_write" else (1,4,8)))

class AtomicMultipartScope(BucketScope):
    """Resolve even ambiguous internal Create outcomes with actual MPU listings confined to this proven owned UUID bucket, then abort real IDs before ordinary object cleanup."""
    def cleanup_objects(self):
        previous=None
        for attempt in range(10000):
            listing=self.call("mp_list",max_uploads=1000)
            rows=listing["uploads"]
            if not rows:
                assert_equal(listing.get("truncated"),False)
                return super().cleanup_objects()
            signature=tuple((row["key"],row["upload_id"]) for row in rows)
            if signature==previous:raise RuntimeError("owned atomic multipart cleanup did not advance")
            previous=signature
            for row in rows:
                result=self.raw("mp_abort_raw",key=row["key"],upload_id=row["upload_id"])
                if not result["ok"] and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):raise RuntimeError("actual owned atomic MPU abort failed: "+str(result))
        raise RuntimeError("owned atomic multipart cleanup exceeded safety bound")

def multipart(p):
    with AtomicMultipartScope(p) as bucket:
        bucket.written_keys.add("mymultipart")
        result=bucket.call("atomic_stream",mode="multipart_resend",key="mymultipart",size=8)
        assert_equal(result["final_size"],8)
        # Original source does not assert equal dry/A counts or that nested B
        # fired. Preserve that limit and report actual calibration as evidence.
        return {key:result[key] for key in ("dry_eof_reads","outer_eof_reads","nested_eof_reads","nested_actions","completed_entries","final_size")}

CASES+=(case("ceph_atomic_stream_multipart_resend","Ceph 分块重传真实EOF校准","保留原8字节C dry-run并完成/删除、实际新UploadId、A正EOF次数等于dry校准时才同client上传B、真实B/A ETag strip外引号且PartNumber均1顺序保留、最终fresh全8字节A；不声称已知最后网络读取或额外要求B必触发",SCOPE_OPERATIONS+" atomic_stream_multipart_resend mp_list mp_abort_raw",multipart,"test_multipart_resend_first_finishes_last",CEPH),)

def dual_conditional(p):
    with BucketScope(p) as bucket:
        bucket.written_keys.add("testobj")
        result=bucket.call("atomic_stream",mode="dual_conditional_write",key="testobj",size=1024*1024)
        assert_equal(result["observed_error_status"],412);assert_equal(result["observed_error_code"],"PreconditionFailed");assert_equal(result["final_size"],1024*1024)
        return {key:result[key] for key in ("eof_callbacks","observed_error_status","observed_error_code","error_origin","final_size")}

def bucket_gone(p):
    with BucketScope(p) as bucket:
        bucket.written_keys.add("foo")
        result=bucket.call("atomic_stream",mode="bucket_gone",key="foo",size=1024*1024)
        assert_equal(result["observed_error_status"],404);assert_equal(result["observed_error_code"],"NoSuchBucket")
        return {key:result[key] for key in ("eof_callbacks","observed_error_status","observed_error_code","error_origin")}

def versioned_overlap(p):
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        bucket.written_keys.add("myobj")
        result=bucket.call("atomic_stream",mode="versioned_overlap",key="myobj",size=1)
        assert_equal(result["versions_present"],False)
        return {key:result[key] for key in ("source_put_threads","actual_delete_group_sizes","thread_errors","versions_present")}

CASES+=(case("ceph_atomic_stream_dual_conditional_write","Ceph 同client双条件EOF写","保留初始1MiB A真实ETag去除所有引号、fresh A全验、同client B/C共享真实If-Match、每次C正EOF返回前B PUT、实际412/PreconditionFailed及fresh最终全B；不伪造错误来源",SCOPE_OPERATIONS+" atomic_stream_dual_conditional_write",dual_conditional,"test_atomic_dual_conditional_write_1mb",CEPH),
        case("ceph_atomic_stream_bucket_gone","Ceph 上传EOF回调删除桶","保留空桶/foo/1MiB A、每个正EOF回调同client DeleteBucket、实际404/NoSuchBucket；允许源捕获异常来自重复nested DELETE并明确错误来源，不冒称外PUT拒绝",SCOPE_OPERATIONS+" atomic_stream_bucket_gone",bucket_gone,"test_atomic_write_bucket_gone",CEPH),
        case("ceph_atomic_versions_overlap","Ceph 同client动态版本创建删除重叠","保留三轮各start三个data0/1/2 PUT后立即真实List并按opaque VersionId启动删除，三轮后统一join、最终实际List/删除/join再List断言Versions缺失；原后台线程异常仅记录证据，finally join后按实际历史版本清理",VERSION_SCOPE_OPERATIONS+" versioning_put versioning_get atomic_versions_overlap",versioned_overlap,"test_versioned_concurrent_object_create_and_remove",CEPH))

def register(registry):
    registry.add_suite(Suite("ceph_atomic_streams",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
