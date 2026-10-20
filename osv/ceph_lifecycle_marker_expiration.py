"""Preserve Ceph's separate delete-marker expiration HEAD-header contract."""
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_versioning import configure
from .ceph_state_boundaries import header
from .ceph_lifecycle_expiration import interval,wait,lifecycle
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def check_marker(bucket,key,expected):
    actual="none"
    response=bucket.raw("head_version",key=key)
    if not response["ok"]:actual=header(response,"x-amz-delete-marker")
    assert_equal(actual,expected)

def expired(p):
    clock=interval(p)
    with VersionScope(p) as bucket:
        key="nugent.ted";configure(bucket,"Enabled")
        bucket.put(key,("body version "+key).encode());bucket.call("delete_version",key=key)
        check_marker(bucket,key,"true")
        lifecycle(bucket,[{"Expiration":{"ExpiredObjectDeleteMarker":True},"ID":"dm-1-days","Prefix":"","Status":"Enabled"},{"ID":"noncur-1-days","Prefix":"","Status":"Enabled","NoncurrentVersionExpiration":{"NoncurrentDays":1}}],True)
        wait(6*clock);check_marker(bucket,key,"false")

CASES=(case("ceph_lc_delete_marker_expiration","Ceph 删除标记过期HEAD头","原始true→6倍服务加速间隔→false字符串头；先过期旧版本再标记，保留源成功HEAD视none与缺头错误",VERSION_SCOPE_OPERATIONS+" put versioning_put versioning_get delete_version head_version api.PutBucketLifecycleConfiguration lifecycle_legacy_prefix",expired,"test_delete_marker_expiration",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_lifecycle_marker_expiration",CASES[0].operations,lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
