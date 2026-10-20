"""Independent IAM/STS/S3 scripts for five original role authorization transitions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote,parse_qs,urlsplit
import uuid
from osv.ceph_iam_assumed_roles import assumed
from osv.scenarios import Context
from osv.suites import Probe
from test_iam_accounts_sdk import PREFIX,PATH,entity,iam_fixture,verify_requests
from test_iam_cross_resources_sdk import AuditHandler,BUCKET,EMPTY,NO,OK,DEL,DENIED


def role_plans(mode):
    iam=[];identities={};name=PREFIX+('MyUser' if mode=='list_buckets' else 'AltUser');role_name=PREFIX+'MyRole';person=entity('User',name);person['UserId']='actual-role-user-id';person['Arn']='arn:aws:iam::'+('123456789012' if mode=='list_buckets' else '999999999999')+':user'+PATH+name
    def add(op,values=None,data=None,code=None,identity='iam-root-real'):
        if identity!='iam-root-real':identities[len(iam)]=identity
        iam.append((op,values or {},data,code))
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    alt=mode!='list_buckets';credential='iam-alt-root-real' if alt else 'iam-root-real'
    if alt:add('GetUser',data={'User':{'Arn':'arn:aws:iam::999999999999:root'}},identity=credential)
    add('CreateUser',{'UserName':name,'Path':PATH},{'User':person},identity=credential)
    def key():add('CreateAccessKey',{'UserName':name},{'AccessKey':{'AccessKeyId':'FRESH_ROLE_USER_KEY','SecretAccessKey':'actual-role-user-secret'}},identity=credential)
    if alt:key()
    trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':person['Arn']}}]});role=entity('Role',role_name,uid='actual-fresh-role-id');role['AssumeRolePolicyDocument']=quote(trust,safe='');add('CreateRole',{'RoleName':role_name,'Path':PATH,'AssumeRolePolicyDocument':trust},{'Role':role})
    if not alt:key()
    if mode=='get_role':add('GetRole',{'RoleName':role_name},code='AccessDenied',identity='ACTUAL_ASSUMED_ROLE_KEY')
    action='s3:ListAllMyBuckets' if mode=='list_buckets' else 's3:ListBucket' if mode in ('same_account','cross_account') else ['s3:CreateBucket','s3:PutBucketAcl'] if mode=='create_bucket' else 'iam:GetRole';policy_name='AllowListAllMyBuckets' if mode=='list_buckets' else 'AllowListBucket' if mode in ('same_account','cross_account') else 'AllowCreateBucket' if mode=='create_bucket' else 'AllowGetRole'
    document=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':action,'Resource':'*'}]});add('PutRolePolicy',{'RoleName':role_name,'PolicyName':policy_name,'PolicyDocument':document})
    if mode=='get_role':add('GetRole',{'RoleName':role_name},{'Role':role},identity='ACTUAL_ASSUMED_ROLE_KEY')
    if mode=='cross_account':add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    def get_user():add('GetUser',{'UserName':name},{'User':person},identity=credential)
    def get_role():add('GetRole',{'RoleName':role_name},{'Role':role})
    if alt:get_user();add('DeleteAccessKey',{'UserName':name,'AccessKeyId':'FRESH_ROLE_USER_KEY'},identity=credential);get_user();add('DeleteUser',{'UserName':name},identity=credential)
    get_role();add('DeleteRolePolicy',{'RoleName':role_name,'PolicyName':policy_name})
    if not alt:get_user();add('DeleteAccessKey',{'UserName':name,'AccessKeyId':'FRESH_ROLE_USER_KEY'})
    get_role();add('DeleteRole',{'RoleName':role_name})
    if not alt:get_user();add('DeleteUser',{'UserName':name})
    sts=[('AssumeRole',{'RoleArn':role['Arn'],'RoleSessionName':'MySession'},{'Credentials':{'AccessKeyId':'ACTUAL_ASSUMED_ROLE_KEY','SecretAccessKey':'actual-assumed-role-secret','SessionToken':'actual-assumed-role-token','Expiration':'2030-01-01T00:00:00Z'},'AssumedRoleUser':{'AssumedRoleId':'actual-fresh-role-id:MySession','Arn':'arn:aws:sts::123456789012:assumed-role/'+role_name+'/MySession'}},None)]
    cleanup=[OK,(200,{},EMPTY),DEL,NO]
    if mode=='list_buckets':s3=[DENIED,(200,{},b'<ListAllMyBucketsResult><Buckets/></ListAllMyBucketsResult>')]
    elif mode=='same_account':s3=[NO,OK,DENIED,(200,{},EMPTY)]+cleanup
    elif mode=='cross_account':s3=[NO,OK,DENIED,DENIED,OK,(200,{},EMPTY),DEL]+cleanup
    elif mode=='create_bucket':
        acl=b'<AccessControlPolicy><Owner><ID>independent-source-account</ID></Owner><AccessControlList><Grant><Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="CanonicalUser"><ID>independent-source-account</ID></Grantee><Permission>FULL_CONTROL</Permission></Grant></AccessControlList></AccessControlPolicy>'
        s3=[NO,DENIED,OK,(200,{},acl)]+cleanup
    else:s3=[]
    return iam,sts,s3,identities


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable original assumed-role SDK contracts')
class IamAssumedRolesSdkTest(unittest.TestCase):
    """Validate native fresh-user and assumed-role credentials across actual original account/resource boundaries."""
    def test_five_role_sources_keep_original_two_stage_denials_policies_and_ownership(self):
        """Execute all five complete original role sources on both SDKs; expect actual fresh-user STS signatures, returned key/token S3/IAM signatures, exact role/bucket grants, cross-account denial before resource policy and independent Owner/Grants account assertions."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('list_buckets','same_account','cross_account','create_bucket','get_role'):
                iam,sts,s3,identities=role_plans(mode);audit=[]
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,handler=AuditHandler,sts_plan=sts) as (worker,server):
                    server.kind='iam';server.audit=audit;server.s3.kind='s3';server.s3.audit=audit;server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_root_account_id':'independent-source-account'}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):assumed(p,mode)
                    verify_requests(self,server,iam,identities);self.assertFalse(server.s3.fixtures);self.assertFalse(server.sts.fixtures);self.assertEqual(len(server.s3.requests),len(s3));self.assertEqual(len(server.sts.requests),1)
                    method,path,headers,body=server.sts.requests[0];query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query);self.assertEqual(query['Action'],['AssumeRole']);self.assertEqual(query['RoleSessionName'],['MySession']);self.assertIn('Credential=FRESH_ROLE_USER_KEY/',{k.lower():v for k,v in headers.items()}['authorization'])
                    temporary_requests=[]
                    for method,path,headers,body in server.s3.requests+server.requests:
                        lower={k.lower():v for k,v in headers.items()}
                        if 'Credential=ACTUAL_ASSUMED_ROLE_KEY/' in lower['authorization']:temporary_requests.append((method,path,lower,body));self.assertEqual(lower['x-amz-security-token'],'actual-assumed-role-token')
                    self.assertGreaterEqual(len(temporary_requests),2)
                    if mode=='create_bucket':
                        creates=[request for request in temporary_requests if request[0]=='PUT'];self.assertEqual(len(creates),2)
                        for method,path,headers,body in creates:self.assertEqual(headers['x-amz-object-ownership'],'ObjectWriter');self.assertEqual(headers['x-amz-acl'],'private')
                    if mode=='cross_account':
                        resource=[json.loads(body) for method,path,headers,body in server.s3.requests if method=='PUT' and 'policy' in path];self.assertEqual(resource[0]['Statement'][0]['Principal'],{'AWS':'arn:aws:iam::123456789012:root'})
