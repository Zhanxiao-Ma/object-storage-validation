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
import json
import shlex
import string
import random
import time
import logging
from .ceph_logging_remaining_runtime import (get_new_bucket_name,get_new_bucket_resource,get_new_bucket,get_client,get_iam_client,get_iam_root_client,get_iam_root_s3client,get_main_user_id,get_alt_user_id,get_sts_client,get_config_endpoint,get_parameter_name,nuke_role,boto3,ClientError,ParamValidationError)
logger=logging.getLogger(__name__)
expected_object_roll_time=5

def _get_keys(response):
    """
    return lists of strings that are the keys from a client.list_objects() response
    """
    keys = []
    if 'Contents' in response:
        objects_list = response['Contents']
        keys = [obj['Key'] for obj in objects_list]
    return keys

def _get_body(response):
    body = response['Body']
    got = body.read()
    if type(got) is bytes:
        got = got.decode()
    return got

def _set_log_bucket_policy_tenant(client, log_tenant, log_bucket_name, src_tenant, src_user, src_buckets, log_prefixes):
    statements = []
    for j in range(len(src_buckets)):
        if len(log_prefixes) == 1:
            prefix = log_prefixes[0]
        else:
            prefix = log_prefixes[j]
        statements.append({
            "Sid": "S3ServerAccessLogsPolicy",
            "Effect": "Allow",
            "Principal": {"Service": "logging.s3.amazonaws.com"},
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3::{}:{}/{}".format(log_tenant, log_bucket_name, prefix),
            "Condition": {
                "ArnLike": {"aws:SourceArn": "arn:aws:s3::{}:{}".format(src_tenant, src_buckets[j])},
                "StringEquals": {
                    "aws:SourceAccount": src_user
                    }
            }
        })

    policy_document = json.dumps({
        "Version": "2012-10-17",
        "Statement": statements
        })

    result = client.put_bucket_policy(Bucket=log_bucket_name, Policy=policy_document)
    assert(result['ResponseMetadata']['HTTPStatusCode'] == 204)

def _set_log_bucket_policy(client, log_bucket_name, src_buckets, log_prefixes):
    _set_log_bucket_policy_tenant(client, "", log_bucket_name, "", get_main_user_id(), src_buckets, log_prefixes)

def _has_bucket_logging_extension():
    src_bucket_name = get_new_bucket_name()
    log_bucket_name = get_new_bucket_name()
    client = get_client()
    log_prefix = 'log/'
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': log_prefix, 'LoggingType': 'Journal'}
    try:
        response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
            'LoggingEnabled': logging_enabled,
        })
    except ParamValidationError as e:
        return False
    except ClientError as e:
        # should fail on non-existing bucket
        return True

    return True

def _parse_standard_log_record(record):
    record = record.replace('[', '"').replace(']', '"')
    chunks = shlex.split(record)
    assert len(chunks) == 26
    return {
            'BucketOwner':      chunks[0],
            'BucketName':       chunks[1],
            'RequestDateTime':  chunks[2],
            'RemoteIP':         chunks[3],
            'Requester':        chunks[4],
            'RequestID':        chunks[5],
            'Operation':        chunks[6],
            'Key':              chunks[7],
            'RequestURI':       chunks[8],
            'HTTPStatus':       chunks[9],
            'ErrorCode':        chunks[10],
            'BytesSent':        chunks[11],
            'ObjectSize':       chunks[12],
            'TotalTime':        chunks[13],
            'TurnAroundTime':   chunks[14],
            'Referrer':         chunks[15],
            'UserAgent':        chunks[16],
            'VersionID':        chunks[17],
            'HostID':           chunks[18],
            'SigVersion':       chunks[19],
            'CipherSuite':      chunks[20],
            'AuthType':         chunks[21],
            'HostHeader':       chunks[22],
            'TLSVersion':       chunks[23],
            'AccessPointARN':   chunks[24],
            'ACLRequired':      chunks[25],
            }

def _parse_journal_log_record(record):
    record = record.replace('[', '"').replace(']', '"')
    chunks = shlex.split(record)
    assert len(chunks) == 8
    return {
            'BucketOwner':      chunks[0],
            'BucketName':       chunks[1],
            'RequestDateTime':  chunks[2],
            'Operation':        chunks[3],
            'Key':              chunks[4],
            'ObjectSize':       chunks[5],
            'VersionID':        chunks[6],
            'ETAG':             chunks[7],
            }

