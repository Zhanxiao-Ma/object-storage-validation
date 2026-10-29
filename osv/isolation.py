"""Separate fixture bucket lifecycle from the operation actually under test."""

FIXTURE_OPERATIONS = frozenset({'bucket_head', 'bucket_create_raw', 'bucket_delete'})


def bucket_required(name, operations):
    """Keep bucket-state and account contracts intact; ordinary object fixtures need only a prefix."""
    operations = set(operations)
    special = ('ceph_bucket_', 'ceph_names_', 'ceph_header_bucket_', 'ceph_state_recreate_',
               'ceph_basic_missing_', 'ceph_basic_literal_')
    if name.startswith(special) and name not in {'ceph_bucket_missing_key','ceph_bucket_head'}:
        return True
    if name in {'bucket_create_idempotent', 'ceph_atomic_stream_bucket_gone',
                'ceph_metadata_edges_recreate','ceph_raw_post_no_key',
                'ceph_select_json_column_sum_min_max','ceph_select_column_sum_min_max'}:
        return True
    for op in operations - FIXTURE_OPERATIONS:
        if (op.startswith('bucket_') or op in {'versioning_get', 'versioning_put'}
                or op.startswith(('api.GetBucket', 'api.PutBucket', 'api.DeleteBucket',
                                  'api.GetPublicAccessBlock', 'api.PutPublicAccessBlock',
                                  'api.DeletePublicAccessBlock', 'api.GetObjectLockConfiguration',
                                  'api.PutObjectLockConfiguration'))):
            return True
    return False
