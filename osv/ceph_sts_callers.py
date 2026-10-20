"""Original STS caller-identity vectors using fresh IAM and actual chained temporary credentials."""
import json
from .ceph_iam_scope import IamScope,checked
from .ceph_iam_policy_scope import credentials,retry,scoped_retry
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun

SOURCE=CEPH.replace('test_s3.py','test_sts.py')


def sts(p,operation,identity='iam',**values):return p.ctx.worker.call({'op':'api.sts.'+operation,'identity':identity,'input':values})


def transport(p):
    import os
    env={**os.environ,**p.ctx.worker.target.get('env',{})}
    if not env.get('OSV_STS_ENDPOINT') or not env.get('OSV_STS_REGION'):raise CaseNotRun('Source requires explicitly configured STS transport','OSV_STS_ENDPOINT/REGION')


def root(p):
    user_id=p.ctx.workload.get('iam_root_user_id')
    if not isinstance(user_id,str) or not user_id:raise CaseNotRun('Original root caller oracle needs the independently configured IAM root user ID','workload.iam_root_user_id')
    if 'identity_sts_iam_root' not in p.ctx.worker.capabilities:raise CaseNotRun('Actual root STS identity is not configured','identity_sts_iam_root')
    data=checked(sts(p,'GetCallerIdentity','iam_root'));assert_equal(data['UserId'],user_id);assert_equal(data['Account'],user_id);assert_equal(data['Arn'],'arn:aws:iam::'+user_id+':root')


def user(p,assume=False):
    transport(p)
    with IamScope(p) as s:
        name=s.name('MyUser');role_name=s.name('MyRole');created=s.create('User',name,s.path);arn=created['Arn'];account=arn.removeprefix('arn:aws:iam::').removesuffix(':user'+s.path+name);key=s.call('CreateAccessKey',UserName=name)['AccessKey']
        with credentials(p,key,True) as client:
            if assume:
                trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':arn}}]})
                role=checked(scoped_retry(s,'CreateRole','MalformedPolicyDocument',RoleName=role_name,Path=s.path,AssumeRolePolicyDocument=trust))['Role']
                assumed=checked(retry(client,'api.sts.AssumeRole',('InvalidClientTokenId','AccessDenied'),identity='iam',input={'RoleArn':role['Arn'],'RoleSessionName':'MySession'}))
                with credentials(p,assumed['Credentials'],True) as temporary:data=checked(sts(temporary,'GetCallerIdentity'))
                s.call('DeleteRole',RoleName=role_name)
            else:data=checked(sts(client,'GetCallerIdentity'))
            s.call('DeleteAccessKey',UserName=name,AccessKeyId=key['AccessKeyId']);s.call('DeleteUser',UserName=name)
            assert_equal(data['UserId'],role['RoleId']+':MySession' if assume else created['UserId']);assert_equal(data['Account'],account);assert_equal(data['Arn'],assumed['AssumedRoleUser']['Arn'] if assume else arn)


OPS='identity_iam_root '+' '.join('api.iam.'+op for op in ('GetUser','CreateUser','DeleteUser','CreateAccessKey','DeleteAccessKey','CreateRole','GetRole','DeleteRole'))+' api.sts.AssumeRole api.sts.GetCallerIdentity'
CASES=(case('ceph_sts_caller_root','Ceph root STS身份','原独立root user_id对三个字段完整断言；不从被测响应派生oracle','identity_sts_iam_root api.sts.GetCallerIdentity',root,'test_get_caller_identity_root',SOURCE),case('ceph_sts_caller_user','Ceph fresh user STS身份','源CreateUser ARN推导account与真实UserId/Arn对照，真实CreateAccessKey进入新官方STS消费者，源删除后完整断言',OPS,lambda p:user(p),'test_get_caller_identity_non_root_user',SOURCE),case('ceph_sts_caller_assumed_role','Ceph真实两级凭据STS身份','真实fresh key→AssumeRole凭据→新GetCallerIdentity；保留十次重试、原session字符串与RoleId/Account/Arn全部断言，清理actual IDs',OPS,lambda p:user(p,True),'test_get_caller_identity_after_assume_role',SOURCE))


def register(registry):registry.add_suite(Suite('ceph_sts_callers',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
