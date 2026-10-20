"""Static pinned Ceph logging source bodies bound to actual SDK clients by logging_remaining_runtime.

Source: https://github.com/ceph/s3-tests/blob/5b3987116eca0b4b8fa57d6b10f3db57bab196b6/s3tests/functional/test_s3.py
No upstream test runner is imported or executed. Original helpers, waits, strings and assertions are retained.

Copyright (c) 2011 New Dream Network, LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""
import threading
from .ceph_logging_remaining_runtime import get_new_bucket_name,get_new_bucket_resource
from .ceph_logging_remaining_parallel_runtime import get_client,botocore
from .ceph_logging_remaining_sources import _has_bucket_logging_extension,_set_log_bucket_policy,_flush_logs,_get_keys,_get_body,_verify_records,randcontent,logger
from .ceph_logging_remaining_extension_sources import pytest,_bucket_logging_flush,_bucket_logging_cleanup,_bucket_logging_partial_cleanup,_bucket_logging_conf_update,_do_wait_completion

def test_bucket_logging_concurrent_flush_j():
    """Verify five-source Journal concurrent POST flushes with original key-count rounds [10, 100, 0] and separate source prefixes; compare actual flushed-object keys and every original PUT/DELETE log count."""
    _bucket_logging_flush('Journal', False, True, 10)
    _bucket_logging_flush('Journal', False, True, 100)
    _bucket_logging_flush('Journal', False, True, 0)

def test_bucket_logging_concurrent_flush_s():
    """Verify five-source Standard concurrent POST flushes with original key-count rounds [10, 100] and separate source prefixes; compare actual flushed-object keys and every original PUT/DELETE log count."""
    _bucket_logging_flush('Standard', False, True, 10)
    _bucket_logging_flush('Standard', False, True, 100)

def test_bucket_logging_concurrent_flush_j_single():
    """Verify five-source Journal concurrent POST flushes with original key-count rounds [10, 100, 0] and one shared prefix; compare actual flushed-object keys and every original PUT/DELETE log count."""
    _bucket_logging_flush('Journal', True, True, 10)
    _bucket_logging_flush('Journal', True, True, 100)
    _bucket_logging_flush('Journal', True, True, 0)

def test_bucket_logging_concurrent_flush_s_single():
    """Verify five-source Standard concurrent POST flushes with original key-count rounds [10, 100] and one shared prefix; compare actual flushed-object keys and every original PUT/DELETE log count."""
    _bucket_logging_flush('Standard', True, True, 10)
    _bucket_logging_flush('Standard', True, True, 100)

def test_bucket_logging_put_and_flush():
    """Verify 300 original PUT threads and30 concurrent flush threads plus final synchronous POST on actual pool100; require sorted returned log keys and exactly300 Journal PUT records."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client_config = botocore.config.Config(max_pool_connections=100)
    client = get_client(client_config=client_config)

    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_prefix = 'log/'
    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [log_prefix])

    logging_enabled = {'TargetBucket': log_bucket_name,
                       'LoggingType': 'Journal',
                       'TargetPrefix': log_prefix
                       }

    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    num_keys = 300
    flush_rate = 10
    put_threads = []
    flush_threads = []
    src_names = []
    for j in range(num_keys):
        name = 'myobject'+str(j)
        src_names.append(name)
        put_thr = threading.Thread(target=client.put_object,
                                      kwargs={'Bucket': src_bucket_name,
                                              'Key': name,
                                              'Body': randcontent()})
        put_thr.start()
        put_threads.append(put_thr)

        if j % flush_rate == 0:
            flush_thr = threading.Thread(target = client.post_bucket_logging,
                                         kwargs={'Bucket': src_bucket_name})
            flush_thr.start()
            flush_threads.append(flush_thr)

    _do_wait_completion(put_threads)
    _do_wait_completion(flush_threads)

    # making sure everything is flushed synchronously
    _flush_logs(client, src_bucket_name)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)

    body = ''
    prev_key = ''
    for key in keys:
        logger.info('bucket log record: %s', key)
        assert key > prev_key
        prev_key = key
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)
    assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_names, 'Journal', num_keys)

def test_bucket_logging_cleanup_bucket_concurrent_deletion_j():
    """Verify concurrent deletion of five Journal sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('deletion', 'Journal', False, True)

def test_bucket_logging_cleanup_bucket_concurrent_deletion_j_single():
    """Verify concurrent deletion of five Journal sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('deletion', 'Journal', True, True)

