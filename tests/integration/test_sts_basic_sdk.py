"""Original basic STS workflows; long expiry validation never replaces the real source clock."""
import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit,quote
import uuid
from osv.ceph_sts_basic import assume,session_token
from osv.scenarios import Context
from osv.suites import Probe
from test_iam_accounts_sdk import iam_fixture,entity

UUID=uuid.UUID(int=1);ROLE='osvrole'+UUID.hex;POLICY='osvpolicy'+UUID.hex;SESSION='osvsession'+UUID.hex;USER='dedicated-alt-user'
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":["arn:aws:iam:::user/dedicated-alt-user"]},"Action":["sts:AssumeRole"]}]}'
ALLOW='{"Version":"2012-10-17","Statement":{"Effect":"Allow","Action":"s3:*","Resource":"arn:aws:s3:::*"}}'


def expiry_plans():
    role=entity('Role',ROLE,'/',uid='actual-legacy-role-id');role['Arn']='arn:aws:iam:::role/'+ROLE;role['AssumeRolePolicyDocument']=quote(TRUST,safe='')
    iam=[('CreateRole',{'RoleName':ROLE,'Path':'/','AssumeRolePolicyDocument':TRUST},{'Role':role},None),('PutRolePolicy',{'RoleName':ROLE,'PolicyName':POLICY,'PolicyDocument':ALLOW},{},None),('GetRole',{'RoleName':ROLE},{'Role':role},None),('DeleteRolePolicy',{'RoleName':ROLE,'PolicyName':POLICY},{},None),('DeleteRole',{'RoleName':ROLE},{},None)]
    expiration=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=900)).isoformat().replace('+00:00','Z')
    sts=[('AssumeRole',{'RoleArn':role['Arn'],'RoleSessionName':SESSION,'DurationSeconds':900},{'Credentials':{'AccessKeyId':'ACTUAL_EXPIRY_SESSION_KEY','SecretAccessKey':'actual-expiry-session-secret','SessionToken':'actual-expiry-token','Expiration':expiration},'AssumedRoleUser':{'AssumedRoleId':'actual-legacy-role-id:'+SESSION,'Arn':'arn:aws:sts::123456789012:assumed-role/'+ROLE+'/'+SESSION}},None)]
    return iam,sts


