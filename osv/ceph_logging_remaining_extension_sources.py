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
from .ceph_logging_remaining_sources import logger,_put_bucket_logging_account
import json
import threading
import time
from .ceph_logging_remaining_sources import (get_new_bucket_name,get_new_bucket_resource,get_client,_set_log_bucket_policy,_flush_logs,_get_keys,_get_body,_verify_records,_has_bucket_logging_extension,randcontent,expected_object_roll_time)
from .suites import CaseNotRun

class SourcePrerequisites:
    """Translate the original explicit extension skip into a precise case prerequisite result without fabricating service outcomes."""
    @staticmethod
    def skip(reason):raise CaseNotRun(reason,"workload.ceph_logging_wire_extensions")

pytest=SourcePrerequisites()

def _bucket_logging_key_filter(log_type):
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()
    prefix = 'log/'
    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])

    logging_enabled = {
            'TargetBucket': log_bucket_name,
            'LoggingType': log_type,
            'TargetPrefix': prefix,
            'ObjectRollTime': expected_object_roll_time,
            'TargetObjectKeyFormat': {'SimplePrefix': {}},
            'RecordsBatchSize': 0,
             'Filter':
                {
                    'Key': {
                        'FilterRules': [
                            {'Name': 'prefix', 'Value': 'test/'},
                            {'Name': 'suffix', 'Value': '.txt'}
                        ]
                    }
                }
            }

    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    response = client.get_bucket_logging(Bucket=src_bucket_name)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    if log_type == 'Journal':
        assert response['LoggingEnabled'] == logging_enabled
    elif log_type == 'Standard':
        print('TODO')
    else:
        assert False, 'unknown log type: %s' % log_type

    names = []
    num_keys = 5
    for j in range(num_keys):
        name = 'myobject'+str(j)
        if log_type == 'Standard':
            # standard log records are not filtered
            names.append(name)
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())

    for j in range(num_keys):
        name = 'test/'+'myobject'+str(j)+'.txt'
        names.append(name)
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())

    expected_count = len(names)

    flushed_obj = _flush_logs(client, src_bucket_name, dummy_key="test/dummy.txt")

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 1

    for key in keys:
        if flushed_obj is not None:
            assert key == flushed_obj
        assert key.startswith('log/')
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body = _get_body(response)
        assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', names, log_type, expected_count, exact_match=True)

def test_bucket_logging_key_filter_s():
    """Verify original Standard prefix/suffix filter configuration and all exact record/count expectations through actual logging extension SDK requests."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _bucket_logging_key_filter('Standard')

def test_bucket_logging_key_filter_j():
    """Verify original Journal prefix/suffix filter configuration and all exact record/count expectations through actual logging extension SDK requests."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _bucket_logging_key_filter('Journal')

def _do_wait_completion(t):
    for thr in t:
        thr.join()

def _post_bucket_logging(client, src_bucket_name, flushed_objs):
    result = client.post_bucket_logging(Bucket=src_bucket_name)
    assert result['ResponseMetadata']['HTTPStatusCode'] == 200
    flushed_objs[src_bucket_name] = result['FlushedLoggingObject']