def test_bucket_logging_cleanup_concurrent_disabling_j():
    """Verify concurrent disabling of five Journal sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('disabling', 'Journal', False, True)

def test_bucket_logging_cleanup_concurrent_disabling_j_single():
    """Verify concurrent disabling of five Journal sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('disabling', 'Journal', True, True)

def test_bucket_logging_cleanup_concurrent_updating_j():
    """Verify concurrent updating of five Journal sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('updating', 'Journal', False, True)

def test_bucket_logging_cleanup_concurrent_updating_j_single():
    """Verify concurrent updating of five Journal sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('updating', 'Journal', True, True)

def test_bucket_logging_cleanup_bucket_concurrent_deletion_s():
    """Verify concurrent deletion of five Standard sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('deletion', 'Standard', False, True)

def test_bucket_logging_cleanup_bucket_concurrent_deletion_s_single():
    """Verify concurrent deletion of five Standard sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('deletion', 'Standard', True, True)

def test_bucket_logging_cleanup_concurrent_disabling_s():
    """Verify concurrent disabling of five Standard sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('disabling', 'Standard', False, True)

def test_bucket_logging_cleanup_concurrent_disabling_s_single():
    """Verify concurrent disabling of five Standard sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('disabling', 'Standard', True, True)

def test_bucket_logging_cleanup_concurrent_updating_s():
    """Verify concurrent updating of five Standard sources after ten PUT/DELETE pairs with separate prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('updating', 'Standard', False, True)

def test_bucket_logging_cleanup_concurrent_updating_s_single():
    """Verify concurrent updating of five Standard sources after ten PUT/DELETE pairs with shared prefixes; retain original 1..5 file bound and exact record counts for every source."""
    _bucket_logging_cleanup('updating', 'Standard', True, True)

def test_bucket_logging_part_cleanup_concurrent_updating_s():
    """Verify Standard concurrent updating of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('updating', 'Standard', True)

def test_bucket_logging_part_cleanup_concurrent_disabling_s():
    """Verify Standard concurrent disabling of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('disabling', 'Standard', True)

def test_bucket_logging_part_cleanup_concurrent_deletion_s():
    """Verify Standard concurrent deletion of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('deletion', 'Standard', True)

def test_bucket_logging_part_cleanup_concurrent_updating_j():
    """Verify Journal concurrent updating of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('updating', 'Journal', True)

def test_bucket_logging_part_cleanup_concurrent_disabling_j():
    """Verify Journal concurrent disabling of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('disabling', 'Journal', True)

def test_bucket_logging_part_cleanup_concurrent_deletion_j():
    """Verify Journal concurrent deletion of the first two of three sources after three PUT/DELETE pairs, then repeat final-source writes and flush; retain original 1..3 files and unasserted record booleans."""
    _bucket_logging_partial_cleanup('deletion', 'Journal', True)

def test_bucket_logging_conf_concurrent_updating_roll_s():
    """Verify Standard roll_time changes while100 PUT threads remain active, actual three-file flush membership, then100 further serial PUTs and one final log; preserve source-specific warning-only errors and omitted Standard-prefix first assertion."""
    _bucket_logging_conf_update('Standard', 'roll_time', True)

def test_bucket_logging_conf_concurrent_updating_roll_j():
    """Verify Journal roll_time changes while100 PUT threads remain active, actual three-file flush membership, then100 further serial PUTs and one final log; preserve source-specific warning-only errors and omitted Standard-prefix first assertion."""
    _bucket_logging_conf_update('Journal', 'roll_time', True)

def test_bucket_logging_conf_concurrent_updating_pfx_s():
    """Verify Standard prefix changes while100 PUT threads remain active, actual three-file flush membership, then100 further serial PUTs and one final log; preserve source-specific warning-only errors and omitted Standard-prefix first assertion."""
    _bucket_logging_conf_update('Standard', 'prefix', True)

def test_bucket_logging_conf_concurrent_updating_pfx_j():
    """Verify Journal prefix changes while100 PUT threads remain active, actual three-file flush membership, then100 further serial PUTs and one final log; preserve source-specific warning-only errors and omitted Standard-prefix first assertion."""
    _bucket_logging_conf_update('Journal', 'prefix', True)