def basic_plans(mode):
    iam,sts=expiry_plans();sts[0][1].pop('DurationSeconds')
    if mode=='deny':document='{"Version":"2012-10-17","Statement":{"Effect":"Deny","Action":"s3:*","Resource":"arn:aws:s3:::*"}}'
    elif mode=='deny_head':document='{"Version":"2012-10-17","Statement":{"Effect":"Allow","Action":"s3:GetObject","Principal":"*","Resource":"arn:aws:s3:::*"}}'
    elif mode=='allow_head':document='{"Version":"2012-10-17","Statement":{"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Principal":"*","Resource":"arn:aws:s3:::*"}}'
    else:document=ALLOW
    iam[1][1]['PolicyDocument']=document
    no=(404,{},b'');ok=(200,{},b'');delete=(204,{},b'');denied=(403,{},b'<Error><Code>AccessDenied</Code></Error>');empty=(200,{},b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>')
    if mode=='allow':s3=[no,ok,delete,no]
    elif mode=='deny':s3=[no,denied]
    else:s3=[no,ok,(403 if mode=='deny_head' else 404,{},b''),ok,empty,delete,no]
    return iam,sts,s3


def token_plans():
    document='{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"s3:*","Resource":["*"],"Condition":{"BoolIfExists":{"sts:authentication":"false"}}},{"Effect":"Allow","Action":"sts:GetSessionToken","Resource":"*","Condition":{"BoolIfExists":{"sts:authentication":"false"}}}]}'
    owner={'UserName':USER,'UserId':'actual-dedicated-alt-id'};args={'UserName':USER,'PolicyName':POLICY};iam=[('GetUser',{'UserName':USER},{'User':owner},None),('ListUserPolicies',{'UserName':USER},{'PolicyNames':[],'IsTruncated':False},None),('GetUserPolicy',args,None,'NoSuchEntity'),('GetUser',{'UserName':'some-non-existing-user-id'},None,'NoSuchEntity'),('PutUserPolicy',{**args,'PolicyDocument':document},{},None),('DeleteUserPolicy',args,{},None),('GetUser',{'UserName':USER},{'User':owner},None),('DeleteUserPolicy',args,None,'NoSuchEntity')]
    creds={'AccessKeyId':'ACTUAL_GET_SESSION_KEY','SecretAccessKey':'actual-get-session-secret','SessionToken':'actual-get-session-token','Expiration':'2030-01-01T00:00:00Z'};sts=[('GetSessionToken',{}, {'Credentials':creds},None)];s3=[(404,{},b''),(200,{},b''),(204,{},b''),(404,{},b'')];return iam,sts,s3


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real basic STS native SDK contracts')
class StsBasicSdkTest(unittest.TestCase):
    """Check all five short original session/role sources with unchanged raw input vectors."""
    def test_five_short_sources_keep_legacy_arns_original_json_and_actual_alt_temporary_identities(self):
        """Run GetSessionToken, allow/deny bucket creation and deny/allow missing-object HEAD through both SDKs; expect exact legacy trust/policy strings, alt STS signatures, native temporary-token S3 signatures and all original status/code assertions."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('allow','deny','deny_head','allow_head','token'):
                iam,sts,s3=token_plans() if mode=='token' else basic_plans(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
                    server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}))
                    with patch('osv.ceph_sts_legacy_scope.uuid.uuid4',return_value=UUID):session_token(p) if mode=='token' else assume(p,mode)
                    self.assertFalse(server.fixtures);self.assertFalse(server.sts.fixtures);self.assertFalse(server.s3.fixtures);self.assertEqual(len(server.requests),len(iam));self.assertEqual(len(server.s3.requests),len(s3))
                    for request,(operation,values,data,code) in zip(server.requests,iam):
                        method,path,headers,body=request;query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);self.assertEqual(query['Action'],[operation])
                        for key,value in values.items():self.assertEqual(query[key],[str(value)])
                        self.assertIn('Credential=iam-tenant-real/',{k.lower():v for k,v in headers.items()}['authorization'])
                    method,path,headers,body=server.sts.requests[0];query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);self.assertNotIn('DurationSeconds',query);self.assertEqual(query['Action'],[sts[0][0]]);self.assertIn('Credential=s3-alt-real/',{k.lower():v for k,v in headers.items()}['authorization'])
                    temporary='ACTUAL_GET_SESSION_KEY' if mode=='token' else 'ACTUAL_EXPIRY_SESSION_KEY';requests=[request for request in server.s3.requests if 'Credential='+temporary+'/' in {k.lower():v for k,v in request[2].items()}['authorization']];self.assertTrue(requests)
                    for method,path,headers,body in requests:self.assertEqual({k.lower():v for k,v in headers.items()}['x-amz-security-token'],'actual-get-session-token' if mode=='token' else 'actual-expiry-token')


@unittest.skipUnless(os.environ.get('OSV_RUN_STS_EXPIRY_TESTS')=='1','explicit real 900-second expiry source test')
class StsExpirySdkTest(unittest.TestCase):
    """Verify original SDK protocol and actual elapsed wait; this raw fixture does not establish a cloud provider's expiration semantics."""
    def test_original_expiry_waits_full_nine_hundred_seconds_and_keeps_real_temporary_signature(self):
        """Run the unmodified DurationSeconds900/real sleep900 source on each selected SDK; expect at least 900 actual seconds, native ExpiredToken HTTP400 decoding and the generated temporary key/token on the real final S3 request."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            iam,sts=expiry_plans()
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
                server.s3.fixtures.extend([(404,{},b''),(400,{},b'<Error><Code>ExpiredToken</Code><Message>Raw SDK expiry oracle after the original wait</Message></Error>')]);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}));started=time.monotonic()
                with patch('osv.ceph_sts_legacy_scope.uuid.uuid4',return_value=UUID):assume(p,'expiry')
                self.assertGreaterEqual(time.monotonic()-started,900);self.assertFalse(server.fixtures);self.assertFalse(server.sts.fixtures);self.assertFalse(server.s3.fixtures);self.assertEqual(len(server.requests),5);self.assertEqual(len(server.sts.requests),1);self.assertEqual(len(server.s3.requests),2)
                for request,(operation,values,data,code) in zip(server.requests,iam):
                    method,path,headers,body=request;query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);self.assertEqual(query['Action'],[operation])
                    for key,value in values.items():self.assertEqual(query[key],[str(value)])
                    self.assertIn('Credential=iam-tenant-real/',{k.lower():v for k,v in headers.items()}['authorization'])
                method,path,headers,body=server.sts.requests[0];query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);self.assertEqual(query['DurationSeconds'],['900']);self.assertEqual(query['RoleArn'],['arn:aws:iam:::role/'+ROLE]);self.assertIn('Credential=s3-alt-real/',{k.lower():v for k,v in headers.items()}['authorization'])
                method,path,headers,body=server.s3.requests[-1];self.assertEqual(method,'PUT');lower={k.lower():v for k,v in headers.items()};self.assertIn('Credential=ACTUAL_EXPIRY_SESSION_KEY/',lower['authorization']);self.assertEqual(lower['x-amz-security-token'],'actual-expiry-token')