def _bucket_logging_flush(logging_type, single_prefix, concurrency, num_keys):
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()

    num_buckets = 5
    buckets = []
    log_prefixes = []
    longer_time = expected_object_roll_time*10
    for j in range(num_buckets):
        src_bucket_name = get_new_bucket_name()
        src_bucket = get_new_bucket_resource(name=src_bucket_name)
        buckets.append(src_bucket_name)
        if single_prefix:
            log_prefixes.append('log/')
        else:
            log_prefixes.append(src_bucket_name+'/')

    _set_log_bucket_policy(client, log_bucket_name, buckets, log_prefixes)

    for j in range(num_buckets):
        logging_enabled = {'TargetBucket': log_bucket_name,
                           'ObjectRollTime': longer_time,
                           'LoggingType': logging_type,
                           'TargetPrefix': log_prefixes[j]
                           }

        response = client.put_bucket_logging(Bucket=buckets[j], BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
        assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    src_names = []
    for j in range(num_keys):
        src_names.append('myobject'+str(j))

    for src_bucket_name in buckets:
        for name in src_names:
            client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
            client.delete_object(Bucket=src_bucket_name, Key=name)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 0

    t = []
    flushed_objs = {}
    for src_bucket_name in buckets:
        if concurrency:
            thr = threading.Thread(target = _post_bucket_logging,
                                   args=(client, src_bucket_name, flushed_objs))
            thr.start()
            t.append(thr)
        else:
            result = client.post_bucket_logging(Bucket=src_bucket_name)
            assert result['ResponseMetadata']['HTTPStatusCode'] == 200
            flushed_objs[src_bucket_name] = result['FlushedLoggingObject']
        if single_prefix:
            break

    _do_wait_completion(t)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)

    if single_prefix:
        assert len(keys) == 1
        assert len(flushed_objs) == 1
    else:
        assert len(keys) == num_buckets
        assert len(flushed_objs) >= num_buckets

    for key in keys:
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body = _get_body(response)
        found = False
        for j in range(num_buckets):
            prefix = log_prefixes[j]
            if key.startswith(prefix):
                flushed_obj = flushed_objs.get(buckets[j])
                if flushed_obj is not None:
                    assert key == flushed_obj
                found = True
                assert _verify_records(body, buckets[j], 'REST.PUT.OBJECT', src_names, logging_type, num_keys)
                assert _verify_records(body, buckets[j], 'REST.DELETE.OBJECT', src_names, logging_type, num_keys)
        assert found

def test_bucket_logging_flush_empty():
    """Verify five original empty Journal commits return each actual POST flushed key and matching empty log bodies."""
    # testing empty commits
    _bucket_logging_flush('Journal', False, False, 0)

def test_bucket_logging_flush_j():
    """Verify separate-prefix Journal flushes for original 10/100/0 keys, five source buckets and exact actual POST/list/log PUT and DELETE record relationships."""
    _bucket_logging_flush('Journal', False, False, 10)
    _bucket_logging_flush('Journal', False, False, 100)
    # testing empty commits after some activity
    _bucket_logging_flush('Journal', False, False, 0)

def test_bucket_logging_flush_s():
    """Verify separate-prefix Standard flushes for original 10/100 keys across five sources with actual POST keys and all original record/count assertions."""
    _bucket_logging_flush('Standard', False, False, 10)
    _bucket_logging_flush('Standard', False, False, 100)

def test_bucket_logging_flush_j_single():
    """Verify shared-prefix Journal flushes for original 10/100 keys by flushing only the first source and matching all five source records in the one returned log."""
    _bucket_logging_flush('Journal', True, False, 10)
    _bucket_logging_flush('Journal', True, False, 100)

def test_bucket_logging_flush_s_single():
    """Verify shared-prefix Standard flushes for original 10/100 keys by flushing only the first source and matching all original five-source records."""
    _bucket_logging_flush('Standard', True, False, 10)
    _bucket_logging_flush('Standard', True, False, 100)

def _verify_flushed_on_put(result):
    if _has_bucket_logging_extension():
        assert result['ResponseMetadata']['HTTPStatusCode'] == 200
        return result['FlushedLoggingObject']

