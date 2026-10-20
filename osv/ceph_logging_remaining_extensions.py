"""Explicit Ceph protocol-adapter cases; these never claim stock AWS SDK support for logging extensions."""
from . import ceph_logging_remaining_extension_sources as sources
from .ceph_logging_remaining_extension_runtime import ExtensionRuntime
from .ceph_logging_remaining import BASE
from .scenarios import Suite
from .suites import case,run_cases,CEPH

SPECS=(('key_filter_s',16280),('key_filter_j',16288),('flush_empty',16392),('flush_j',16399),('flush_s',16408),('flush_j_single',16417),('flush_s_single',16424))

CLEANUP_SPECS=(('cleanup_bucket_deletion_j', 18407, 'deletion', 'Journal', False, False), ('cleanup_bucket_deletion_j_single', 18414, 'deletion', 'Journal', True, False), ('cleanup_disabling_j', 18421, 'disabling', 'Journal', False, False), ('cleanup_disabling_j_single', 18428, 'disabling', 'Journal', True, False), ('cleanup_updating_j', 18435, 'updating', 'Journal', False, False), ('cleanup_updating_j_single', 18442, 'updating', 'Journal', True, False), ('notupdating_j', 18449, 'notupdating', 'Journal', False, False), ('notupdating_j_single', 18456, 'notupdating', 'Journal', True, False), ('cleanup_bucket_deletion_s', 18463, 'deletion', 'Standard', False, False), ('cleanup_bucket_deletion_s_single', 18470, 'deletion', 'Standard', True, False), ('cleanup_disabling_s', 18477, 'disabling', 'Standard', False, False), ('cleanup_disabling_s_single', 18484, 'disabling', 'Standard', True, False), ('cleanup_updating_s', 18491, 'updating', 'Standard', False, False), ('cleanup_updating_s_single', 18498, 'updating', 'Standard', True, False), ('notupdating_s', 18505, 'notupdating', 'Standard', False, False), ('notupdating_s_single', 18512, 'notupdating', 'Standard', True, False), ('target_cleanup_j', 18519, 'target', 'Journal', False, False), ('target_cleanup_j_single', 18526, 'target', 'Journal', True, False), ('target_cleanup_s', 18533, 'target', 'Standard', False, False), ('target_cleanup_s_single', 18540, 'target', 'Standard', True, False))
SPECS += tuple((suffix,line) for suffix,line,*parameters in CLEANUP_SPECS)

PARTIAL_CONF_SPECS=(('part_cleanup_updating_s', 18805, 'updating', 'Standard', False), ('part_cleanup_disabling_s', 18812, 'disabling', 'Standard', False), ('part_cleanup_deletion_s', 18819, 'deletion', 'Standard', False), ('part_cleanup_updating_j', 18826, 'updating', 'Journal', False), ('part_cleanup_disabling_j', 18833, 'disabling', 'Journal', False), ('part_cleanup_deletion_j', 18840, 'deletion', 'Journal', False), ('conf_updating_roll_s', 18985, 'Standard', 'roll_time', False), ('conf_updating_roll_j', 18992, 'Journal', 'roll_time', False), ('conf_updating_pfx_s', 18999, 'Standard', 'prefix', False), ('conf_updating_pfx_j', 19006, 'Journal', 'prefix', False))
SPECS += tuple((suffix,line) for suffix,line,*parameters in PARTIAL_CONF_SPECS)

SPECS += (('event_type_j',17976),('event_type_s',17984),('roll_time',17992),('account_j',17229))

def execute(p,suffix):
    with ExtensionRuntime(p,'account' if suffix=='account_j' else 'extension'):
        getattr(sources,('test_put_bucket_logging_account_j' if suffix=='account_j' else 'test_bucket_logging_'+suffix))()
    return {'request_transport':'official_sdk','protocol_adapter':'ceph_logging_wire_extensions','source_semantics':'original logging extension fields, actual captured XML, original record and returned flushed-key assertions'}

CASES=tuple(case('ceph_logging_remaining_'+suffix,'Ceph显式日志扩展 '+suffix,'保留原函数全部输入/断言；显式启用官方SDK公共hook协议适配；不代表stock AWS SDK原生支持',BASE+' head_version ceph_logging_wire_extensions'+(' identity_iam_root api.iam.GetUser' if suffix=='account_j' else ''),lambda p,s=suffix:execute(p,s),('test_put_bucket_logging_account_j' if suffix=='account_j' else 'test_bucket_logging_'+suffix),CEPH) for suffix,line in SPECS)

def register(registry):registry.add_suite(Suite('ceph_logging_remaining_extensions',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
