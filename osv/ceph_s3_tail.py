"""Preserve remaining ordinary S3 source requests without replacing official SDK transport."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def chunked_write(p):
    """Apply the original before-sign Transfer-Encoding header only to the foo/bar PUT, leaving SDK framing and all cleanup requests authoritative."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b'bar')
        result=bucket.call('put_http',key='foo',body_path=str(p.ctx.body),wire_headers={'Transfer-Encoding':'chunked'})
        assert_equal(result['status_code'],200)
    return {'cleanup_confirmed':bucket.cleanup_completed,'request_transport':'official_sdk','header_phase':'before_sign'}


CASES=(case('ceph_s3_tail_chunked_write','Ceph 原始chunked请求头写入','原始foo/bar PUT在官方签名前增加Transfer-Encoding:chunked，实际状态必须200；保留SDK真实传输行为',SCOPE_OPERATIONS+' put_http wire_headers',chunked_write,'test_object_write_with_chunked_transfer_encoding',CEPH),)


def register(registry):
    registry.add_suite(Suite('ceph_s3_tail',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