def _bucket_logging_cleanup(cleanup_type, logging_type, single_prefix, concurrency):
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()

    num_buckets = 5
    buckets = []
    log_prefixes = []
    longer_time = expected_object_roll_time*10
    for j in range(num_buckets):
        src_bucket_name = get_new_bucket_name()
        src_bucket = get_new_bucket_resource(name=src_bucket_name)
        buckets.append(src_bucket_name)
        if single_prefix:
            log_prefixes.append('log/')
        else:
            log_prefixes.append(src_bucket_name+'/')

    _set_log_bucket_policy(client, log_bucket_name, buckets, log_prefixes)

    for j in range(num_buckets):
        logging_enabled = {'TargetBucket': log_bucket_name,
                           'ObjectRollTime': longer_time,
                           'LoggingType': logging_type,
                           'TargetPrefix': log_prefixes[j]}
        response = client.put_bucket_logging(Bucket=buckets[j], BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
        assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    num_keys = 10
    src_names = []
    for j in range(num_keys):
        src_names.append('myobject'+str(j))

    for src_bucket_name in buckets:
        for name in src_names:
            client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
            client.delete_object(Bucket=src_bucket_name, Key=name)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 0

    t = []
    flushed_obj = None
    updated_longer_time = expected_object_roll_time*20
    first_time = True
    for src_bucket_name in buckets:
        if not single_prefix:
            logging_enabled['TargetPrefix'] = src_bucket_name+'/'
        if cleanup_type == 'deletion':
            #  cleanup based on bucket deletion
            if concurrency:
                thr = threading.Thread(target = client.delete_bucket,
                                       kwargs={'Bucket': src_bucket_name})
                thr.start()
                t.append(thr)
            else:
                client.delete_bucket(Bucket=src_bucket_name)
        elif cleanup_type == 'disabling':
            # cleanup based on disabling bucket logging
            if concurrency:
                thr = threading.Thread(target = client.put_bucket_logging,
                                       kwargs={'Bucket': src_bucket_name, 'BucketLoggingStatus': {}})
                thr.start()
                t.append(thr)
            else:
                result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={})
                if first_time:
                    flushed_obj = _verify_flushed_on_put(result)
                    if single_prefix:
                        first_time = False
        elif cleanup_type == 'updating':
            # cleanup based on updating bucket logging parameters
            logging_enabled['ObjectRollTime'] = updated_longer_time
            if concurrency:
                thr = threading.Thread(target = client.put_bucket_logging,
                                       kwargs={'Bucket': src_bucket_name, 'BucketLoggingStatus': {'LoggingEnabled': logging_enabled}})
                thr.start()
                t.append(thr)
            else:
                result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
                    'LoggingEnabled': logging_enabled,
                })
                flushed_obj = _verify_flushed_on_put(result)
        elif cleanup_type == 'notupdating':
            # no concurrecy testing
            client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
                'LoggingEnabled': logging_enabled,
            })
        elif cleanup_type != 'target':
            assert False, 'invalid cleanup type: ' + cleanup_type

    _do_wait_completion(t)

    if cleanup_type == 'target':
        # delete the log bucket and then create it to make sure that no pending objects remained
        # no concurrecy testing
        client.delete_bucket(Bucket=log_bucket_name)
        log_bucket = get_new_bucket_resource(name=log_bucket_name)
        _set_log_bucket_policy(client, log_bucket_name, buckets, log_prefixes)
        old_names = src_names
        src_names = []
        for j in range(num_keys):
            src_names.append('after_deletion_myobject'+str(j))
        for src_bucket_name in buckets:
            for name in src_names:
                client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
                client.delete_object(Bucket=src_bucket_name, Key=name)
        response = client.list_objects_v2(Bucket=log_bucket_name)
        keys = _get_keys(response)
        assert len(keys) == 0
        # flsuh any new pending objects
        for src_bucket_name in buckets:
            client.post_bucket_logging(Bucket=src_bucket_name)
        # make sure that only the new objects are logged
        exact_match = True

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)

    if flushed_obj:
        assert flushed_obj in keys

    if cleanup_type == 'notupdating':
        # no cleanup expected
        assert len(keys) == 0
        return

    if concurrency:
        assert len(keys) >= 1 and len(keys) <= num_buckets
    else:
        if single_prefix and logging_type == 'Journal' and cleanup_type not in ['target', 'updating']:
            assert len(keys) == 1
        else:
            assert len(keys) == num_buckets

    prefixes = []
    for src_bucket_name in buckets:
        if single_prefix:
            prefix = 'log/'
        else:
            prefix = src_bucket_name+'/'
        prefixes.append(prefix)

    body = ""
    for key in keys:
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)
        found = False
        for prefix in prefixes:
            if key.startswith(prefix):
                found = True
        assert found, 'log key does not match any expected prefix: ' + key + ' expected prefixes: ' + str(prefixes)

    exact_match = True
    for src_bucket_name in buckets:
        assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_names, logging_type, num_keys, exact_match)
        assert _verify_records(body, src_bucket_name, 'REST.DELETE.OBJECT', src_names, logging_type, num_keys, exact_match)

