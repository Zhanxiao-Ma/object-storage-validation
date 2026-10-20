"""Actual cross-account SDK signatures and cross-service ordering from independent wire replies."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import uuid
from osv.ceph_iam_cross_resources import cross,SPECS
from osv.scenarios import Context
from osv.suites import Probe
from sdk_fixtures import FixtureHandler
from test_iam_accounts_sdk import PREFIX,PATH,entity,iam_fixture,verify_requests

BUCKET='osv-'+uuid.UUID(int=1).hex
EMPTY=b'<ListBucketResult><Name>'+BUCKET.encode()+b'</Name><IsTruncated>false</IsTruncated></ListBucketResult>'
OK=(200,{},b'');DEL=(204,{},b'');NO=(404,{},b'');DENIED=(403,{},b'<Error><Code>AccessDenied</Code></Error>')


class AuditHandler(FixtureHandler):
    """Record arrival times from real native HTTP, sharing no storage or authorization implementation."""
    def respond(self):
        started=time.monotonic();index=len(self.server.requests);super().respond()
        self.server.audit.append((started,self.server.kind,self.server.requests[index]))
    do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond


def plans(resource,principal,first,root=False,same=False):
    iam=[];identities={};name=PREFIX+('name' if same else 'AltUser');user=entity('User',name);user['Arn']='arn:aws:iam::'+('123456789012' if same else '999999999999')+':user'+PATH+name;user['UserId']='actual-fresh-user-id'
    def add(op,values=None,data=None,code=None,alt=False):
        if alt:identities[len(iam)]='iam-alt-root-real'
        iam.append((op,values or {},data,code))
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    if not same:add('GetUser',data={'User':{'Arn':'arn:aws:iam::999999999999:root'}},alt=True)
    if not root:add('CreateUser',{'UserName':name,'Path':PATH},{'User':user},alt=not same)
    if root and principal=='account_arn':add('GetUser',data={'User':{'Arn':'arn:aws:iam::999999999999:root'}},alt=True)
    document=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]})
    def user_policy():add('PutUserPolicy',{'UserName':name,'PolicyName':'AllowStar','PolicyDocument':document},alt=True)
    if not root and not same and first=='user':user_policy()
    if not root:add('CreateAccessKey',{'UserName':name},{'AccessKey':{'AccessKeyId':'FRESH_CROSS_KEY','SecretAccessKey':'actual-fresh-cross-secret'}},alt=not same)
    if not root and not same and first=='bucket':user_policy()
    if not root and not same:add('GetUser',{'UserName':name},{'User':user},alt=True);add('DeleteUserPolicy',{'UserName':name,'PolicyName':'AllowStar'},alt=True)
    if not root:
        add('GetUser',{'UserName':name},{'User':user},alt=not same);add('DeleteAccessKey',{'UserName':name,'AccessKeyId':'FRESH_CROSS_KEY'},alt=not same);add('GetUser',{'UserName':name},{'User':user},alt=not same);add('DeleteUser',{'UserName':name},alt=not same)
    grant_first=not root and not same and first=='bucket'
    s3=[NO,OK]+([OK,DENIED] if grant_first else [DENIED,OK])+[(200,{},EMPTY)]+([DEL] if resource=='policy' else [])+[OK,(200,{},EMPTY),DEL,NO]
    return iam,s3,identities,user


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM/S3 cross-account SDK contracts')
class IamCrossResourcesSdkTest(unittest.TestCase):
    """Validate all original identities and policy/ACL ordering through actual native wire calls."""
    def test_seventeen_original_policy_acl_matrices_keep_grants_order_and_identities(self):
        """Execute all seventeen sources on both SDKs; expect exact raw user/canonical/email grants and principals, real fresh-key/root signatures, and source-mandated user-policy-before-resource or resource-before-user-policy HTTP ordering."""
        variants=[('policy',principal,first,False,False) for principal in ('user_arn','account_arn','account_id') for first in ('bucket','user')]+[('acl',principal,first,False,False) for principal in ('user_id','canonical_id','account_email') for first in ('bucket','user')]+[('policy',principal,'bucket',True,False) for principal in ('account_arn','account_id')]+[('acl',principal,'bucket',True,False) for principal in ('canonical_id','account_email')]+[('policy','user_arn','bucket',False,True)]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for resource,principal,first,root,same in variants:
                iam,s3,identities,created=plans(resource,principal,first,root,same);audit=[]
                with self.subTest(language=language,resource=resource,principal=principal,first=first,root=root,same=same),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,handler=AuditHandler) as (worker,server):
                    server.kind='iam';server.audit=audit;server.s3.kind='s3';server.s3.audit=audit;server.s3.fixtures.extend(s3)
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_root_account_id':'independent-account-id','iam_alt_root_user_id':'independent-canonical-id','iam_alt_root_email':'independent-account@example.com'}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):cross(p,resource,principal,first,root,same)
                    verify_requests(self,server,iam,identities);self.assertFalse(server.s3.fixtures);self.assertEqual(len(server.s3.requests),len(s3))
                    grants=[];consumers=[]
                    for method,path,headers,body in server.s3.requests:
                        lower={k.lower():v for k,v in headers.items()};self.assertIn('/'+BUCKET,path)
                        if method=='GET' and 'policy' not in path and 'acl' not in path:
                            consumers.append(lower['authorization'])
                            if len(consumers)<=2:self.assertIn('Credential='+('iam-alt-root-real' if root else 'FRESH_CROSS_KEY')+'/',lower['authorization']);continue
                        self.assertIn('Credential=iam-root-real/',lower['authorization'])
                        if method=='PUT' and ('policy' in path or 'acl' in path):grants.append((path,lower,body))
                    self.assertEqual(len(grants),1)
                    if resource=='policy':
                        expected=created['Arn'] if principal=='user_arn' else 'arn:aws:iam::999999999999:root' if principal=='account_arn' else 'independent-account-id';document=json.loads(grants[0][2]);self.assertEqual(document['Statement'][0]['Principal'],{'AWS':expected});self.assertEqual(document['Statement'][0]['Resource'],'arn:aws:s3:::'+BUCKET)
                    else:self.assertEqual(grants[0][1]['x-amz-grant-read'],('emailAddress=independent-account@example.com' if principal=='account_email' else 'id=actual-fresh-user-id' if principal=='user_id' else 'id=independent-canonical-id'))
                    ordered=sorted(audit);policy_positions=[i for i,(_,kind,request) in enumerate(ordered) if kind=='iam' and parse_qs(request[3].decode()).get('Action')==['PutUserPolicy']];grant_position=next(i for i,(_,kind,request) in enumerate(ordered) if kind=='s3' and request[0]=='PUT' and ('policy' in request[1] or 'acl' in request[1]))
                    if not root and not same:
                        self.assertEqual(len(policy_positions),1);self.assertLess(policy_positions[0],grant_position) if first=='user' else self.assertLess(grant_position,policy_positions[0])