def _parse_log_record(record, record_type):
    if record_type == 'Standard':
        return _parse_standard_log_record(record)
    elif record_type == 'Journal':
        return _parse_journal_log_record(record)
    else:
        assert False, 'unknown log record type'

def _verify_records(records, bucket_name, event_type, src_keys, record_type, expected_count, exact_match=False):
    keys_found = []
    all_keys = []
    for record in iter(records.splitlines()):
        parsed_record = _parse_log_record(record, record_type)
        logger.debug('bucket log record: %s', json.dumps(parsed_record, indent=4))
        if bucket_name in record and event_type in record:
            all_keys.append(parsed_record['Key'])
            for key in src_keys:
                if key in record:
                    keys_found.append(key)
                    break
    logger.info('%d keys found in bucket log: %s', len(all_keys), str(all_keys))
    logger.info('%d keys from the source bucket: %s', len(src_keys), str(src_keys))
    if exact_match:
        return len(keys_found) == expected_count and len(keys_found) == len(all_keys)
    return len(keys_found) == expected_count

def _verify_record_field(records, bucket_name, event_type, object_key, record_type, field_name, expected_value):
    for record in iter(records.splitlines()):
        if bucket_name in record and event_type in record and object_key in record:
            parsed_record = _parse_log_record(record, record_type)
            logger.info('bucket log record: %s', json.dumps(parsed_record, indent=4))
            try:
                value = parsed_record[field_name]
                return expected_value == value
            except KeyError:
                return False
    return False

def randcontent():
    letters = string.ascii_lowercase
    length = random.randint(10, 1024)
    return ''.join(random.choice(letters) for i in range(length))

def _flush_logs(client, src_bucket_name, dummy_key="dummy"):
    if _has_bucket_logging_extension():
        result = client.post_bucket_logging(Bucket=src_bucket_name)
        assert result['ResponseMetadata']['HTTPStatusCode'] == 200
        return result['FlushedLoggingObject']
    else:
        time.sleep(expected_object_roll_time*1.1)
        client.put_object(Bucket=src_bucket_name, Key=dummy_key, Body='dummy')
        return None

def test_bucket_logging_requester_assumed_role():
    """Verify actual source IAM role and alternate STS credentials produce a real temporary S3 request whose retrieved standard log Requester contains the original assumed-role ARN substring."""
    """verify the standard access log Requester field contains the assumed-role ARN
    for requests made with STS temporary credentials"""
    iam_client = get_iam_client()
    sts_client = get_sts_client()
    alt_user_id = get_alt_user_id()
    default_endpoint = get_config_endpoint()
    role_session_name = get_parameter_name()

    # create an IAM role assumable by the alt user
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": [f"arn:aws:iam:::user/{alt_user_id}"]},
            "Action": ["sts:AssumeRole"]
        }]
    })
    role_name = get_parameter_name()
    role_response = iam_client.create_role(Path='/', RoleName=role_name, AssumeRolePolicyDocument=trust_policy)
    role_arn = role_response['Role']['Arn']

    try:
        role_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": "s3:*",
                "Resource": "*"
            }]
        })
        iam_client.put_role_policy(RoleName=role_name, PolicyName='S3FullAccess', PolicyDocument=role_policy)

        # assume the role
        resp = sts_client.assume_role(RoleArn=role_arn, RoleSessionName=role_session_name)

        assert resp['ResponseMetadata']['HTTPStatusCode'] == 200
        creds = resp['Credentials']
        role_s3 = boto3.client(
            's3',
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
            endpoint_url=default_endpoint,
            region_name='')

        src_bucket_name = get_new_bucket_name()
        log_bucket_name = get_new_bucket_name()
        role_s3.create_bucket(Bucket=src_bucket_name)
        role_s3.create_bucket(Bucket=log_bucket_name)
        prefix = 'log/'
        _set_log_bucket_policy_tenant(role_s3, "", log_bucket_name, "", alt_user_id, [src_bucket_name], [prefix])

        logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix}
        role_s3.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={'LoggingEnabled': logging_enabled})

        key = 'my-test-object'
        role_s3.put_object(Bucket=src_bucket_name, Key=key, Body=randcontent())

        _flush_logs(role_s3, src_bucket_name)
        response = role_s3.list_objects_v2(Bucket=log_bucket_name)
        log_keys = _get_keys(response)
        assert len(log_keys) == 1

        response = role_s3.get_object(Bucket=log_bucket_name, Key=log_keys[0])
        body = _get_body(response)
        expected_substring = f'assumed-role/{role_name}/{role_session_name}'
        found = False
        for record in body.splitlines():
            if src_bucket_name not in record or 'REST.PUT.OBJECT' not in record or key not in record:
                continue
            parsed = _parse_log_record(record, "Standard")
            requester = parsed['Requester']
            assert requester.startswith('arn:aws:sts::'), f"Requester should be an STS ARN, got: {requester}"
            assert expected_substring in requester, f"Requester missing {expected_substring}, got: {requester}"
            found = True
            break
        assert found, "no PUT log record found for the assumed-role request"
    finally:
        nuke_role(iam_client, role_name)