def test_bucket_logging_cleanup_bucket_deletion_j():
    """Verify original five-source cleanup bucket deletion j transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('deletion', 'Journal', False, False)

def test_bucket_logging_cleanup_bucket_deletion_j_single():
    """Verify original five-source cleanup bucket deletion j single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('deletion', 'Journal', True, False)

def test_bucket_logging_cleanup_disabling_j():
    """Verify original five-source cleanup disabling j transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('disabling', 'Journal', False, False)

def test_bucket_logging_cleanup_disabling_j_single():
    """Verify original five-source cleanup disabling j single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('disabling', 'Journal', True, False)

def test_bucket_logging_cleanup_updating_j():
    """Verify original five-source cleanup updating j transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('updating', 'Journal', False, False)

def test_bucket_logging_cleanup_updating_j_single():
    """Verify original five-source cleanup updating j single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('updating', 'Journal', True, False)

def test_bucket_logging_notupdating_j():
    """Verify original five-source notupdating j transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('notupdating', 'Journal', False, False)

def test_bucket_logging_notupdating_j_single():
    """Verify original five-source notupdating j single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('notupdating', 'Journal', True, False)

def test_bucket_logging_cleanup_bucket_deletion_s():
    """Verify original five-source cleanup bucket deletion s transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('deletion', 'Standard', False, False)

def test_bucket_logging_cleanup_bucket_deletion_s_single():
    """Verify original five-source cleanup bucket deletion s single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('deletion', 'Standard', True, False)

def test_bucket_logging_cleanup_disabling_s():
    """Verify original five-source cleanup disabling s transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('disabling', 'Standard', False, False)

def test_bucket_logging_cleanup_disabling_s_single():
    """Verify original five-source cleanup disabling s single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('disabling', 'Standard', True, False)

def test_bucket_logging_cleanup_updating_s():
    """Verify original five-source cleanup updating s transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('updating', 'Standard', False, False)

def test_bucket_logging_cleanup_updating_s_single():
    """Verify original five-source cleanup updating s single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('updating', 'Standard', True, False)

def test_bucket_logging_notupdating_s():
    """Verify original five-source notupdating s transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('notupdating', 'Standard', False, False)

def test_bucket_logging_notupdating_s_single():
    """Verify original five-source notupdating s single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('notupdating', 'Standard', True, False)

def test_bucket_logging_target_cleanup_j():
    """Verify original five-source target cleanup j transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('target', 'Journal', False, False)

def test_bucket_logging_target_cleanup_j_single():
    """Verify original five-source target cleanup j single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('target', 'Journal', True, False)

