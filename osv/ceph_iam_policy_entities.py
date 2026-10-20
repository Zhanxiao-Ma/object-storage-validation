"""Original fresh-account groups and identity-policy vectors, including native fresh-key S3 consumers."""
import json
from .ceph_iam_scope import error,pages
from .ceph_iam_policy_scope import PolicyScope,credentials,retry
from .ceph_iam_accounts import listing,TRUST
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

SOURCE=CEPH.replace('test_s3.py','test_iam.py')
FULL='arn:aws:iam::aws:policy/AmazonS3FullAccess'
READ='arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess'


def group_names(s):return [x['GroupName'] for x in pages(s.p,'ListGroups','Groups',PathPrefix=s.path)]


def group_create(p):
    with PolicyScope(p) as s:
        name=s.name('G1');assert_equal(group_names(s),[]);item=s.create('Group',name,s.path)
        assert_equal(item['Path'],s.path);assert_equal(item['GroupName'],name);assert_equal(bool(item['GroupId']),True);assert_equal(item['Arn'].startswith('arn:aws:iam:'),True);assert_equal(item['Arn'].endswith(':group'+s.path+name),True)
        error(s.raw('CreateGroup',GroupName=name),'EntityAlreadyExists');assert_equal(s.call('GetGroup',GroupName=name)['Group'],item);assert_equal(group_names(s),[name]);s.call('DeleteGroup',GroupName=name);error(s.raw('GetGroup',GroupName=name),'NoSuchEntity');assert_equal(group_names(s),[])


def group_case(p):
    with PolicyScope(p) as s:
        upper=s.name('G1');lower=s.name('g1');item=s.create('Group',upper,s.path)
        error(s.raw('CreateGroup',GroupName=lower),'EntityAlreadyExists');assert_equal(s.call('GetGroup',GroupName=lower)['Group'],item);s.call('DeleteGroup',GroupName=lower);error(s.raw('DeleteGroup',GroupName=upper),'NoSuchEntity')


def group_update(p):
    with PolicyScope(p) as s:
        name=s.name('G1');uid=s.create('Group',name,s.path)['GroupId'];user=s.name('U1');s.create('User',user,s.path);s.call('AddUserToGroup',GroupName=name,UserName=user)
        def groups(path,name):
            rows=s.call('ListGroupsForUser',UserName=user)['Groups'];assert_equal(len(rows),1)
            for field,value in (('Path',path),('GroupName',name),('GroupId',uid)):assert_equal(rows[0][field],value)
        groups(s.path,name);new=s.name('NG1');path=s.path+'new/';s.call('UpdateGroup',GroupName=name,NewPath=path,NewGroupName=new)
        actual=s.call('GetGroup',GroupName=new);item=actual['Group']
        for field,value in (('Path',path),('GroupName',new),('GroupId',uid)):assert_equal(item[field],value)
        assert_equal(item['Arn'].startswith('arn:aws:iam:'),True);assert_equal(item['Arn'].endswith(':group'+path+new),True);assert_equal(len(actual['Users']),1);assert_equal(actual['Users'][0]['UserName'],user);groups(path,new)


def inline_policy(p,kind):
    with PolicyScope(p) as s:
        name=s.name('name');values={kind+'Name':name,'PolicyName':'List'};bucket=s.name('bucket')
        docs=[json.dumps({'Version':'2012-10-17','Statement':[{'Effect':effect,'Action':'s3:ListBucket','Resource':'arn:aws:s3:::'+bucket}]}) for effect in ('Deny','Allow')]
        for operation in ('Get','Delete','Put'):error(s.raw(operation+kind+'Policy',**values,**({'PolicyDocument':docs[0]} if operation=='Put' else {})),'NoSuchEntity')
        s.create(kind,name,s.path)
        for operation in ('Get','Delete'):error(s.raw(operation+kind+'Policy',**values),'NoSuchEntity')
        for document in docs:
            s.call('Put'+kind+'Policy',**values,PolicyDocument=document);assert_equal(json.dumps(s.call('Get'+kind+'Policy',**values)['PolicyDocument']),document);assert_equal(s.call('List'+kind+'Policies',**{kind+'Name':name})['PolicyNames'],['List'])
        if kind=='Group':error(s.raw('DeleteGroup',GroupName=name),'DeleteConflict')
        s.call('Delete'+kind+'Policy',**values)
        for operation in ('Get','Delete'):error(s.raw(operation+kind+'Policy',**values),'NoSuchEntity')
        assert_equal(s.call('List'+kind+'Policies',**{kind+'Name':name})['PolicyNames'],[])


