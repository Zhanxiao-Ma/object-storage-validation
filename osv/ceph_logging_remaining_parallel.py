"""Original parallel logging source workloads, backed by actual shared-client SDK futures rather than separate workers."""
from . import ceph_logging_remaining_parallel_sources as sources
from .ceph_logging_remaining_parallel_runtime import AsyncRuntime
from .ceph_logging_remaining import BASE
from .scenarios import Suite
from .suites import case,run_cases,CEPH

SPECS=(('concurrent_flush_j', 16431), ('concurrent_flush_s', 16439), ('concurrent_flush_j_single', 16446), ('concurrent_flush_s_single', 16454), ('put_and_flush', 16461), ('cleanup_bucket_concurrent_deletion_j', 18547), ('cleanup_bucket_concurrent_deletion_j_single', 18554), ('cleanup_concurrent_disabling_j', 18561), ('cleanup_concurrent_disabling_j_single', 18568), ('cleanup_concurrent_updating_j', 18575), ('cleanup_concurrent_updating_j_single', 18582), ('cleanup_bucket_concurrent_deletion_s', 18589), ('cleanup_bucket_concurrent_deletion_s_single', 18596), ('cleanup_concurrent_disabling_s', 18603), ('cleanup_concurrent_disabling_s_single', 18610), ('cleanup_concurrent_updating_s', 18617), ('cleanup_concurrent_updating_s_single', 18624), ('part_cleanup_concurrent_updating_s', 18763), ('part_cleanup_concurrent_disabling_s', 18770), ('part_cleanup_concurrent_deletion_s', 18777), ('part_cleanup_concurrent_updating_j', 18784), ('part_cleanup_concurrent_disabling_j', 18791), ('part_cleanup_concurrent_deletion_j', 18798), ('conf_concurrent_updating_roll_s', 19013), ('conf_concurrent_updating_roll_j', 19020), ('conf_concurrent_updating_pfx_s', 19027), ('conf_concurrent_updating_pfx_j', 19034))

def execute(p,suffix):
    with AsyncRuntime(p,require_pool100=suffix=='put_and_flush'):
        getattr(sources,'test_bucket_logging_'+suffix)()
    return {'request_transport':'official_sdk','protocol_adapter':'ceph_logging_wire_extensions','concurrency':'original Python threads submit actual bounded futures on the same persistent SDK client; real task IDs retain each response/capture'}

CASES=tuple(case('ceph_logging_parallel_'+suffix,'Ceph共享SDK并发日志 '+suffix,'原线程/负载/断言，真实同client异步RPC及原始响应，不创建独立SDK client',BASE+' ceph_logging_wire_extensions async_requests',lambda p,s=suffix:execute(p,s),'test_bucket_logging_'+suffix,CEPH) for suffix,line in SPECS)

def register(registry):registry.add_suite(Suite('ceph_logging_remaining_parallel',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