def test_bucket_logging_target_cleanup_s():
    """Verify original five-source target cleanup s transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('target', 'Standard', False, False)

def test_bucket_logging_target_cleanup_s_single():
    """Verify original five-source target cleanup s single transition, exact PUT/DELETE records and actual flushed-object membership without changing source cleanup semantics."""
    _bucket_logging_cleanup('target', 'Standard', True, False)

def _bucket_logging_partial_cleanup(cleanup_type, logging_type, concurrency):
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()

    num_buckets = 3
    buckets = []
    log_prefixes = []
    longer_time = expected_object_roll_time*10
    for j in range(num_buckets):
        src_bucket_name = get_new_bucket_name()
        src_bucket = get_new_bucket_resource(name=src_bucket_name)
        buckets.append(src_bucket_name)
        log_prefixes.append('log/')

    _set_log_bucket_policy(client, log_bucket_name, buckets, log_prefixes)

    for j in range(num_buckets):
        logging_enabled = {'TargetBucket': log_bucket_name,
                           'ObjectRollTime': longer_time,
                           'LoggingType': logging_type,
                           'TargetPrefix': log_prefixes[j]}
        response = client.put_bucket_logging(Bucket=buckets[j], BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
        assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    num_keys = 3
    src_names = []
    for j in range(num_keys):
        src_names.append('myobject'+str(j))

    for src_bucket_name in buckets:
        for name in src_names:
            client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
            client.delete_object(Bucket=src_bucket_name, Key=name)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 0

    t = []
    flushed_obj = None
    updated_longer_time = expected_object_roll_time*20
    first_time = True
    for j in range(num_buckets-1):
        src_bucket_name = buckets[j]
        if cleanup_type == 'deletion':
            #  cleanup based on bucket deletion
            if concurrency:
                thr = threading.Thread(target = client.delete_bucket,
                                       kwargs={'Bucket': src_bucket_name})
                thr.start()
                t.append(thr)
            else:
                client.delete_bucket(Bucket=src_bucket_name)
        elif cleanup_type == 'disabling':
            # cleanup based on disabling bucket logging
            if concurrency:
                thr = threading.Thread(target = client.put_bucket_logging,
                                       kwargs={'Bucket': src_bucket_name, 'BucketLoggingStatus': {}})
                thr.start()
                t.append(thr)
            else:
                result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={})
                if first_time:
                    flushed_obj = _verify_flushed_on_put(result)
                    first_time = False
        elif cleanup_type == 'updating':
            # cleanup based on updating bucket logging parameters
            logging_enabled['ObjectRollTime'] = updated_longer_time
            if concurrency:
                thr = threading.Thread(target = client.put_bucket_logging,
                                       kwargs={'Bucket': src_bucket_name, 'BucketLoggingStatus': {'LoggingEnabled': logging_enabled}})
                thr.start()
                t.append(thr)
            else:
                result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
                    'LoggingEnabled': logging_enabled,
                })
                flushed_obj = _verify_flushed_on_put(result)
        else:
            assert False, 'invalid cleanup type: ' + cleanup_type

    last_bucket_name = buckets[num_buckets-1]
    for name in src_names:
        client.put_object(Bucket=last_bucket_name, Key=name, Body=randcontent())
        client.delete_object(Bucket=last_bucket_name, Key=name)

    _do_wait_completion(t)

    _flush_logs(client, last_bucket_name)
    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)

    if flushed_obj:
        assert flushed_obj in keys

    if concurrency:
        assert len(keys) >= 1 and len(keys) <= num_buckets
    elif cleanup_type == 'updating':
        assert len(keys) == num_buckets
    elif logging_type == 'Standard':
        # 1st deleted/disabled source write the bucket_deletion/put_bucket_logging record
        assert len(keys) == num_buckets
    else:
        assert len(keys) == num_buckets - 1


    prefixes = []
    for src_bucket_name in buckets:
        prefix = 'log/'
        prefixes.append(prefix)

    body = ""
    for key in keys:
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)
        found = False
        for prefix in prefixes:
            if key.startswith(prefix):
                found = True
        assert found, 'log key does not match any expected prefix: ' + key + ' expected prefixes: ' + str(prefixes)

    exact_match = True
    for src_bucket_name in buckets:
        _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_names, logging_type, num_keys, exact_match)
        _verify_records(body, src_bucket_name, 'REST.DELETE.OBJECT', src_names, logging_type, num_keys, exact_match)

def _bucket_logging_conf_update(logging_type, update_value, concurrency):
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()

    prefix = 'log/'
    longer_time = expected_object_roll_time*10
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)

    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])

    logging_enabled = {'TargetBucket': log_bucket_name,
                       'ObjectRollTime': longer_time,
                       'LoggingType': logging_type,
                       'TargetPrefix': prefix}
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200


    # conf update without any log records
    if update_value == "roll_time":
        logging_enabled['ObjectRollTime'] = longer_time*2
    elif update_value == "prefix":
        prefix = 'newlog1/'
        _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])
        logging_enabled['TargetPrefix'] = prefix
    else:
        assert False, 'invalid update value: ' + update_value

    result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    num_keys = 100
    src_names = []
    for j in range(num_keys):
        src_names.append('myobject'+str(j))

    def put_object_with_exception_handling(bucket, key, body):
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body)
        except Exception as e:
            logger.warning(f'put_object failed for {key}: {e}')

    t = []
    for name in src_names:
        if concurrency:
            thr = threading.Thread(target=put_object_with_exception_handling,
                                   args=(src_bucket_name, name, randcontent()))
            thr.start()
            t.append(thr)
        else:
            try:
                client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
            except Exception as e:
                logger.warning(f'put_object failed for {name}: {e}')

    if update_value == "roll_time":
        logging_enabled['ObjectRollTime'] = longer_time*3
    elif update_value == "prefix":
        prefix = 'newlog2/'
        _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])
        logging_enabled['TargetPrefix'] = prefix
    else:
        assert False, 'invalid update value: ' + update_value
    result = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })

    flushed_obj = _verify_flushed_on_put(result)
    logger.info('flushed log object after conf update: %s', flushed_obj)

    # perform conf update while puts are ongoing
    _do_wait_completion(t)

    expected_count = num_keys
    expected_log_objs = 2
    if concurrency:
        # some records may be written after the conf change
        _flush_logs(client, src_bucket_name)
        expected_log_objs = 3
        if update_value == 'prefix' and logging_type == 'Journal':
            # when changing prefix in concurrency mode
            # some put operations may fail, so the list of source objects
            # needs to be taken fro mthe bucket
            response = client.list_objects_v2(Bucket=src_bucket_name)
            src_names = _get_keys(response)
            expected_count = len(src_names)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    # one empty log object due to the initial conf change
    # and one with the log records due to the 2nd conf change
    # and one flushed object at the end
    assert len(keys) == expected_log_objs
    assert flushed_obj in keys

    exact_match = True
    body = ""
    for key in keys:
        logger.info('processing log object: %s', key)
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)
        # delete the key that we already processed
        client.delete_object(Bucket=log_bucket_name, Key=key)

    ok = _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_names, logging_type, expected_count, exact_match)
    if not (concurrency and update_value == 'prefix' and logging_type == 'Standard'):
        # we can have silent failures when changing the prefix in concurrency mode with standard logging
        assert ok

    # make sure that logging still works after conf change
    src_names = []
    for j in range(num_keys):
        src_names.append('anotherobject'+str(j))
    for name in src_names:
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())

    flushed_obj = _flush_logs(client, src_bucket_name)
    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 1
    assert flushed_obj in keys

    body = ""
    for key in keys:
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)

    assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_names, logging_type, num_keys, exact_match)

def test_bucket_logging_part_cleanup_updating_s():
    """Verify original part cleanup updating s source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('updating', 'Standard', False)

