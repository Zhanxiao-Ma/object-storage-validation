"""Pinned tenant inline policies, preserving fixed vectors on an explicitly dedicated existing user."""
from contextlib import contextmanager
import json
from .ceph_iam_scope import authorized,rpc,checked,error,pages
from .scenarios import Suite,assert_equal
from .suites import CaseNotRun,case,run_cases,CEPH

IAM_SOURCE=CEPH.replace("test_s3.py","test_iam.py")

ALLOW={'Version':'2012-10-17','Statement':{'Effect':'Allow','Action':'*','Resource':'*'}}
BAD_USER='some-non-existing-user-id'
NAMES=('AllAccessPolicy','AllAccessPolicy'*10,'AllowAccessPolicy','AllowAccessPolicy1','AllowAccessPolicy2','AllowAccessPolicy3','non-existing-policy-name')


@contextmanager
def dedicated(p,names=NAMES):
    authorized(p);name=p.ctx.workload.get('iam_alt_user_name')
    if 'identity_iam' not in p.ctx.worker.capabilities:raise CaseNotRun('Tenant IAM requires its explicit IAM endpoint and credentials','identity_iam')
    if not isinstance(name,str) or not name:raise CaseNotRun('Inline source requires an explicitly dedicated existing alt IAM user','workload.iam_alt_user_name')
    owner=checked(rpc(p,'GetUser','iam',UserName=name))['User'];uid=owner['UserId']
    active=pages(p,'ListUserPolicies','PolicyNames','iam',UserName=name)
    if any(item in active for item in names):raise CaseNotRun('A source-fixed policy already exists; refusing to overwrite it','dedicated IAM user with absent source policy names')
    for policy in names:
        result=rpc(p,'GetUserPolicy','iam',UserName=name,PolicyName=policy)
        if result.get('ok'):raise CaseNotRun('A source-fixed policy already exists; refusing to overwrite it','dedicated IAM user with absent source policy names')
        # The deliberately overlong PolicyName may be rejected by validation;
        # only list absence can prove its absence in that specific case.
        if policy=='AllAccessPolicy'*10 and result.get('status_code')==400:continue
        error(result,'NoSuchEntity')
    absent=rpc(p,'GetUser','iam',UserName=BAD_USER)
    if absent.get('ok'):raise CaseNotRun('Source fixed invalid user exists; refusing to modify it','isolated IAM account without '+BAD_USER)
    error(absent,'NoSuchEntity');attempted=set()
    def call(operation,policy=None,user=None,document=None,status=None,negative=None,source_helper=False):
        values={'UserName':name if user is None else user}
        if policy is not None:values['PolicyName']=policy
        if document is not None:values['PolicyDocument']=document if isinstance(document,str) else json.dumps(document)
        if operation=='PutUserPolicy':attempted.add((values['UserName'],policy))
        result=rpc(p,operation,'iam',**values)
        if source_helper and not result.get('ok') and result.get('status_code') is not None:raise KeyError('Code')
        if negative is not None:error(result,status=negative)
        else:
            checked(result)
            if status is not None:assert_equal(result.get('status_code'),status)
        return result
    def cleanup():
        # Never delete policy on a replacement user or a newly appeared invalid
        # user. Only the explicitly authorized user's original immutable ID owns
        # this case's absent-before-write policy namespace.
        current=rpc(p,'GetUser','iam',UserName=name)
        if not current.get('ok'):
            error(current,'NoSuchEntity')
        elif checked(current)['User']['UserId']!=uid:raise RuntimeError('Dedicated IAM user ownership changed; refusing cleanup')
        else:
            for user,policy in sorted(attempted):
                if user!=name:continue
                result=rpc(p,'DeleteUserPolicy','iam',UserName=user,PolicyName=policy)
                if not result.get('ok'):
                    # The oversized input cannot be accepted by the typed API;
                    # confirm list absence before accepting validation on cleanup.
                    if policy=='AllAccessPolicy'*10 and result.get('status_code')==400:
                        assert_equal(policy in pages(p,'ListUserPolicies','PolicyNames','iam',UserName=name),False)
                    else:error(result,'NoSuchEntity')
    call.cleanup=cleanup
    try:yield call
    finally:cleanup()


