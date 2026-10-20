"""Actual IAM→STS→STS Query XML contracts, without token mocks or SDK substitutes."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit,quote
import uuid
from osv.ceph_sts_callers import root,user
from osv.scenarios import Context
from osv.suites import Probe
from test_iam_accounts_sdk import PREFIX,PATH,entity,iam_fixture,verify_requests


def caller_plans(mode):
    iam=[];sts=[];name=PREFIX+'MyUser';role_name=PREFIX+'MyRole';person=entity('User',name);key={'AccessKeyId':'FRESH_CALLER_KEY','SecretAccessKey':'fresh-caller-secret'}
    def add(op,values=None,data=None,code=None):iam.append((op,values or {},data,code))
    if mode=='root':sts.append(('GetCallerIdentity',{}, {'UserId':'123456789012','Account':'123456789012','Arn':'arn:aws:iam::123456789012:root'},None));return iam,sts
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}});add('CreateUser',{'UserName':name,'Path':PATH},{'User':person});add('CreateAccessKey',{'UserName':name},{'AccessKey':key})
    if mode=='assume':
        trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':person['Arn']}}]});role=entity('Role',role_name,uid='fresh-role-id');role['AssumeRolePolicyDocument']=quote(trust,safe='')
        add('CreateRole',{'RoleName':role_name,'Path':PATH,'AssumeRolePolicyDocument':trust},{'Role':role})
        session='arn:aws:sts::123456789012:assumed-role/'+role_name+'/MySession';temporary={'AccessKeyId':'TEMPORARY_STS_CALLER_KEY','SecretAccessKey':'temporary-sts-secret','SessionToken':'actual-temporary-session-token','Expiration':'2028-01-01T00:00:00Z'}
        sts.append(('AssumeRole',{'RoleArn':role['Arn'],'RoleSessionName':'MySession'},{'Credentials':temporary,'AssumedRoleUser':{'AssumedRoleId':'fresh-role-id:MySession','Arn':session}},None));sts.append(('GetCallerIdentity',{}, {'UserId':'fresh-role-id:MySession','Account':'123456789012','Arn':session},None));add('DeleteRole',{'RoleName':role_name})
    else:sts.append(('GetCallerIdentity',{}, {'UserId':person['UserId'],'Account':'123456789012','Arn':person['Arn']},None))
    add('DeleteAccessKey',{'UserName':name,'AccessKeyId':key['AccessKeyId']});add('DeleteUser',{'UserName':name})
    # Cleanup rechecks actual immutable identities; the source itself has
    # already removed every entity, so NoSuchEntity is the only tolerated error.
    add('GetUser',{'UserName':name},code='NoSuchEntity')
    if mode=='assume':add('GetRole',{'RoleName':role_name},code='NoSuchEntity')
    add('GetUser',{'UserName':name},code='NoSuchEntity');return iam,sts


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM/STS SDK Query contracts')
class StsCallerSdkTest(unittest.TestCase):
    """Use native returned credentials directly in real subsequent signed STS requests."""
    def test_three_original_caller_sources_use_real_two_stage_native_credentials(self):
        """Run root/user/assumed-role caller sources on both official SDKs; expect all exact UserId/Account/Arn assertions, actual root/fresh/temporary credential scopes and actual SessionToken header in the final consumer."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('root','user','assume'):
                iam,sts=caller_plans(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_root_user_id':'123456789012'}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                        root(p) if mode=='root' else user(p,mode=='assume')
                    verify_requests(self,server,iam);self.assertFalse(server.sts.fixtures);self.assertEqual(len(server.sts.requests),len(sts))
                    for index,(method,path,headers,body) in enumerate(server.sts.requests):
                        query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);op,values,data,code=sts[index];self.assertEqual(query['Action'],[op]);self.assertEqual(query['Version'],['2011-06-15'])
                        for key,value in values.items():self.assertEqual(query[key],[str(value)])
                        lower={k.lower():v for k,v in headers.items()};credential='iam-root-real' if mode=='root' else 'FRESH_CALLER_KEY' if index==0 else 'TEMPORARY_STS_CALLER_KEY'
                        self.assertIn('Credential='+credential+'/',lower['authorization']);self.assertIn('/us-east-1/sts/aws4_request',lower['authorization'])
                        if mode=='assume' and index==1:self.assertEqual(lower['x-amz-security-token'],'actual-temporary-session-token')