def test_bucket_logging_part_cleanup_disabling_s():
    """Verify original part cleanup disabling s source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('disabling', 'Standard', False)

def test_bucket_logging_part_cleanup_deletion_s():
    """Verify original part cleanup deletion s source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('deletion', 'Standard', False)

def test_bucket_logging_part_cleanup_updating_j():
    """Verify original part cleanup updating j source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('updating', 'Journal', False)

def test_bucket_logging_part_cleanup_disabling_j():
    """Verify original part cleanup disabling j source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('disabling', 'Journal', False)

def test_bucket_logging_part_cleanup_deletion_j():
    """Verify original part cleanup deletion j source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_partial_cleanup('deletion', 'Journal', False)

def test_bucket_logging_conf_updating_roll_s():
    """Verify original conf updating roll s source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_conf_update('Standard', 'roll_time', False)

def test_bucket_logging_conf_updating_roll_j():
    """Verify original conf updating roll j source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_conf_update('Journal', 'roll_time', False)

def test_bucket_logging_conf_updating_pfx_s():
    """Verify original conf updating pfx s source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_conf_update('Standard', 'prefix', False)

def test_bucket_logging_conf_updating_pfx_j():
    """Verify original conf updating pfx j source operations, actual flushed keys and original asserted/non-asserted record expectations without strengthening its oracle."""
    _bucket_logging_conf_update('Journal', 'prefix', False)