def managed_policy(p,kind):
    with PolicyScope(p) as s:
        name=s.name('name');namearg={kind+'Name':name}
        for operation in ('Attach','Detach','ListAttached'):error(s.raw(operation+kind+('Policies' if operation=='ListAttached' else 'Policy'),**namearg,**({} if operation=='ListAttached' else {'PolicyArn':FULL})),'NoSuchEntity')
        s.create(kind,name,s.path,**({'AssumeRolePolicyDocument':TRUST} if kind=='Role' else {}));error(s.raw('Detach'+kind+'Policy',**namearg,PolicyArn=FULL),'NoSuchEntity')
        for _ in range(2):s.call('Attach'+kind+'Policy',**namearg,PolicyArn=FULL)
        def one():
            rows=s.call('ListAttached'+kind+'Policies',**namearg)['AttachedPolicies'];assert_equal(len(rows),1);assert_equal(rows[0]['PolicyName'],'AmazonS3FullAccess');assert_equal(rows[0]['PolicyArn'],FULL)
        one();s.call('Attach'+kind+'Policy',**namearg,PolicyArn=READ);rows=s.call('ListAttached'+kind+'Policies',**namearg)['AttachedPolicies'];assert_equal(len(rows),2)
        for value in ('AmazonS3FullAccess','AmazonS3ReadOnlyAccess'):assert_equal(value in [row['PolicyName'] for row in rows],True)
        for value in (FULL,READ):assert_equal(value in [row['PolicyArn'] for row in rows],True)
        s.call('Detach'+kind+'Policy',**namearg,PolicyArn=READ);error(s.raw('Detach'+kind+'Policy',**namearg,PolicyArn=READ),'NoSuchEntity');one();error(s.raw('Delete'+kind,**namearg),'DeleteConflict')


def allows(p,kind,managed=False):
    with PolicyScope(p) as s:
        user=s.name('User' if kind=='Group' else 'name');s.create('User',user,s.path);key=s.call('CreateAccessKey',UserName=user)['AccessKey']
        if kind=='Group':
            name=s.name('Group');s.create('Group',name,s.path);s.call('AddUserToGroup',GroupName=name,UserName=user)
        else:name=user
        with credentials(p,key) as client:
            error(retry(client,'api.ListBuckets','InvalidAccessKeyId',input={}),status=403,code='AccessDenied')
            if managed:s.call('AttachGroupPolicy',GroupName=name,PolicyArn=READ)
            else:
                policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]});s.call('Put'+kind+'Policy',**{kind+'Name':name},PolicyName='AllowStar',PolicyDocument=policy)
            from .ceph_iam_scope import checked
            checked(retry(client,'api.ListBuckets','AccessDenied',input={}))


SPECS=(('test_account_group_create',group_create),('test_account_group_case_insensitive_name',group_case),('test_account_group_list',lambda p:listing(p,'Group')),('test_account_group_update',group_update),('test_account_inline_group_policy',lambda p:inline_policy(p,'Group')),('test_account_managed_group_policy',lambda p:managed_policy(p,'Group')),('test_account_inline_group_policy_allow',lambda p:allows(p,'Group')),('test_account_managed_group_policy_allow',lambda p:allows(p,'Group',True)),('test_account_user_policy',lambda p:inline_policy(p,'User')),('test_account_user_policy_managed',lambda p:managed_policy(p,'User')),('test_account_user_policy_allow',lambda p:allows(p,'User')))
OPS='identity_iam_root api.ListBuckets '+' '.join('api.iam.'+x for x in ('GetUser','CreateUser','DeleteUser','CreateAccessKey','DeleteAccessKey','GetGroup','CreateGroup','DeleteGroup','ListGroups','UpdateGroup','AddUserToGroup','RemoveUserFromGroup','ListGroupsForUser','PutUserPolicy','GetUserPolicy','DeleteUserPolicy','ListUserPolicies','AttachUserPolicy','DetachUserPolicy','ListAttachedUserPolicies','PutGroupPolicy','GetGroupPolicy','DeleteGroupPolicy','ListGroupPolicies','AttachGroupPolicy','DetachGroupPolicy','ListAttachedGroupPolicies'))
CASES=tuple(case('ceph_iam_'+reference.removeprefix('test_'),'Ceph IAM用户组与策略原始向量','保留真实分页、全部策略文档/固定托管ARN、原错误码、真实十次退避及生成密钥到实际S3消费者；按immutable ID先清理真实依赖再删资源',OPS,execute,reference,SOURCE) for reference,execute in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_policy_entities',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