def inline(p,mode):
    with dedicated(p) as c:
        def put(name='AllAccessPolicy',status=200,document=ALLOW):return c('PutUserPolicy',name,document=document,status=status)
        def delete(name='AllAccessPolicy',status=None):return c('DeleteUserPolicy',name,status=status)
        if mode=='put_user_policy':put();delete(status=200)
        elif mode=='put_user_policy_invalid_user':c('PutUserPolicy','AllAccessPolicy',user=BAD_USER,document=ALLOW,negative=404)
        elif mode=='put_user_policy_parameter_limit':c('PutUserPolicy','AllAccessPolicy'*10,document={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]*1000},negative=400)
        elif mode=='put_user_policy_invalid_element':
            statement={'Effect':'Allow','Action':'*','Resource':'*'}
            for document in ({'Version':'2010-10-17','Statement':[statement]},{'Version':'2012-10-17'},{'Version':'2012-10-17','Statement':[{'Sid':'98AB54CF',**statement},{'Sid':'98AB54CF',**statement}]},{'Version':'2012-10-17','Statement':[{**statement,'Principal':'arn:aws:iam:::username'}]}):c('PutUserPolicy','AllAccessPolicy',document=document,negative=400)
        elif mode=='put_existing_user_policy':put();put(status=None);delete()
        elif mode=='list_user_policy':put();c('ListUserPolicies',status=200);delete()
        elif mode=='list_user_policy_invalid_user':c('ListUserPolicies',user=BAD_USER,negative=404)
        elif mode=='get_user_policy':put();c('GetUserPolicy','AllAccessPolicy',status=200);delete(status=200)
        elif mode=='get_user_policy_invalid_user':put();c('GetUserPolicy','AllAccessPolicy',user=BAD_USER,negative=404);delete()
        elif mode=='get_user_policy_invalid_policy_name':put(status=None);c('GetUserPolicy','non-existing-policy-name',negative=404);delete()
        elif mode=='get_deleted_user_policy':put(status=None);delete();c('GetUserPolicy','AllAccessPolicy',negative=404)
        elif mode=='get_user_policy_from_multiple_policies':
            for name in ('AllowAccessPolicy1','AllowAccessPolicy2'):put(name)
            c('GetUserPolicy','AllowAccessPolicy2',status=200)
            for name in ('AllowAccessPolicy1','AllowAccessPolicy2'):delete(name,200)
        elif mode=='delete_user_policy':put('AllowAccessPolicy');delete('AllowAccessPolicy',200)
        elif mode=='delete_user_policy_invalid_user':put('AllowAccessPolicy');c('DeleteUserPolicy','AllAccessPolicy',user=BAD_USER,negative=404);delete('AllowAccessPolicy',200)
        elif mode=='delete_user_policy_invalid_policy_name':put('AllowAccessPolicy');c('DeleteUserPolicy','non-existing-policy-name',negative=404);delete('AllowAccessPolicy',200)
        elif mode=='delete_user_policy_from_multiple_policies':
            for name in ('AllowAccessPolicy1','AllowAccessPolicy2','AllowAccessPolicy3'):put(name)
            for name in ('AllowAccessPolicy1','AllowAccessPolicy2'):delete(name,200)
            c('GetUserPolicy','AllowAccessPolicy3',status=200);delete('AllowAccessPolicy3',200)
        else:raise ValueError('unknown inline vector')


MODES=('put_user_policy','put_user_policy_invalid_user','put_user_policy_parameter_limit','put_user_policy_invalid_element','put_existing_user_policy','list_user_policy','list_user_policy_invalid_user','get_user_policy','get_user_policy_invalid_user','get_user_policy_invalid_policy_name','get_deleted_user_policy','get_user_policy_from_multiple_policies','delete_user_policy','delete_user_policy_invalid_user','delete_user_policy_invalid_policy_name','delete_user_policy_from_multiple_policies')
OPS='identity_iam '+' '.join('api.iam.'+x for x in ('GetUser','PutUserPolicy','GetUserPolicy','ListUserPolicies','DeleteUserPolicy'))
CASES=tuple(case('ceph_iam_'+mode,'Ceph IAM租户内联策略','保留源固定名字、全部原始PolicyDocument和HTTP断言；仅专用alt用户namespace且先证明固定policy不存在，finally按实际UserId清理',OPS,lambda p,m=mode:inline(p,m),'test_'+mode,IAM_SOURCE) for mode in MODES)


def register(registry):
    registry.add_suite(Suite('ceph_iam_inline',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
