"""Pinned Ceph account user/key and role CRUD vectors on native IAM SDK operations."""
import json
from .ceph_iam_scope import IamScope,rpc,checked,error,pages
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

IAM_SOURCE=CEPH.replace("test_s3.py","test_iam.py")

TRUST=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':'*'}}]})


def names(s,kind,prefix,page=False):
    values={'PathPrefix':prefix}
    if page:values['MaxItems']=1
    return [row[kind+'Name'] for row in pages(s.p,'List'+kind+'s',kind+'s',**values)]


def create(s,kind,name,path=None,**values):
    if kind=='Role':values['AssumeRolePolicyDocument']=TRUST
    return s.create(kind,name,path,**values)


def creation(p,kind):
    with IamScope(p) as s:
        name=s.name('U1' if kind=='User' else 'R1');extra={} if kind=='User' else {'Description':'my role description','MaxSessionDuration':43200}
        item=create(s,kind,name,s.path,**extra)
        assert_equal(item['Path'],s.path);assert_equal(item[kind+'Name'],name);assert_equal(bool(item[kind+'Id']),True)
        assert_equal(item['Arn'].startswith('arn:aws:iam:'),True);assert_equal(item['Arn'].endswith(':'+kind.lower()+s.path+name),True);assert_equal(item['CreateDate']>0,True)
        if kind=='Role':
            assert_equal(json.dumps(item['AssumeRolePolicyDocument']),TRUST)
            role=s.call('GetRole',RoleName=name)['Role'];assert_equal(role['Arn'],item['Arn']);assert_equal(role['Description'],'my role description');assert_equal(role['MaxSessionDuration'],43200)
        error(s.raw('Create'+kind,**{kind+'Name':name,'Path':s.path+'foo/'},**({} if kind=='User' else {'AssumeRolePolicyDocument':TRUST})),'EntityAlreadyExists')
        other=s.name('U2' if kind=='User' else 'R2');item=create(s,kind,other,s.path+'foo/');assert_equal(item['Path'],s.path+'foo/');assert_equal(item[kind+'Name'],other)


def case_insensitive(p,kind):
    with IamScope(p) as s:
        upper=s.name('U1' if kind=='User' else 'R1');lower=s.name('u1' if kind=='User' else 'r1');item=create(s,kind,upper,s.path)
        error(s.raw('Create'+kind,**{kind+'Name':lower},**({} if kind=='User' else {'AssumeRolePolicyDocument':TRUST})),'EntityAlreadyExists')
        actual=s.call('Get'+kind,**{kind+'Name':lower})[kind]
        assert_equal(actual,item) if kind=='User' else assert_equal(actual['RoleId'],item['RoleId'])
        s.call('Delete'+kind,**{kind+'Name':lower});error(s.raw('Get'+kind,**{kind+'Name':lower}),'NoSuchEntity')


def deletion(p,kind):
    with IamScope(p) as s:
        name=s.name('U1');error(s.raw('Delete'+kind,**{kind+'Name':name}),'NoSuchEntity')
        old=create(s,kind,name,s.path);s.call('Delete'+kind,**{kind+'Name':name});new=create(s,kind,name,s.path)
        assert_equal(old[kind+'Id']!=new[kind+'Id'],True);assert_equal(old['CreateDate']<=new['CreateDate'],True)


def listing(p,kind,path_prefix=False):
    with IamScope(p) as s:
        result=s.call('List'+kind+'s',PathPrefix=s.path);assert_equal(len(result[kind+'s']),0);assert_equal(result['IsTruncated'],False)
        ns=[s.name(x) for x in (('a','b','c','d') if path_prefix else ('aa','Ab','ac','Ad'))]
        if path_prefix:
            for name,suffix in zip(ns,('','','a/','a/x/')):create(s,kind,name,s.path+suffix)
            for suffix,expected in (('',ns),('a',ns[2:]),('a/x',ns[3:])):
                for page in (False,True):assert_equal(names(s,kind,s.path+suffix,page),expected)
            assert_equal(names(s,kind,s.path+'a/x/d'),[])
        else:
            for name,suffix in zip(reversed(ns),('w/','x/','y/','z/')):create(s,kind,name,s.path+suffix)
            for page in (False,True):assert_equal(names(s,kind,s.path,page),ns)


def update_user(p,path_change=False):
    with IamScope(p) as s:
        a,b,z=[s.name(x) for x in ('a','b','z')]
        if not path_change:error(s.raw('UpdateUser',UserName=a,NewUserName=z),'NoSuchEntity')
        create(s,'User',a,s.path);create(s,'User',b,s.path+'m/');assert_equal(names(s,'User',s.path),[a,b])
        old=s.call('GetUser',UserName=a)['User'];assert_equal(old['UserName'],a)
        if path_change:assert_equal(old['Path'],s.path)
        s.call('UpdateUser',UserName=a,**({'NewPath':s.path+'z/'} if path_change else {'NewUserName':z}))
        if not path_change:error(s.raw('GetUser',UserName=a),'NoSuchEntity')
        current=s.call('GetUser',UserName=a if path_change else z)['User'];assert_equal(current['UserName'],a if path_change else z);assert_equal(current['UserId'],old['UserId'])
        if path_change:assert_equal(current['Path'],s.path+'z/')
        assert_equal(current['Arn'].endswith(':user'+s.path+('z/'+a if path_change else z)),True);assert_equal(names(s,'User',s.path),[a,b] if path_change else [b,z])


