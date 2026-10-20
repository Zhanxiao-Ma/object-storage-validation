"""Statically migrated Ceph STS source bodies; actual clients are provided by ceph_sts_web.

Source: https://github.com/ceph/s3-tests/blob/5b3987116eca0b4b8fa57d6b10f3db57bab196b6/s3tests/functional/test_sts.py
MIT permission notice retained below; original contributors and pinned revision are authoritative.
No upstream test runner or boto3 client is imported or executed. Original policy strings,
request argument order, branches and assertions remain present for source review.

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
from .ceph_sts_web_runtime import (check_webidentity,get_iam_client,get_sts_client,get_config_endpoint,get_parameter_name,get_thumbprint,get_aud,get_token,get_realm_name,get_sub,get_azp,get_user_token,get_iam_access_key,get_iam_secret_key,get_s3_client_using_iam_creds,get_s3_resource_using_iam_creds,get_new_bucket_name,boto3,ClientError)

def create_role(iam_client,path,rolename,policy_document,description,sessionduration,permissionboundary,tag_list=None):
    role_err=None
    role_response = None
    if rolename is None:
        rolename=get_parameter_name()
    if tag_list is None:
        tag_list = []
    try:
        role_response = iam_client.create_role(Path=path,RoleName=rolename,AssumeRolePolicyDocument=policy_document,Tags=tag_list)
    except ClientError as e:
    	role_err = e.response['Code']
    return (role_err,role_response,rolename)

def put_role_policy(iam_client,rolename,policyname,role_policy):
    role_err=None
    role_response = None
    if policyname is None:
        policyname=get_parameter_name() 
    try:
        role_response = iam_client.put_role_policy(RoleName=rolename,PolicyName=policyname,PolicyDocument=role_policy)
    except ClientError as e:
    	role_err = e.response['Code']
    return (role_err,role_response)

def create_oidc_provider(iam_client, url, clientidlist, thumbprintlist):
    oidc_arn = None
    oidc_error = None
    clientids = []
    if clientidlist is None:
        clientidlist=clientids
    try:
        oidc_response = iam_client.create_open_id_connect_provider(
            Url=url,
            ClientIDList=clientidlist,
            ThumbprintList=thumbprintlist,
        )
        oidc_arn = oidc_response['OpenIDConnectProviderArn']
        print (oidc_arn)
    except ClientError as e:
        oidc_error = e.response['Code']
        print (oidc_error)
        try:
            oidc_error = None
            print (url)
            if url.startswith('http://'):
                url = url[len('http://'):]
            elif url.startswith('https://'):
                url = url[len('https://'):]
            elif url.startswith('www.'):
                url = url[len('www.'):]
            oidc_arn = 'arn:aws:iam:::oidc-provider/{}'.format(url)
            print (url)
            print (oidc_arn)
            oidc_response = iam_client.get_open_id_connect_provider(OpenIDConnectProviderArn=oidc_arn)
        except ClientError as e:
            oidc_arn = None
    return (oidc_arn, oidc_error)

def test_assume_role_with_web_identity():
    """Verify app_id federated trust produces real temporary credentials which create/delete a bucket with exact HTTP200/204."""
    check_webidentity()
    iam_client=get_iam_client()    
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()
    
    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )
    
    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''
    
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\"}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    if response:
        assert response['ResponseMetadata']['HTTPStatusCode'] == 200
    else:
        assert False, role_err
    
    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200
    
    s3_client = boto3.client('s3',
		aws_access_key_id = resp['Credentials']['AccessKeyId'],
		aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
		aws_session_token = resp['Credentials']['SessionToken'],
		endpoint_url=default_endpoint,
		region_name='',
		)
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204
    
    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_session_policy_check_on_different_buckets():
    """Verify disjoint role/session bucket permissions deny both literal creates and preserve original missing-bucket object error."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"arn:aws:s3:::test2\",\"arn:aws:s3:::test2/*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )

    bucket_name_1 = 'test1'
    try:
        s3bucket = s3_client.create_bucket(Bucket=bucket_name_1)
    except ClientError as e:
        s3bucket_error = e.response.get("Error", {}).get("Code")
    assert s3bucket_error == 'AccessDenied'

    bucket_name_2 = 'test2'
    try:
        s3bucket = s3_client.create_bucket(Bucket=bucket_name_2)
    except ClientError as e:
        s3bucket_error = e.response.get("Error", {}).get("Code")
    assert s3bucket_error == 'AccessDenied'

    bucket_body = 'please-write-something'
    #body.encode(encoding='utf_8')
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'NoSuchBucket'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_check_on_same_bucket():
    """Verify intersecting role/session permissions allow a real PUT on literal test1."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )

    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_check_put_obj_denial():
    """Verify a GetObject-only session denies the original PutObject."""
    check_webidentity()
    iam_client=get_iam_client()
    iam_access_key=get_iam_access_key()
    iam_secret_key=get_iam_secret_key()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )

    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_swapping_role_policy_and_session_policy():
    """Verify swapping broad and scoped role/session policies retains allowed PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    iam_access_key=get_iam_access_key()
    iam_secret_key=get_iam_secret_key()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_check_different_op_permissions():
    """Verify non-overlapping role PUT and session GET permissions deny PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    iam_access_key=get_iam_access_key()
    iam_secret_key=get_iam_secret_key()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )

    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_check_with_deny_effect():
    """Verify session Deny overrides role Allow for PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    iam_access_key=get_iam_access_key()
    iam_secret_key=get_iam_secret_key()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Deny\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_check_with_deny_on_same_op():
    """Verify explicit Deny on the same operation prevents PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    iam_access_key=get_iam_access_key()
    iam_secret_key=get_iam_secret_key()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy_new = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy_new)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client_iam_creds = get_s3_client_using_iam_creds()

    bucket_name_1 = 'test1'
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Deny\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )

    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_bucket_policy_role_arn():
    """Verify role-ARN bucket grant preserves original PUT success and GET AccessDenied."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3client_iamcreds = get_s3_client_using_iam_creds()
    bucket_name_1 = 'test1'
    s3bucket = s3client_iamcreds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    resource1 = "arn:aws:s3:::" + bucket_name_1
    resource2 = "arn:aws:s3:::" + bucket_name_1 + "/*"
    rolearn = "arn:aws:iam:::role/" + general_role_name
    bucket_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "{}".format(rolearn)},
        "Action": ["s3:GetObject","s3:PutObject"],
        "Resource": [
            "{}".format(resource1),
            "{}".format(resource2)
          ]
        }]
     })
    s3client_iamcreds.put_bucket_policy(Bucket=bucket_name_1, Policy=bucket_policy)
    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    try:
        obj = s3_client.get_object(Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3object_error = e.response.get("Error", {}).get("Code")
    assert s3object_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_bucket_policy_session_arn():
    """Verify assumed-session ARN bucket grant permits the original PUT and GET."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3client_iamcreds = get_s3_client_using_iam_creds()
    bucket_name_1 = 'test1'
    s3bucket = s3client_iamcreds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    resource1 = "arn:aws:s3:::" + bucket_name_1
    resource2 = "arn:aws:s3:::" + bucket_name_1 + "/*"
    rolesessionarn = "arn:aws:iam:::assumed-role/" + general_role_name + "/" + role_session_name
    bucket_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "{}".format(rolesessionarn)},
        "Action": ["s3:GetObject","s3:PutObject"],
        "Resource": [
            "{}".format(resource1),
            "{}".format(resource2)
          ]
        }]
    })
    s3client_iamcreds.put_bucket_policy(Bucket=bucket_name_1, Policy=bucket_policy)
    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200


    s3_get_obj = s3_client.get_object(Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_get_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_copy_object():
    """Verify session bucket grant permits the original managed copy and destination GET."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3client_iamcreds = get_s3_client_using_iam_creds()
    bucket_name_1 = 'test1'
    s3bucket = s3client_iamcreds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    resource1 = "arn:aws:s3:::" + bucket_name_1
    resource2 = "arn:aws:s3:::" + bucket_name_1 + "/*"
    rolesessionarn = "arn:aws:iam:::assumed-role/" + general_role_name + "/" + role_session_name
    print (rolesessionarn)
    bucket_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "{}".format(rolesessionarn)},
        "Action": ["s3:GetObject","s3:PutObject"],
        "Resource": [
            "{}".format(resource1),
            "{}".format(resource2)
          ]
        }]
     })
    s3client_iamcreds.put_bucket_policy(Bucket=bucket_name_1, Policy=bucket_policy)
    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    copy_source = {
    'Bucket': bucket_name_1,
    'Key': 'test-1.txt'
    }

    s3_client.copy(copy_source, bucket_name_1, "test-2.txt")

    s3_get_obj = s3_client.get_object(Bucket=bucket_name_1, Key="test-2.txt")
    assert s3_get_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_no_bucket_role_policy():
    """Verify absent role permissions prevent session PUT despite session allow."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    s3client_iamcreds = get_s3_client_using_iam_creds()
    bucket_name_1 = 'test1'
    s3bucket = s3client_iamcreds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\",\"s3:GetObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3putobj_error = e.response.get("Error", {}).get("Code")
    assert s3putobj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_session_policy_bucket_policy_deny():
    """Verify explicit session-ARN bucket Deny prevents PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    aud=get_aud()
    token=get_token()
    realm=get_realm_name()

    url = 'http://localhost:8080/auth/realms/{}'.format(realm)
    thumbprintlist = [thumbprint]
    (oidc_arn,oidc_error) = create_oidc_provider(iam_client, url, None, thumbprintlist)
    if oidc_error is not None:
        raise RuntimeError('Unable to create/get openid connect provider {}'.format(oidc_error))

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_arn+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":app_id\":\""+aud+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":[\"*\"]}}"

    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    s3client_iamcreds = get_s3_client_using_iam_creds()
    bucket_name_1 = 'test1'
    s3bucket = s3client_iamcreds.create_bucket(Bucket=bucket_name_1)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    resource1 = "arn:aws:s3:::" + bucket_name_1
    resource2 = "arn:aws:s3:::" + bucket_name_1 + "/*"
    rolesessionarn = "arn:aws:iam:::assumed-role/" + general_role_name + "/" + role_session_name
    bucket_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [{
        "Effect": "Deny",
        "Principal": {"AWS": "{}".format(rolesessionarn)},
        "Action": ["s3:GetObject","s3:PutObject"],
        "Resource": [
            "{}".format(resource1),
            "{}".format(resource2)
          ]
        }]
    })
    s3client_iamcreds.put_bucket_policy(Bucket=bucket_name_1, Policy=bucket_policy)
    session_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":[\"s3:PutObject\"],\"Resource\":[\"arn:aws:s3:::test1\",\"arn:aws:s3:::test1/*\"]}}"

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token,Policy=session_policy)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
                aws_access_key_id = resp['Credentials']['AccessKeyId'],
                aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
                aws_session_token = resp['Credentials']['SessionToken'],
                endpoint_url=default_endpoint,
                region_name='',
                )
    bucket_body = 'this is a test file'

    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name_1, Key="test-1.txt")
    except ClientError as e:
        s3putobj_error = e.response.get("Error", {}).get("Code")
    assert s3putobj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_arn
    )

