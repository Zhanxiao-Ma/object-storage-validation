"""Pinned account role-policy and account-summary management assertions."""
import json
from .ceph_iam_scope import checked,error,rpc
from .ceph_iam_policy_scope import PolicyScope,credentials
from .ceph_iam_policy_entities import managed_policy
from .ceph_iam_accounts import TRUST
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

SOURCE=CEPH.replace('test_s3.py','test_iam.py')
ROLE_POLICY=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]})


def inline_role(p):
    with PolicyScope(p) as s:
        name=s.name('r');args={'RoleName':name,'PolicyName':'MyPolicy'}
        for operation in ('Get','Delete','Put'):error(s.raw(operation+'RolePolicy',**args,**({'PolicyDocument':ROLE_POLICY} if operation=='Put' else {})),'NoSuchEntity')
        s.create('Role',name,s.path,AssumeRolePolicyDocument=TRUST)
        for operation in ('Get','Delete'):error(s.raw(operation+'RolePolicy',**args),'NoSuchEntity')
        s.call('PutRolePolicy',**args,PolicyDocument=ROLE_POLICY);data=s.call('GetRolePolicy',**args);assert_equal(data['RoleName'],name);assert_equal(data['PolicyName'],'MyPolicy');assert_equal(json.dumps(data['PolicyDocument']),ROLE_POLICY);assert_equal(s.call('ListRolePolicies',RoleName=name)['PolicyNames'],['MyPolicy'])
        s.call('PutRolePolicy',RoleName=name,PolicyName='AnotherPolicy',PolicyDocument=ROLE_POLICY);assert_equal(s.call('ListRolePolicies',RoleName=name)['PolicyNames'],['AnotherPolicy','MyPolicy'])
        for policy in ('MyPolicy','AnotherPolicy'):s.call('DeleteRolePolicy',RoleName=name,PolicyName=policy)
        for operation in ('Get','Delete'):error(s.raw(operation+'RolePolicy',**args),'NoSuchEntity')


def permission(p):
    with PolicyScope(p) as s:
        user=s.name('MyUser');s.create('User',user,s.path);key=s.call('CreateAccessKey',UserName=user)['AccessKey']
        with credentials(p,key,True) as client:
            error(rpc(client,'ListRoles','iam',PathPrefix=s.path),code='AccessDenied',status=403)
            s.call('AttachUserPolicy',UserName=user,PolicyArn='arn:aws:iam::aws:policy/IAMReadOnlyAccess')
            # Original immediately lists, without adding retries or sort.
            checked(rpc(client,'ListRoles','iam',PathPrefix=s.path))


def summary(p,policy=False):
    with PolicyScope(p) as s:
        if policy:
            user=s.name('MyUser');s.create('User',user,s.path)
            document={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:GetAccountSummary','Resource':'*'}]}
            s.call('PutUserPolicy',UserName=user,PolicyName='AllowIamAction',PolicyDocument=json.dumps(document));key=s.call('CreateAccessKey',UserName=user)['AccessKey']
            with credentials(p,key,True) as client:data=checked(rpc(client,'GetAccountSummary','iam'))['SummaryMap']
        else:data=s.call('GetAccountSummary')['SummaryMap']
        for field in ('Users','Groups','UsersQuota','GroupsQuota','AccessKeysPerUserQuota'):assert_equal(field in data,True)


SPECS=(('test_account_role_policy',inline_role),('test_account_role_policy_managed',lambda p:managed_policy(p,'Role')),('test_account_role_list_permission',permission),('test_get_account_summary_root',summary),('test_get_account_summary_policy',lambda p:summary(p,True)))
OPS='identity_iam_root '+' '.join('api.iam.'+x for x in ('GetUser','CreateUser','DeleteUser','CreateAccessKey','DeleteAccessKey','CreateRole','GetRole','DeleteRole','ListRoles','PutRolePolicy','GetRolePolicy','DeleteRolePolicy','ListRolePolicies','AttachRolePolicy','DetachRolePolicy','ListAttachedRolePolicies','AttachUserPolicy','DetachUserPolicy','PutUserPolicy','DeleteUserPolicy','GetAccountSummary'))
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_'),'Ceph IAM角色策略/账户摘要','保留全部源JSON字符串顺序、PolicyNames字典序、固定托管ARN、首次403及真实新凭据后续IAM会话；所有fresh依赖按immutable ID清理',OPS,execute,ref,SOURCE) for ref,execute in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_role_policies',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