def update_role(p):
    with IamScope(p) as s:
        a=s.name('a');error(s.raw('UpdateRole',RoleName=a),'NoSuchEntity');create(s,'Role',a,s.path)
        old=s.call('GetRole',RoleName=a)['Role'];assert_equal(old['RoleName'],a)
        s.call('UpdateRole',RoleName=a,Description='my role description',MaxSessionDuration=43200)
        current=s.call('GetRole',RoleName=a)['Role']
        for field in ('RoleId','Arn'):assert_equal(current[field],old[field])
        assert_equal(current['Description'],'my role description');assert_equal(current['MaxSessionDuration'],43200)


def key_ids(s,name,page=False):
    return [row['AccessKeyId'] for row in pages(s.p,'ListAccessKeys','AccessKeyMetadata',UserName=name,**({'MaxItems':1} if page else {}))]


def access_key(p,mode):
    with IamScope(p) as s:
        name=s.name('a');fake='abcdefghijklmnopqrstu'
        if mode=='create':error(s.raw('CreateAccessKey',UserName=name),'NoSuchEntity')
        else:
            # Source literal belongs to a negative vector; prove absence first so
            # this vector can never deactivate/delete another user's real key.
            absent=rpc(p,'GetUser',UserName='nosuchuser')
            if absent.get('ok'):
                from .suites import CaseNotRun
                raise CaseNotRun('Source fixed negative user nosuchuser exists; refusing to mutate it','isolated IAM account without nosuchuser')
            error(absent,'NoSuchEntity')
            op={'update':'UpdateAccessKey','delete':'DeleteAccessKey','list':'ListAccessKeys'}[mode]
            values={'UserName':'nosuchuser'}
            if mode!='list':values['AccessKeyId']=fake
            if mode=='update':values['Status']='Active'
            error(s.raw(op,**values),'NoSuchEntity')
        create(s,'User',name,s.path)
        if mode=='list':
            for page in (False,True):assert_equal(key_ids(s,name,page),[])
            first=s.call('CreateAccessKey',UserName=name)['AccessKey']['AccessKeyId']
            for page in (False,True):assert_equal(key_ids(s,name,page),[first])
            second=s.call('CreateAccessKey',UserName=name)['AccessKey']['AccessKeyId']
            for page in (False,True):assert_equal(sorted(key_ids(s,name,page)),sorted([first,second]))
            return
        if mode in ('update','delete'):
            values={'UserName':name,'AccessKeyId':fake}
            if mode=='update':values['Status']='Active'
            # Update source creates the real key before this second negative.
            if mode=='delete':error(s.raw('DeleteAccessKey',**values),'NoSuchEntity')
        key=s.call('CreateAccessKey',UserName=name)['AccessKey'];keyid=key['AccessKeyId']
        if mode=='create':
            assert_equal(key['UserName'],name);assert_equal(bool(keyid),True);assert_equal(bool(key['SecretAccessKey']),True);assert_equal(key['Status'],'Active');assert_equal(key['CreateDate']>0,True)
        elif mode=='update':
            assert_equal(key['CreateDate']>0,True);error(s.raw('UpdateAccessKey',UserName=name,AccessKeyId=fake,Status='Active'),'NoSuchEntity')
            for status in ('Active','Inactive'):s.call('UpdateAccessKey',UserName=name,AccessKeyId=keyid,Status=status)
            rows=s.call('ListAccessKeys',UserName=name)['AccessKeyMetadata'];assert_equal(len(rows),1);actual=rows[0]
            for field,wanted in (('UserName',name),('AccessKeyId',keyid),('Status','Inactive'),('CreateDate',key['CreateDate'])):assert_equal(actual[field],wanted)
        else:
            s.call('DeleteAccessKey',UserName=name,AccessKeyId=keyid);error(s.raw('DeleteAccessKey',UserName=name,AccessKeyId=keyid),'NoSuchEntity');assert_equal(len(s.call('ListAccessKeys',UserName=name)['AccessKeyMetadata']),0)


SPECS=[]
for kind in ('User','Role'):
    for suffix,fn in (('create',lambda p,k=kind:creation(p,k)),('case_insensitive_name',lambda p,k=kind:case_insensitive(p,k)),('delete',lambda p,k=kind:deletion(p,k)),('list',lambda p,k=kind:listing(p,k)),('list_path_prefix',lambda p,k=kind:listing(p,k,True))):SPECS.append(('test_account_'+kind.lower()+'_'+suffix,fn))
SPECS.extend((('test_account_user_update_name',lambda p:update_user(p)),('test_account_user_update_path',lambda p:update_user(p,True)),('test_account_role_update',update_role)))
for mode in ('create','update','delete','list'):SPECS.append(('test_account_user_access_key_'+mode,lambda p,m=mode:access_key(p,m)))
OPS='identity_iam_root '+' '.join('api.iam.'+x for x in ('GetUser','CreateUser','UpdateUser','DeleteUser','ListUsers','CreateAccessKey','UpdateAccessKey','DeleteAccessKey','ListAccessKeys','CreateRole','GetRole','UpdateRole','DeleteRole','ListRoles'))
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_'),'Ceph IAM账户资源原始向量','保留源输入、大小写、分页顺序、真实日期和全部断言；显式隔离授权、真实root证明及实际ID归属清理',OPS,fn,ref,IAM_SOURCE) for ref,fn in SPECS)


def register(registry):
    registry.add_suite(Suite('ceph_iam_accounts',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
