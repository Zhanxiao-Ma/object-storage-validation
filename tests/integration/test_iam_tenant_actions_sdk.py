"""Real native IAM/S3 requests for all eleven original tenant policy action sources."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
import uuid
from osv.ceph_iam_tenant_actions import actions,allow_iam
from osv.scenarios import Context
from osv.suites import Probe
from test_iam_accounts_sdk import iam_fixture,verify_requests
from test_iam_cross_resources_sdk import BUCKET,EMPTY,NO,OK,DEL,DENIED

USER='dedicated-alt-user';NAMES=('AllAccessPolicy','AllAccessPolicy'*10,'AllowAccessPolicy','AllowAccessPolicy1','AllowAccessPolicy2','AllowAccessPolicy3','non-existing-policy-name')
TAGS=b'<Tagging><TagSet><Tag><Key>Hello</Key><Value>World</Value></Tag></TagSet></Tagging>'


def plans(kind,deny=False):
    iam=[];identities={};s3=[];signatures=[];methods=[];owner={'UserName':USER,'UserId':'actual-dedicated-alt-id'};written=[]
    def add(op,values=None,data=None,code=None,alt=False):
        if alt:identities[len(iam)]='s3-alt-real'
        else:identities[len(iam)]='iam-tenant-real'
        iam.append((op,values or {},data,code))
    def wire(reply,method,credential):s3.append(reply);methods.append(method);signatures.append(credential)
    def get_user():add('GetUser',{'UserName':USER},{'User':owner})
    get_user();add('ListUserPolicies',{'UserName':USER},{'PolicyNames':[],'IsTruncated':False})
    for name in NAMES+(() if kind=='iam' else ('DenyAccessPolicy',)):add('GetUserPolicy',{'UserName':USER,'PolicyName':name},code='ValidationError' if len(name)>128 else 'NoSuchEntity')
    add('GetUser',{'UserName':'some-non-existing-user-id'},code='NoSuchEntity')
    if kind=='iam':
        document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CGA','Effect':'Allow','Action':['iam:PutUserPolicy','iam:GetUserPolicy','iam:ListUserPolicies','iam:DeleteUserPolicy'],'Resource':'arn:aws:iam:::user/'+USER}};add('PutUserPolicy',{'UserName':USER,'PolicyName':'AllowAccessPolicy','PolicyDocument':json.dumps(document)})
        for operation,values in (('GetUserPolicy',{'PolicyName':'AllowAccessPolicy'}),('ListUserPolicies',{}),('DeleteUserPolicy',{'PolicyName':'AllowAccessPolicy'})):add(operation,{'UserName':USER,**values},{'PolicyNames':[]} if operation=='ListUserPolicies' else {},alt=True)
        get_user();add('DeleteUserPolicy',{'UserName':USER,'PolicyName':'AllowAccessPolicy'},code='NoSuchEntity');return iam,s3,identities,methods,signatures
    source_owner='s3-alt-real' if deny or kind.startswith('conflict') else 'iam-tenant-real';wire(NO,'HEAD',source_owner);wire(OK,'PUT',source_owner)
    resource='arn:aws:s3:::'+BUCKET
    if kind=='bucket':
        if not deny:wire(OK,'PUT','iam-tenant-real');written=['foo']
        document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:ListAllMyBuckets','s3:DeleteBucket'] if deny else ['s3:ListBucket','s3:DeleteBucket'],'Resource':'arn:aws:s3:::*' if deny else resource}}
    elif kind=='object':
        if deny:wire(OK,'PUT','s3-alt-real');written=['foo']
        statement={'Effect':'Deny' if deny else 'Allow','Action':['s3:PutObject','s3:GetObject','s3:DeleteObject'],'Resource':resource+'/*'};document={'Version':'2012-10-17','Statement':[statement,{'Effect':'Allow','Action':['s3:DeleteBucket'],'Resource':resource}] if deny else statement}
    elif kind=='multipart':document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:ListBucketMultipartUploads','s3:AbortMultipartUpload'],'Resource':'arn:aws:s3:::*'}}
    elif kind=='tagging':document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:PutBucketTagging','s3:GetBucketTagging','s3:PutObjectTagging','s3:DeleteObjectTagging' if deny else 's3:GetObjectTagging'],'Resource':'arn:aws:s3:::*'}}
    elif kind=='conflict_statements':document={'Version':'2012-10-17','Statement':[{'Sid':'98AB54CG','Effect':'Allow','Action':'s3:ListBucket','Resource':resource},{'Sid':'98AB54CA','Effect':'Deny','Action':'s3:ListBucket','Resource':resource}]}
    else:document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CG','Effect':'Allow','Action':'s3:ListBucket','Resource':resource}}
    policy='DenyAccessPolicy' if deny or kind=='conflict_statements' else 'AllowAccessPolicy';written_policies=[policy];add('PutUserPolicy',{'UserName':USER,'PolicyName':policy,'PolicyDocument':json.dumps(document)})
    if kind=='conflict_policies':
        written_policies.append('DenyAccessPolicy');document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CGZ','Effect':'Deny','Action':'s3:ListBucket','Resource':resource}};add('PutUserPolicy',{'UserName':USER,'PolicyName':'DenyAccessPolicy','PolicyDocument':json.dumps(document)})
    if kind=='bucket':
        if deny:wire(DENIED,'GET','s3-alt-real');wire(DENIED,'DELETE','s3-alt-real');wire(DEL,'DELETE','s3-alt-real')
        else:
            wire((200,{},b'<ListBucketResult><Contents><Key>foo</Key></Contents></ListBucketResult>'),'GET','s3-alt-real');wire(DEL,'DELETE','iam-tenant-real');wire(DEL,'DELETE','s3-alt-real')
            # Deliberately still list the deleted bucket: the original shadows
            # its variable and never checks membership, so do not strengthen it.
            wire((200,{},b'<ListAllMyBucketsResult><Buckets><Bucket><Name>'+BUCKET.encode()+b'</Name></Bucket></Buckets></ListAllMyBucketsResult>'),'GET','iam-tenant-real')
    elif kind=='object':
        if deny:
            wire(DENIED,'PUT','s3-alt-real');wire(DENIED,'GET','s3-alt-real');wire(DENIED,'DELETE','s3-alt-real')
        else:
            wire(OK,'PUT','s3-alt-real');wire((200,{},b'bar'),'GET','s3-alt-real');wire(DEL,'DELETE','s3-alt-real');wire((404,{},b'<Error><Code>NoSuchKey</Code></Error>'),'GET','iam-tenant-real');wire(DEL,'DELETE','iam-tenant-real');written=['foo']
    elif kind=='multipart':
        wire((200,{},b'<InitiateMultipartUploadResult><Bucket>'+BUCKET.encode()+b'</Bucket><Key>mymultipart</Key><UploadId>actual-source-upload-id</UploadId></InitiateMultipartUploadResult>'),'POST',source_owner);wire((200,{'ETag':'"actual-returned-part-etag"'},b''),'PUT',source_owner)
        wire(DENIED if deny else (200,{},b'<ListMultipartUploadsResult><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>'),'GET','s3-alt-real');wire(DENIED if deny else DEL,'DELETE','s3-alt-real');wire(DEL,'DELETE',source_owner)
    elif kind=='tagging':
        wire(DENIED if deny else OK,'PUT','s3-alt-real');wire(DENIED if deny else (200,{},TAGS),'GET','s3-alt-real');wire(OK,'PUT',source_owner);written=['obj'];wire(DENIED if deny else OK,'PUT','s3-alt-real');wire(DENIED if deny else (200,{},TAGS),'DELETE' if deny else 'GET','s3-alt-real');wire(DEL,'DELETE',source_owner);wire(DEL,'DELETE',source_owner)
    else:wire(DENIED,'GET','s3-alt-real')
    for name in written_policies:add('DeleteUserPolicy',{'UserName':USER,'PolicyName':name})
    def cleanup():
        get_user()
        for name in sorted(written_policies):add('DeleteUserPolicy',{'UserName':USER,'PolicyName':name},code='NoSuchEntity')
    cleanup()
    if kind=='multipart' and deny:wire((404,{},b'<Error><Code>NoSuchBucket</Code></Error>'),'DELETE',source_owner)
    if kind=='object' and deny or kind.startswith('conflict'):
        wire(OK,'HEAD',source_owner)
        for name in written:wire(DEL,'DELETE',source_owner)
        wire((200,{},EMPTY),'GET',source_owner);wire(DEL,'DELETE',source_owner);wire(NO,'HEAD',source_owner)
    else:wire(NO,'HEAD',source_owner)
    cleanup();return iam,s3,identities,methods,signatures


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable original tenant IAM/S3 action contracts')
class IamTenantActionsSdkTest(unittest.TestCase):
    """Keep source documents, SDK consumer credentials and upstream assertion defects observable."""
    def test_eleven_original_tenant_policy_sources_keep_every_native_identity_and_input(self):
        """Execute all eleven tenant sources through both official SDKs; expect exact policy JSON/names, original 5MiB repeated random multipart body and returned UploadId, raw tags/body values, alternate IAM management signatures and self-denial removal before owner cleanup."""
        variants=[(kind,deny) for kind in ('bucket','object','multipart','tagging') for deny in (False,True)]+[('conflict_statements',False),('conflict_policies',False),('iam',False)]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for kind,deny in variants:
                if kind=='multipart' and os.environ.get('OSV_IAM_DEFER_MULTIPART_TESTS')=='1':continue
                iam,s3,identities,methods,signatures=plans(kind,deny)
                with self.subTest(language=language,kind=kind,deny=deny),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam) as (worker,server):
                    server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}))
                    with patch('osv.bucket_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):allow_iam(p) if kind=='iam' else actions(p,kind,deny)
                    verify_requests(self,server,iam,identities);self.assertFalse(server.s3.fixtures);self.assertEqual(len(server.s3.requests),len(s3))
                    for request,method,signature in zip(server.s3.requests,methods,signatures):self.assertEqual(request[0],method);self.assertIn('Credential='+signature+'/',{k.lower():v for k,v in request[2].items()}['authorization'])
                    if kind=='multipart':
                        uploads=[(path,body) for method,path,headers,body in server.s3.requests if method=='PUT' and 'uploadId=' in path];self.assertEqual(len(uploads),1);self.assertIn('uploadId=actual-source-upload-id',uploads[0][0]);body=uploads[0][1];self.assertEqual(len(body),5*1024*1024);self.assertTrue(body.isalpha());self.assertEqual(body,body[:1024]*(5*1024))
                    if kind=='object' and deny:self.assertEqual([body for method,path,headers,body in server.s3.requests if method=='PUT' and '/foo' in path],[b'bar',b''])
                    if kind=='tagging':
                        bodies=[body for method,path,headers,body in server.s3.requests if method=='PUT' and 'tagging' in path];self.assertEqual(len(bodies),2)
                        for body in bodies:self.assertIn(b'<Key>Hello</Key>',body);self.assertIn(b'<Value>World</Value>',body)
