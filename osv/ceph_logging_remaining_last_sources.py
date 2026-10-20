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
import datetime
from .ceph_logging_remaining_last_runtime import get_tenant_client,get_tenant_name,get_tenant_user_id
from .ceph_logging_remaining_runtime import get_new_bucket_name,get_new_bucket_resource,get_new_bucket,get_client,get_main_user_id,ClientError
from .ceph_logging_remaining_sources import json,_has_bucket_logging_extension,_set_log_bucket_policy,_set_log_bucket_policy_tenant,_bucket_logging_objects,_flush_logs,_get_keys,_get_body,_parse_log_record,randcontent,logger
from .ceph_logging_remaining_extension_sources import pytest


def _put_bucket_logging_tenant(log_type):
    # src is on default tenant and log is on a different tenant
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    tenant_client = get_tenant_client()
    log_bucket = get_new_bucket(client=tenant_client, name=log_bucket_name)
    client = get_client()
    prefix = 'log/'
    log_tenant = get_tenant_name()
    _set_log_bucket_policy_tenant(tenant_client, log_tenant, log_bucket_name, "", get_main_user_id(), [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_tenant+':'+log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, tenant_client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    # src is on default tenant and log is on a different tenant with the same name
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = src_bucket_name
    tenant_client = get_tenant_client()
    log_bucket = get_new_bucket(client=tenant_client, name=log_bucket_name)
    client = get_client()
    tenant_name = get_tenant_name()
    _set_log_bucket_policy_tenant(tenant_client, tenant_name, log_bucket_name, "", get_main_user_id(),  [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': tenant_name+':'+log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, tenant_client, log_bucket_name, log_type, 'REST.PUT.OBJECT')


    try:
        # src is on default tenant and log is on a different tenant
        # log bucket name not set correctly
        src_bucket_name = get_new_bucket_name()
        src_bucket = get_new_bucket_resource(name=src_bucket_name)
        log_bucket_name = get_new_bucket_name()
        log_client = get_tenant_client()
        log_bucket = get_new_bucket(client=log_client, name=log_bucket_name)
        client = get_client()
        _set_log_bucket_policy_tenant(log_client, get_tenant_name(), log_bucket_name, "", get_main_user_id(), [src_bucket_name], [prefix])
        logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix}
        if log_type == 'Journal':
            logging_enabled['LoggingType'] = 'Journal'
        response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
    except ClientError as e:
        assert e.response['Error']['Code'] == 'NoSuchKey'
    else:
        assert False, 'expected failure'

    # src and log are on the same tenant
    # log bucket name is set with tenant
    client = get_tenant_client()
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket(client=client, name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket(client=client, name=log_bucket_name)
    tenant_name = get_tenant_name()
    _set_log_bucket_policy_tenant(client, tenant_name, log_bucket_name, tenant_name, get_tenant_user_id(), [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': tenant_name+':'+log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    # src and log are on the same tenant
    # log bucket name is set without tenant
    client = get_tenant_client()
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket(client=client, name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket(client=client, name=log_bucket_name)
    tenant_name = get_tenant_name()
    _set_log_bucket_policy_tenant(client, tenant_name, log_bucket_name, tenant_name, get_tenant_user_id(), [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    # src is on tenant and log is on the default tenant
    # log bucket name is set with explicit default tenant
    client = get_tenant_client()
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket(client=client, name=src_bucket_name)
    log_client = get_client()
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket(client=log_client, name=log_bucket_name)
    _set_log_bucket_policy_tenant(log_client, "", log_bucket_name, get_tenant_name(), get_tenant_user_id(), [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': ':'+log_bucket_name, 'TargetPrefix': 'log/'}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, log_client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    try:
        # src is on tenant and log is on the default tenant
        client = get_tenant_client()
        src_bucket_name = get_new_bucket_name()
        src_bucket = get_new_bucket(client=client, name=src_bucket_name)
        log_bucket_name = get_new_bucket_name()
        log_client = get_client()
        log_bucket = get_new_bucket(client=log_client, name=log_bucket_name)
        _set_log_bucket_policy_tenant(log_client, "", log_bucket_name, get_tenant_name(), get_tenant_user_id(), [src_bucket_name], [prefix])
        logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': 'log/'}
        if log_type == 'Journal':
            logging_enabled['LoggingType'] = 'Journal'
        response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
    except ClientError as e:
        assert e.response['Error']['Code'] == 'NoSuchKey'
    else:
        assert False, 'expected failure'

def test_put_bucket_logging_tenant_j():
    """Verify all seven original Journal tenant namespace/address vectors, both exact NoSuchKey failures, five full five-PUT/log readback vectors and same bare names in different actual SDK identities."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    _put_bucket_logging_tenant('Journal')

def test_bucket_logging_object_meta():
    """Verify original lock-enabled version producer, ACL/tags/hold and sixty-day governance changes produce exactly the six Journal events with actual returned version IDs; retain original final hold OFF and bypass delete."""
    if not _has_bucket_logging_extension():
        pytest.skip('ceph extension to bucket logging not supported at client')
    client = get_client()
    src_bucket_name = get_new_bucket_name()
    src_bucket = client.create_bucket(Bucket=src_bucket_name, ObjectLockEnabledForBucket=True)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket_resource(name=log_bucket_name)

    prefix = 'log/'
    _set_log_bucket_policy(client, log_bucket_name, [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix, 'LoggingType': 'Journal'}
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    name = 'myobject'
    response = client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())
    version_id = response['VersionId']
    # PutObjectAcl
    client.put_object_acl(ACL='public-read-write', Bucket=src_bucket_name, Key=name, VersionId=version_id)
    # PutObjectTagging
    client.put_object_tagging(Bucket=src_bucket_name, Key=name, VersionId=version_id,
                              Tagging={'TagSet': [{'Key': 'tag1', 'Value': 'value1'}, {'Key': 'tag2', 'Value': 'value2'}]})
    # DeleteObjectTagging
    client.delete_object_tagging(Bucket=src_bucket_name, Key=name, VersionId=version_id)
    # PutObjectLegalHold
    client.put_object_legal_hold(Bucket=src_bucket_name, Key=name, LegalHold={'Status': 'ON'})
    # PutObjectRetention
    client.put_object_retention(Bucket=src_bucket_name, Key=name, Retention={'Mode': 'GOVERNANCE', 'RetainUntilDate': datetime.datetime.now() + datetime.timedelta(days=60)})

    _flush_logs(client, src_bucket_name)
    response = client.list_objects_v2(Bucket=log_bucket_name)
    log_keys = _get_keys(response)
    assert len(log_keys) == 1
    log_key = log_keys[0]
    expected_op_list = ['REST.PUT.OBJECT', 'REST.PUT.ACL', 'REST.PUT.LEGAL_HOLD', 'REST.PUT.RETENTION', 'REST.PUT.OBJECT_TAGGING', 'REST.DELETE.OBJECT_TAGGING']
    op_list = []
    response = client.get_object(Bucket=log_bucket_name, Key=log_key)
    body = _get_body(response)
    for record in iter(body.splitlines()):
        parsed_record = _parse_log_record(record, 'Journal')
        logger.info('bucket log record: %s', json.dumps(parsed_record, indent=4))
        op_list.append(parsed_record['Operation'])
        version = parsed_record['VersionID']
        if version != '-':
            assert version == version_id

    assert sorted(expected_op_list) == sorted(op_list)

    # allow cleanup
    client.put_object_legal_hold(Bucket=src_bucket_name, Key=name, LegalHold={'Status': 'OFF'})
    client.delete_object(Bucket=src_bucket_name, Key=name, VersionId=version_id, BypassGovernanceRetention=True)
