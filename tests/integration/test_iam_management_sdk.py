"""Independent raw IAM vectors for fresh policies, groups and OIDC management sources."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
import uuid
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.ceph_iam_policy_entities import inline_policy,managed_policy,group_create,group_case,group_update,allows
from osv.ceph_iam_accounts import listing
from osv.ceph_iam_role_policies import inline_role,permission,summary
from osv.ceph_iam_oidc import provider
from test_iam_accounts_sdk import PREFIX,PATH,TRUST,entity,iam_fixture,verify_requests,account_plan

FULL='arn:aws:iam::aws:policy/AmazonS3FullAccess'
READ='arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess'


def policy_plan(kind,managed=False,role=False):
    plan=[];name=PREFIX+('r' if role else 'name');item=entity(kind,name);namearg={kind+'Name':name};deps=[]
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    def create():add('Create'+kind,{**namearg,'Path':PATH,**({'AssumeRolePolicyDocument':json.dumps(TRUST)} if kind=='Role' else {})},{kind:item})
    def get():add('Get'+kind,namearg,{kind:item})
    def cleanup():
        for policy in reversed(deps):
            get();add(('Detach' if managed else 'Delete')+kind+'Policy',{**namearg,('PolicyArn' if managed else 'PolicyName'):policy},code='NoSuchEntity' if not managed else None)
        get();add('Delete'+kind,namearg)
    if managed:
        for operation in ('Attach','Detach','ListAttached'):add(operation+kind+('Policies' if operation=='ListAttached' else 'Policy'),{**namearg,**({} if operation=='ListAttached' else {'PolicyArn':FULL})},code='NoSuchEntity')
        create();add('Detach'+kind+'Policy',{**namearg,'PolicyArn':FULL},code='NoSuchEntity')
        for _ in range(2):add('Attach'+kind+'Policy',{**namearg,'PolicyArn':FULL})
        deps.append(FULL)
        one={'AttachedPolicies':[{'PolicyName':'AmazonS3FullAccess','PolicyArn':FULL}]}
        add('ListAttached'+kind+'Policies',namearg,one);add('Attach'+kind+'Policy',{**namearg,'PolicyArn':READ});deps.append(READ)
        add('ListAttached'+kind+'Policies',namearg,{'AttachedPolicies':[{'PolicyName':'AmazonS3ReadOnlyAccess','PolicyArn':READ},{'PolicyName':'AmazonS3FullAccess','PolicyArn':FULL}]})
        add('Detach'+kind+'Policy',{**namearg,'PolicyArn':READ});add('Detach'+kind+'Policy',{**namearg,'PolicyArn':READ},code='NoSuchEntity');add('ListAttached'+kind+'Policies',namearg,one);add('Delete'+kind,namearg,code='DeleteConflict');cleanup();return plan
    policy='MyPolicy' if role else 'List';args={**namearg,'PolicyName':policy}
    documents=[json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]})] if role else [json.dumps({'Version':'2012-10-17','Statement':[{'Effect':effect,'Action':'s3:ListBucket','Resource':'arn:aws:s3:::'+PREFIX+'bucket'}]}) for effect in ('Deny','Allow')]
    for operation in ('Get','Delete','Put'):add(operation+kind+'Policy',{**args,**({'PolicyDocument':documents[0]} if operation=='Put' else {})},code='NoSuchEntity')
    create()
    for operation in ('Get','Delete'):add(operation+kind+'Policy',args,code='NoSuchEntity')
    for document in documents:
        add('Put'+kind+'Policy',{**args,'PolicyDocument':document});add('Get'+kind+'Policy',args,{**args,'PolicyDocument':quote(document,safe='')});add('List'+kind+'Policies',namearg,{'PolicyNames':[policy]})
    deps.append(policy)
    if role:
        add('PutRolePolicy',{**namearg,'PolicyName':'AnotherPolicy','PolicyDocument':documents[0]});deps.append('AnotherPolicy');add('ListRolePolicies',namearg,{'PolicyNames':['AnotherPolicy','MyPolicy']})
        for policyname in ('MyPolicy','AnotherPolicy'):add('DeleteRolePolicy',{**namearg,'PolicyName':policyname})
    else:
        if kind=='Group':add('DeleteGroup',namearg,code='DeleteConflict')
        add('Delete'+kind+'Policy',args)
    for operation in ('Get','Delete'):add(operation+kind+'Policy',args,code='NoSuchEntity')
    if not role:add('List'+kind+'Policies',namearg,{'PolicyNames':[]})
    cleanup();return plan


def oidc_plan(mode):
    plan=[];host=PATH[1:]+'example.com';url='http://'+host;arn='arn:aws:iam::123456789012:oidc-provider/'+host;args={'OpenIDConnectProviderArn':arn};old='3768084dfb3d2b68b7897bf5f565da8efEXAMPLE';new='3768084dfb3d2b68b7897bf5f565da8efSAMPLE1'
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    def get(clients,thumb=old):add('GetOpenIDConnectProvider',args,{'Url':url,'ClientIDList':clients,'ThumbprintList':[thumb]})
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}});add('ListOpenIDConnectProviders',data={'OpenIDConnectProviderList':[]})
    clients=['my-application-id'] if mode=='basic' else ['app-jee-jsp'] if mode=='add_new' else ['app-jee-jsp','app-profile-jsp']
    # Query SDK list member parameters are asserted separately, not represented
    # here as an invented JSON-on-the-wire request.
    add('CreateOpenIDConnectProvider',{'Url':url},{'OpenIDConnectProviderArn':arn})
    if mode=='basic':
        add('ListOpenIDConnectProviders',data={'OpenIDConnectProviderList':[{'Arn':arn}]});get(clients);add('DeleteOpenIDConnectProvider',args);add('ListOpenIDConnectProviders',data={'OpenIDConnectProviderList':[]})
        for op in ('GetOpenIDConnectProvider','DeleteOpenIDConnectProvider'):add(op,args,code='NoSuchEntity')
    else:
        get(clients)
        if mode in ('add_new','add_existing'):add('AddClientIDToOpenIDConnectProvider',{**args,'ClientID':'app-profile-jsp'});get(['app-jee-jsp','app-profile-jsp'])
        elif mode=='remove':add('RemoveClientIDFromOpenIDConnectProvider',{**args,'ClientID':'app-profile-jsp'});get(['app-jee-jsp'])
        else:add('UpdateOpenIDConnectProviderThumbprint',args);get(clients,new)
        add('DeleteOpenIDConnectProvider',args)
    add('GetOpenIDConnectProvider',args,code='NoSuchEntity');return plan,clients,old,new


def group_plan(mode):
    if mode=='list':return account_plan('Group','list')
    plan=[];name=PREFIX+'G1';group=entity('Group',name);args={'GroupName':name}
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    def groups(items):add('ListGroups',{'PathPrefix':PATH},{'Groups':items,'IsTruncated':False})
    def get_group(name,item,users=None):add('GetGroup',{'GroupName':name},{'Group':item,**({'Users':users} if users is not None else {})})
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    if mode=='create':groups([])
    add('CreateGroup',{**args,'Path':PATH},{'Group':group})
    if mode in ('create','case'):
        lower=PREFIX+'g1' if mode=='case' else name;add('CreateGroup',{'GroupName':lower},code='EntityAlreadyExists');get_group(lower,group)
        if mode=='create':groups([group])
        add('DeleteGroup',{'GroupName':lower});add('DeleteGroup' if mode=='case' else 'GetGroup',args,code='NoSuchEntity')
        if mode=='create':groups([])
        add('GetGroup',args,code='NoSuchEntity');return plan
    user=PREFIX+'U1';person=entity('User',user,uid='fresh-user-id');new=PREFIX+'NG1';changed=entity('Group',new,PATH+'new/');userarg={'UserName':user}
    add('CreateUser',{**userarg,'Path':PATH},{'User':person});add('AddUserToGroup',{**args,**userarg});add('ListGroupsForUser',userarg,{'Groups':[group]});add('UpdateGroup',{**args,'NewPath':PATH+'new/','NewGroupName':new});get_group(new,changed,[person]);add('ListGroupsForUser',userarg,{'Groups':[changed]})
    get_group(new,changed);add('GetUser',userarg,{'User':person});add('RemoveUserFromGroup',{'GroupName':new,**userarg});add('GetUser',userarg,{'User':person});add('DeleteUser',userarg);get_group(new,changed);add('DeleteGroup',{'GroupName':new});return plan


def consumer_plan(kind='User',managed=False,mode='allows'):
    plan=[];user=PREFIX+('MyUser' if mode!='allows' else 'User' if kind=='Group' else 'name');person=entity('User',user);userarg={'UserName':user};key={'UserName':user,'AccessKeyId':'AKIAREALSOURCEKEY','SecretAccessKey':'real-source-dynamic-secret','Status':'Active','CreateDate':'2025-01-01T00:00:00Z'};identities={}
    def add(op,values=None,data=None,code=None,credential=None):
        if credential:identities[len(plan)]=credential
        plan.append((op,values or {},data,code))
    def get_user():add('GetUser',userarg,{'User':person})
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    fields=('Users','Groups','UsersQuota','GroupsQuota','AccessKeysPerUserQuota');summary_xml='<SummaryMap>'+''.join('<entry><key>'+field+'</key><value>7</value></entry>' for field in fields)+'</SummaryMap>'
    if mode=='summary_root':add('GetAccountSummary',data={'__raw_xml__':summary_xml});return plan,identities
    add('CreateUser',{**userarg,'Path':PATH},{'User':person})
    if mode=='summary':add('PutUserPolicy',{**userarg,'PolicyName':'AllowIamAction','PolicyDocument':json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:GetAccountSummary','Resource':'*'}]})})
    add('CreateAccessKey',userarg,{'AccessKey':key})
    if mode=='permission':
        add('ListRoles',{'PathPrefix':PATH},code='AccessDenied',credential=key['AccessKeyId']);add('AttachUserPolicy',{**userarg,'PolicyArn':'arn:aws:iam::aws:policy/IAMReadOnlyAccess'});add('ListRoles',{'PathPrefix':PATH},{'Roles':[],'IsTruncated':False},credential=key['AccessKeyId'])
    elif mode=='summary':add('GetAccountSummary',data={'__raw_xml__':summary_xml},credential=key['AccessKeyId'])
    else:
        name=PREFIX+'Group' if kind=='Group' else user;group=entity('Group',name,uid='fresh-group-id')
        if kind=='Group':add('CreateGroup',{'GroupName':name,'Path':PATH},{'Group':group});add('AddUserToGroup',{'GroupName':name,**userarg})
        if managed:add('AttachGroupPolicy',{'GroupName':name,'PolicyArn':READ})
        else:add('Put'+kind+'Policy',{kind+'Name':name,'PolicyName':'AllowStar','PolicyDocument':json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]})})
    if mode in ('summary','permission'):
        get_user();add('DeleteUserPolicy' if mode=='summary' else 'DetachUserPolicy',{**userarg,**({'PolicyName':'AllowIamAction'} if mode=='summary' else {'PolicyArn':'arn:aws:iam::aws:policy/IAMReadOnlyAccess'})})
    elif kind=='Group':
        add('GetGroup',{'GroupName':name},{'Group':group});add('DetachGroupPolicy' if managed else 'DeleteGroupPolicy',{'GroupName':name,**({'PolicyArn':READ} if managed else {'PolicyName':'AllowStar'})});add('GetGroup',{'GroupName':name},{'Group':group});get_user();add('RemoveUserFromGroup',{'GroupName':name,**userarg})
    else:get_user();add('DeleteUserPolicy',{**userarg,'PolicyName':'AllowStar'})
    get_user();add('DeleteAccessKey',{**userarg,'AccessKeyId':key['AccessKeyId']})
    if mode=='allows' and kind=='Group':add('GetGroup',{'GroupName':name},{'Group':group});add('DeleteGroup',{'GroupName':name})
    get_user();add('DeleteUser',userarg);return plan,identities


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM SDK Query contracts')
class IamManagementSdkTest(unittest.TestCase):
    """Use real generated service clients and independent scripts to check original JSON and management boundaries."""
    def test_six_inline_and_managed_user_group_role_sources_keep_documents_and_errors(self):
        """Run six original policy sources through both SDKs; expect URL-decoded policy insertion order, fixed AWS managed ARNs, duplicate attachment behavior, exact policy-name ordering and scoped dependency cleanup."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for kind in ('User','Group','Role'):
                for managed in (False,True):
                    plan=policy_plan(kind,managed,kind=='Role' and not managed)
                    with self.subTest(language=language,kind=kind,managed=managed),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                        d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                        with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                            if managed:managed_policy(p,kind)
                            elif kind=='Role':inline_role(p)
                            else:inline_policy(p,kind)
                        verify_requests(self,server,plan)

    def test_five_oidc_sources_keep_original_http_and_nonhex_thumbprint_inputs(self):
        """Run every original OIDC management source unchanged; expect actual http URL and EXAMPLE/SAMPLE1 query values, ordered client IDs, real SDK list membership and finally absence checks."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('basic','add_new','add_existing','remove','thumbprint'):
                plan,clients,old,new=oidc_plan(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):provider(p,mode)
                    verify_requests(self,server,plan)
                    from urllib.parse import parse_qs,urlsplit
                    queries=[parse_qs(body.decode() if method=='POST' else urlsplit(path).query) for method,path,headers,body in server.requests]
                    create=next(q for q in queries if q['Action']==['CreateOpenIDConnectProvider'])
                    self.assertEqual(create['ThumbprintList.member.1'],[old])
                    for index,value in enumerate(clients,1):self.assertEqual(create['ClientIDList.member.'+str(index)],[value])
                    if mode=='thumbprint':self.assertEqual(next(q for q in queries if q['Action']==['UpdateOpenIDConnectProviderThumbprint'])['ThumbprintList.member.1'],[new])

    def test_four_group_sources_keep_case_order_membership_and_rename(self):
        """Execute complete group create/case/list/update sources on both native clients; expect original mixed-case ordering, actual member identities after rename and UID-bound membership removal before resource deletion."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode,execute in (('create',group_create),('case',group_case),('list',lambda p:listing(p,'Group')),('update',group_update)):
                plan=group_plan(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):execute(p)
                    verify_requests(self,server,plan)

    def test_six_real_generated_key_consumers_use_actual_s3_or_iam_sessions(self):
        """Run user/group inline/managed allow3, ListRoles permission1 and summary2 sources; expect generated credentials in actual subsequent native signatures, original AccessDenied→success sequence, actual map decoding and owned dependencies cleanup."""
        variants=(('User',False,'allows'),('Group',False,'allows'),('Group',True,'allows'),('User',False,'permission'),('User',False,'summary'),('User',False,'summary_root'))
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for kind,managed,mode in variants:
                plan,identities=consumer_plan(kind,managed,mode)
                with self.subTest(language=language,kind=kind,managed=managed,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    if mode=='allows':server.s3.fixtures.extend([(403,{},b'<Error><Code>AccessDenied</Code><Message>Original missing identity policy</Message></Error>'),(200,{},b'<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Buckets/></ListAllMyBucketsResult>')])
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                        if mode=='allows':allows(p,kind,managed)
                        elif mode=='permission':permission(p)
                        else:summary(p,mode=='summary')
                    verify_requests(self,server,plan,identities)
                    if mode=='allows':
                        self.assertFalse(server.s3.fixtures);self.assertEqual(len(server.s3.requests),2)
                        for method,path,headers,body in server.s3.requests:self.assertIn('Credential=AKIAREALSOURCEKEY/',{k.lower():v for k,v in headers.items()}['authorization'])

    def test_twenty_seven_default_sources_are_not_run_without_required_control_plane_configuration(self):
        """Run all actual management/root-key/STS caller entries with no control-plane transport or identities; expect 27 precise not_run rows and no HTTP, never fake cloud passes."""
        from osv.ceph_iam_policy_entities import CASES as a
        from osv.ceph_iam_oidc import CASES as b
        from osv.ceph_iam_role_policies import CASES as c
        from osv.ceph_iam_root_keys import CASES as d
        from osv.ceph_sts_callers import CASES as e
        from sdk_fixtures import fixture_worker
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3',{'OSV_IAM_ENDPOINT':'','OSV_IAM_REGION':'','OSV_STS_ENDPOINT':'','OSV_STS_REGION':'','OSV_IAM_ACCESS_KEY':'','OSV_IAM_SECRET_KEY':'','OSV_IAM_ROOT_ACCESS_KEY':'','OSV_IAM_ROOT_SECRET_KEY':''}) as (worker,server):
                directory=Path(temporary);rows=run_cases(Context(worker,'unused/',directory,b'',directory/'body',directory/'out',{}),a+b+c+d+e);self.assertEqual(len(rows),27);self.assertEqual({row['status'] for row in rows},{'not_run'});self.assertEqual(server.requests,[])
