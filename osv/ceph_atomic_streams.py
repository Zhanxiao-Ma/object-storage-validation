"""Original atomic stream callbacks execute within actual SDK transfer, never as a completed-request batch."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
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

def register(registry):
    registry.add_suite(Suite("ceph_atomic_streams",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
