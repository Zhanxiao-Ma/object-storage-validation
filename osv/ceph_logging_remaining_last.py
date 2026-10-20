"""Complete final tenant Journal and locked-object metadata logging sources using actual configured SDK identities and cleanup proof."""
from . import ceph_logging_remaining_last_sources as sources
from .ceph_logging_remaining_last_runtime import LastRuntime
from .ceph_logging_remaining import BASE
from .scenarios import Suite
from .suites import case,run_cases,CEPH

def execute(p,mode):
    with LastRuntime(p,mode):getattr(sources,'test_put_bucket_logging_tenant_j' if mode=='tenant' else 'test_bucket_logging_object_meta')()
    return {'request_transport':'official_sdk','protocol_adapter':'ceph_logging_wire_extensions','owned_cleanup':'real namespace identities; actual same-key governance bypass prerequisite before original60-day retention','request_extensions':(['wire_query.versionId for typed tags operations','wire_headers.x-amz-bypass-governance-retention for version deletion'] if mode=='metadata' else [])}

CASES=(case('ceph_logging_remaining_tenant_j','Ceph Journal七组tenant','原七namespace完整向量、两次NoSuchKey、同名不同身份及五次日志全断言',BASE+' ceph_logging_wire_extensions identity_tenant',lambda p:execute(p,'tenant'),'test_put_bucket_logging_tenant_j',CEPH),case('ceph_logging_remaining_object_meta','Ceph锁定对象日志元数据','原版本ID→ACL/tag/hold/60天治理保留→六事件及版本号断言；真实同key短保留绕过删除证明先行',BASE+' ceph_logging_wire_extensions bucket_create_locked put_version versions_list delete_version wire_headers wire_query tags_put tags_delete api.PutObjectAcl api.PutObjectLegalHold api.PutObjectRetention',lambda p:execute(p,'metadata'),'test_bucket_logging_object_meta',CEPH))

def register(registry):registry.add_suite(Suite('ceph_logging_remaining_last',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
