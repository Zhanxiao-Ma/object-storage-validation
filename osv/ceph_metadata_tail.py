"""Preserve remaining grant-header ordering and unreadable-key source contracts."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_request_headers import creation
from .ceph_acl import actor,grant,check_grants
from .ceph_bucket_config import api,rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH


def header_grants(p):
    """Keep five original unquoted canonical-ID grant headers, exact source ACL helper and genuine alternate writes; restore source cleanup ACL even if an earlier oracle fails."""
    alt=actor(p,'alt')
    permissions=('read','write','read-acp','write-acp','full-control')
    headers={'x-amz-grant-'+permission:'id='+alt['canonical_id'] for permission in permissions}
    with creation(p,headers) as (bucket,response):
        restored=False
        try:
            require_ok(response)
            actual=api(bucket,'GetBucketAcl')['data']['Grants']
            check_grants(actual,[grant(permission,alt) for permission in ('READ','WRITE','READ_ACP','WRITE_ACP','FULL_CONTROL')])
            p.ctx.body.write_bytes(b'bar')
            bucket.call('put_http',key='foo',body_path=str(p.ctx.body),identity='alt')
            bucket.call('api.PutBucketAcl',input={'ACL':'public-read-write'},identity='alt');restored=True
        finally:
            if response.get('ok') and not restored:
                bucket.call('api.PutBucketAcl',input={'ACL':'public-read-write'},identity='alt')
    return {'cleanup_confirmed':bucket.cleanup_completed,'cleanup_acl_identity':'alt'}


def unreadable(p):
    """Retain the original Unicode literal, genuine SDK URI encoding and precise 400/message oracle, without introducing encryption or identity prerequisites."""
    with BucketScope(p) as bucket:
        actual=bucket.raw('get_http',key='\xae\x8a-',output_path=str(p.ctx.output))
        rejected(actual,400);assert_equal(actual['error_message'],"Couldn't parse the specified URI.")


CASES=(case('ceph_tail_bucket_header_grants','Ceph 原始桶grant请求头','五个原始id=值、完整ACL字段/原顺序、真实alt写foo/bar和恢复公开ACL',SCOPE_OPERATIONS+' wire_headers identity_alt put_http api.GetBucketAcl api.PutBucketAcl',header_grants,'test_bucket_header_acl_grants',CEPH),
 case('ceph_tail_unreadable','Ceph 原始不可读对象键','原Unicode字面键必须返回400和精确URI解析错误消息',SCOPE_OPERATIONS+' get_http',unreadable,'test_object_read_unreadable',CEPH))


def register(registry):
    registry.add_suite(Suite('ceph_metadata_tail',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