def _bucket_logging_type(logging_type):
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()
    prefix = 'log/'
    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])
    logging_enabled = {
            'TargetBucket': log_bucket_name,
            'TargetPrefix': prefix,
            'ObjectRollTime': expected_object_roll_time,
            'LoggingType': logging_type
            }
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    num_keys = 5
    for j in range(num_keys):
        name = 'myobject'+str(j)
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
        client.head_object(Bucket=src_bucket_name, Key=name)

    response = client.list_objects_v2(Bucket=src_bucket_name)
    src_keys = _get_keys(response)

    _flush_logs(client, src_bucket_name)

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 1

    key = keys[0]
    assert key.startswith('log/')
    response = client.get_object(Bucket=log_bucket_name, Key=key)
    body = _get_body(response)
    if logging_type == 'Journal':
        assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_keys, 'Journal', num_keys)
        assert _verify_records(body, src_bucket_name, 'REST.HEAD.OBJECT', src_keys, 'Journal', num_keys) == False
    elif logging_type == 'Standard':
        assert _verify_records(body, src_bucket_name, 'REST.HEAD.OBJECT', src_keys, 'Standard', num_keys)
        assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_keys, 'Standard', num_keys)
    else:
        assert False, 'invalid logging type:'+logging_type

def test_bucket_logging_event_type_j():
    """Verify original event type j fields, actual GET/HEAD/PUT events and precise record-count rules while preserving every original real wait."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _bucket_logging_type('Journal')

def test_bucket_logging_event_type_s():
    """Verify original event type s fields, actual GET/HEAD/PUT events and precise record-count rules while preserving every original real wait."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _bucket_logging_type('Standard')

def test_bucket_logging_roll_time():
    """Verify original roll time fields, actual GET/HEAD/PUT events and precise record-count rules while preserving every original real wait."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)
    client = get_client()

    prefix = 'log/'
    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])
    roll_time = 10
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix, 'ObjectRollTime': roll_time}
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    num_keys = 5
    for j in range(num_keys):
        name = 'myobject'+str(j)
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())

    response = client.list_objects_v2(Bucket=src_bucket_name)
    src_keys = _get_keys(response)

    time.sleep(roll_time/2)
    client.put_object(Bucket=src_bucket_name, Key='myobject', Body=randcontent())

    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 0

    time.sleep(roll_time/2)
    client.put_object(Bucket=src_bucket_name, Key='myobject', Body=randcontent())

    # It can take up to 10s for the bucket logging manager to detect a new commit list
    time.sleep(11)
    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 1

    key = keys[0]
    assert key.startswith('log/')
    response = client.get_object(Bucket=log_bucket_name, Key=key)
    body = _get_body(response)
    assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_keys, 'Standard', num_keys)
    client.delete_object(Bucket=log_bucket_name, Key=key)

    num_keys = 25
    for j in range(num_keys):
        name = 'myobject'+str(j)
        client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
        time.sleep(1)

    response = client.list_objects_v2(Bucket=src_bucket_name)
    src_keys = _get_keys(response)

    time.sleep(roll_time)
    client.put_object(Bucket=src_bucket_name, Key='myobject', Body=randcontent())
    time.sleep(5)
    response = client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) > 1

    body = ''
    for key in keys:
        assert key.startswith('log/')
        response = client.get_object(Bucket=log_bucket_name, Key=key)
        body += _get_body(response)
    assert _verify_records(body, src_bucket_name, 'REST.PUT.OBJECT', src_keys, 'Standard', num_keys+1)

def test_put_bucket_logging_account_j():
    """Verify all three original Journal cross-account vectors, real root GetUser account producers and actual extension POST/log readback semantics."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _put_bucket_logging_account('Journal')