def _bucket_logging_objects(src_client, src_bucket_name, log_client, log_bucket_name, log_type, op_name):
    num_keys = 5
    for j in range(num_keys):
        name = 'myobject'+str(j)
        src_client.put_object(Bucket=src_bucket_name, Key=name, Body=randcontent())

    expected_count = num_keys

    response = src_client.list_objects_v2(Bucket=src_bucket_name)
    src_keys = _get_keys(response)

    flushed_obj = _flush_logs(src_client, src_bucket_name)

    response = log_client.list_objects_v2(Bucket=log_bucket_name)
    keys = _get_keys(response)
    assert len(keys) == 1

    for key in keys:
        if flushed_obj is not None:
            assert key == flushed_obj
        assert key.startswith('log/')
        response = log_client.get_object(Bucket=log_bucket_name, Key=key)
        body = _get_body(response)
        assert _verify_records(body, src_bucket_name, op_name, src_keys, log_type, expected_count)

def _put_bucket_logging_account(log_type):
    # src is default user and log is in an account user
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket_resource(name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_client = get_iam_root_s3client()
    log_bucket = get_new_bucket(client=log_client, name=log_bucket_name)
    client = get_client()
    prefix = 'log/'
    _set_log_bucket_policy_tenant(log_client, "", log_bucket_name, "", get_main_user_id(), [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, log_client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    # src and log are in an iam account
    client = get_iam_root_s3client()
    iam_client = get_iam_root_client()
    iam_user = iam_client.get_user()['User']['Arn'].split(':')[-2]  # Get the IAM user name from ARN
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket(client=client, name=src_bucket_name)
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket(client=client, name=log_bucket_name)
    _set_log_bucket_policy_tenant(client, "", log_bucket_name, "", iam_user, [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': prefix}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

    # src is in an iam account and log is the default user
    client = get_iam_root_s3client()
    iam_client = get_iam_root_client()
    iam_user = iam_client.get_user()['User']['Arn'].split(':')[-2]  # Get the IAM user name from ARN
    src_bucket_name = get_new_bucket_name()
    src_bucket = get_new_bucket(client=client, name=src_bucket_name)
    log_client = get_client()
    log_bucket_name = get_new_bucket_name()
    log_bucket = get_new_bucket(client=log_client, name=log_bucket_name)
    _set_log_bucket_policy_tenant(log_client, "", log_bucket_name, "", iam_user, [src_bucket_name], [prefix])
    logging_enabled = {'TargetBucket': log_bucket_name, 'TargetPrefix': 'log/'}
    if log_type == 'Journal':
        logging_enabled['LoggingType'] = 'Journal'
    response = client.put_bucket_logging(Bucket=src_bucket_name, BucketLoggingStatus={
        'LoggingEnabled': logging_enabled,
    })
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    _bucket_logging_objects(client, src_bucket_name, log_client, log_bucket_name, log_type, 'REST.PUT.OBJECT')

def test_put_bucket_logging_account_s():
    """Verify all three original main/account-root logging vectors, actual GetUser ARN-derived SourceAccount and five original records per target, preserving real standard rollover waits."""
    _put_bucket_logging_account('Standard')