def test_assume_role_with_web_identity_with_sub():
    """Verify sub trust claim accepts actual supplied JWT and temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    sub=get_sub()
    token=get_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":sub\":\""+sub+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\"}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_with_azp():
    """Verify azp trust claim accepts actual supplied JWT and temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    azp=get_azp()
    token=get_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\"],\"Condition\":{\"StringEquals\":{\"localhost:8080/auth/realms/"+realm+":azp\":\""+azp+"\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\"}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_with_request_tag():
    """Verify RequestTag Department trust with actual user JWT permits temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\"}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_with_principal_tag():
    """Verify PrincipalTag Department role policy with actual user JWT permits bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"aws:PrincipalTag/Department\":\"Engineering\"}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_for_all_values():
    """Verify original ForAllValues conditions permit temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"ForAllValues:StringEquals\":{\"aws:PrincipalTag/Department\":[\"Engineering\",\"Marketing\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_for_all_values_deny():
    """Verify original ForAllValues negative policy denies bucket create."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    #ForAllValues: The condition returns true if every key value in the request matches at least one value in the policy
    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"ForAllValues:StringEquals\":{\"aws:PrincipalTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    try:
        s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    except ClientError as e:
        s3bucket_error = e.response.get("Error", {}).get("Code")
    assert s3bucket_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_tag_keys_trust_policy():
    """Verify original tag-key trust condition permits temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:TagKeys\":\"Department\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"ForAnyValue:StringEquals\":{\"aws:PrincipalTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_tag_keys_role_policy():
    """Verify original tag-key role policy permits temporary bucket create/delete."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"aws:TagKeys\":[\"Department\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200
    bkt = s3_client.delete_bucket(Bucket=bucket_name)
    assert bkt['ResponseMetadata']['HTTPStatusCode'] == 204

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_resource_tag():
    """Verify duplicate Department bucket tags are transmitted unchanged and original resource-tag PUT succeeds."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'Engineering'},{'Key':'Department', 'Value': 'Marketing'}]})

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_resource_tag_deny():
    """Verify missing original resource tag denies session PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_wrong_resource_tag_deny():
    """Verify incorrect original resource tag denies session PUT."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'WrongResourcetag'}]})

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    try:
        s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key="test-1.txt")
    except ClientError as e:
        s3_put_obj_error = e.response.get("Error", {}).get("Code")
    assert s3_put_obj_error == 'AccessDenied'

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_resource_tag_princ_tag():
    """Verify principal-derived resource tag permits original duplicate-tag PUT and GET."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'Engineering'}]})

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"${aws:PrincipalTag/Department}\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    tags = 'Department=Engineering&Department=Marketing'
    key = "test-1.txt"
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key=key, Tagging=tags)
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_get_obj = s3_client.get_object(Bucket=bucket_name, Key=key)
    assert s3_get_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_resource_tag_copy_obj():
    """Verify original resource-tag copies within/across both real buckets and all destination GET statuses."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    #create two buckets and add same tags to both
    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'Engineering'}]})

    copy_bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=copy_bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(copy_bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'Engineering'}]})

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"aws:RequestTag/Department\":\"Engineering\"}}}]}"
    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"${aws:PrincipalTag/Department}\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    tags = 'Department=Engineering'
    key = "test-1.txt"
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key=key, Tagging=tags)
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    #copy to same bucket
    copy_source = {
    'Bucket': bucket_name,
    'Key': 'test-1.txt'
    }

    s3_client.copy(copy_source, bucket_name, "test-2.txt")

    s3_get_obj = s3_client.get_object(Bucket=bucket_name, Key="test-2.txt")
    assert s3_get_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    #copy to another bucket
    copy_source = {
    'Bucket': bucket_name,
    'Key': 'test-1.txt'
    }

    s3_client.copy(copy_source, copy_bucket_name, "test-1.txt")

    s3_get_obj = s3_client.get_object(Bucket=copy_bucket_name, Key="test-1.txt")
    assert s3_get_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )

def test_assume_role_with_web_identity_role_resource_tag():
    """Verify duplicate role/bucket Department tags remain unchanged and original role-resource-tag PUT succeeds."""
    check_webidentity()
    iam_client=get_iam_client()
    sts_client=get_sts_client()
    default_endpoint=get_config_endpoint()
    role_session_name=get_parameter_name()
    thumbprint=get_thumbprint()
    user_token=get_user_token()
    realm=get_realm_name()

    s3_res_iam_creds = get_s3_resource_using_iam_creds()

    s3_client_iam_creds = s3_res_iam_creds.meta.client

    bucket_name = get_new_bucket_name()
    s3bucket = s3_client_iam_creds.create_bucket(Bucket=bucket_name)
    assert s3bucket['ResponseMetadata']['HTTPStatusCode'] == 200

    bucket_tagging = s3_res_iam_creds.BucketTagging(bucket_name)
    Set_Tag = bucket_tagging.put(Tagging={'TagSet':[{'Key':'Department', 'Value': 'Engineering'},{'Key':'Department', 'Value': 'Marketing'}]})

    oidc_response = iam_client.create_open_id_connect_provider(
    Url='http://localhost:8080/auth/realms/{}'.format(realm),
    ThumbprintList=[
        thumbprint,
    ],
    )

    #iam:ResourceTag refers to the tag attached to role, hence the role is allowed to be assumed only when it has a tag matching the policy.
    policy_document = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Federated\":[\""+oidc_response["OpenIDConnectProviderArn"]+"\"]},\"Action\":[\"sts:AssumeRoleWithWebIdentity\",\"sts:TagSession\"],\"Condition\":{\"StringEquals\":{\"iam:ResourceTag/Department\":\"Engineering\"}}}]}"
    tags_list = [
            {'Key':'Department','Value':'Engineering'},
            {'Key':'Department','Value':'Marketing'}
        ]

    (role_error,role_response,general_role_name)=create_role(iam_client,'/',None,policy_document,None,None,None,tags_list)
    assert role_response['Role']['Arn'] == 'arn:aws:iam:::role/'+general_role_name+''

    role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":{\"Effect\":\"Allow\",\"Action\":\"s3:*\",\"Resource\":\"arn:aws:s3:::*\",\"Condition\":{\"StringEquals\":{\"s3:ResourceTag/Department\":[\"Engineering\"]}}}}"
    (role_err,response)=put_role_policy(iam_client,general_role_name,None,role_policy)
    assert response['ResponseMetadata']['HTTPStatusCode'] == 200

    resp=sts_client.assume_role_with_web_identity(RoleArn=role_response['Role']['Arn'],RoleSessionName=role_session_name,WebIdentityToken=user_token)
    assert resp['ResponseMetadata']['HTTPStatusCode'] == 200

    s3_client = boto3.client('s3',
        aws_access_key_id = resp['Credentials']['AccessKeyId'],
        aws_secret_access_key = resp['Credentials']['SecretAccessKey'],
        aws_session_token = resp['Credentials']['SessionToken'],
        endpoint_url=default_endpoint,
        region_name='',
        )

    bucket_body = 'this is a test file'
    s3_put_obj = s3_client.put_object(Body=bucket_body, Bucket=bucket_name, Key="test-1.txt")
    assert s3_put_obj['ResponseMetadata']['HTTPStatusCode'] == 200

    oidc_remove=iam_client.delete_open_id_connect_provider(
    OpenIDConnectProviderArn=oidc_response["OpenIDConnectProviderArn"]
    )
