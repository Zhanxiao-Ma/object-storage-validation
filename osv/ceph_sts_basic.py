"""Pinned legacy STS session/role vectors, including the original genuine fifteen-minute expiry wait."""
from contextlib import ExitStack
import json
import time
import uuid
from .ceph_iam_scope import authorized,checked,error
from .ceph_iam_inline import dedicated
from .ceph_iam_policy_scope import configured,credentials
from .ceph_sts_legacy_scope import LegacyRole
from .ceph_sts_callers import sts,transport
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun


def encoded(document):return json.dumps(document,separators=(',',':'))


def deferred(owner,stack,creator=None):
    bucket=BucketScope(owner);result=bucket.raw('bucket_head')
    if result.get('ok') or (result.get('error_kind'),result.get('status_code'))!=('not_found',404):raise RuntimeError('Cannot establish generated STS test bucket absence')
    if creator is not None:bucket.probe=creator
    stack.push(bucket);return bucket


def create(client,bucket):
    result=client.ctx.worker.call({'op':'bucket_create_raw','bucket':bucket.name})
    if result.get('ok'):bucket.created=True
    return result


class ExactBucketScope(BucketScope):
    """Preserve explicit source CreateBucket(Bucket=...) without adding regional configuration fields."""
    def __enter__(self):
        result=self.raw('bucket_head')
        if result.get('ok') or (result.get('error_kind'),result.get('status_code'))!=('not_found',404):raise RuntimeError('Cannot establish original raw-create bucket absence')
        checked(self.raw('bucket_create_raw'));self.created=True;return self


def session_token(p):
    authorized(p);transport(p);name=p.ctx.workload.get('iam_alt_user_name')
    if not isinstance(name,str) or not name:raise CaseNotRun('Source configured alternate IAM user is absent','workload.iam_alt_user_name')
    policy_name='osvpolicy'+uuid.uuid4().hex
    document='{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"s3:*","Resource":["*"],"Condition":{"BoolIfExists":{"sts:authentication":"false"}}},{"Effect":"Allow","Action":"sts:GetSessionToken","Resource":"*","Condition":{"BoolIfExists":{"sts:authentication":"false"}}}]}'
    with dedicated(p,(policy_name,)) as policy,configured(p,'alt',True) as alt,ExitStack() as stack:
        policy('PutUserPolicy',policy_name,document=document,status=200,source_helper=True);result=sts(alt,'GetSessionToken');checked(result);assert_equal(result['status_code'],200);temporary=stack.enter_context(credentials(p,result['data']['Credentials']));owner=stack.enter_context(configured(p,'iam'));bucket=deferred(owner,stack,temporary)
        result=create(temporary,bucket);checked(result);assert_equal(result['status_code'],200);checked(temporary.ctx.worker.call({'op':'bucket_delete','bucket':bucket.name}))
        # The original never asserts the delete status in this function.
        policy('DeleteUserPolicy',policy_name)


def assume(p,mode):
    authorized(p);transport(p);name=p.ctx.workload.get('iam_alt_user_name')
    if not isinstance(name,str) or not name:raise CaseNotRun('Source configured alternate IAM user is absent','workload.iam_alt_user_name')
    with ExitStack() as stack:
        bucket=stack.enter_context(ExactBucketScope(p)) if mode in ('deny_head','allow_head') else None
        role=stack.enter_context(LegacyRole(p));trust=encoded({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':['arn:aws:iam:::user/'+name]},'Action':['sts:AssumeRole']}]});created=role.create(trust)
        if mode in ('deny_head','allow_head'):statement={'Effect':'Allow','Action':'s3:GetObject' if mode=='deny_head' else ['s3:GetObject','s3:ListBucket'],'Principal':'*','Resource':'arn:aws:s3:::*'}
        else:statement={'Effect':'Deny' if mode=='deny' else 'Allow','Action':'s3:*','Resource':'arn:aws:s3:::*'}
        response=role.put(encoded({'Version':'2012-10-17','Statement':statement}));assert_equal(response['status_code'],200)
        alt=stack.enter_context(configured(p,'alt',True));response=sts(alt,'AssumeRole',RoleArn=created['Arn'],RoleSessionName=role.session,**({'DurationSeconds':900} if mode=='expiry' else {}));checked(response);assert_equal(response['status_code'],200)
        if mode=='expiry':time.sleep(900)
        temporary=stack.enter_context(credentials(p,response['data']['Credentials']))
        if mode in ('deny_head','allow_head'):
            result=temporary.ctx.worker.call({'op':'head_http','bucket':bucket.name,'key':'nonexistent'});status=200 if result.get('ok') else result.get('status_code');assert_equal(status,403 if mode=='deny_head' else 404)
        else:
            owner=stack.enter_context(configured(p,'iam'));bucket=deferred(owner,stack,temporary);result=create(temporary,bucket)
            if mode=='allow':checked(result);assert_equal(result['status_code'],200);result=temporary.ctx.worker.call({'op':'bucket_delete','bucket':bucket.name});checked(result);assert_equal(result['status_code'],204)
            elif mode=='deny':assert_equal(result.get('error_code') if not result.get('ok') else None,'AccessDenied')
            else:
                # Upstream leaves both locals unbound on unexpected success;
                # retain that failure rather than making successful creation pass.
                if not result.get('ok'):s3bucket_error=result.get('error_code');http_status_code=result.get('status_code')
                assert_equal(s3bucket_error,'ExpiredToken');assert_equal(http_status_code,400)


SPECS=(('test_get_session_token',session_token),('test_assume_role_allow',lambda p:assume(p,'allow')),('test_assume_role_deny',lambda p:assume(p,'deny')),('test_assume_role_creds_expiry',lambda p:assume(p,'expiry')),('test_assume_role_deny_head_nonexistent',lambda p:assume(p,'deny_head')),('test_assume_role_allow_head_nonexistent',lambda p:assume(p,'allow_head')))
OPS='identity_iam identity_alt api.iam.CreateRole api.iam.GetRole api.iam.DeleteRole api.iam.PutRolePolicy api.iam.DeleteRolePolicy api.iam.GetUser api.iam.GetUserPolicy api.iam.ListUserPolicies api.iam.PutUserPolicy api.iam.DeleteUserPolicy api.sts.GetSessionToken api.sts.AssumeRole bucket_create_raw head_http '+SCOPE_OPERATIONS
CASES=tuple(case('ceph_sts_'+ref.removeprefix('test_'),'Ceph原始STS会话/角色授权','保留legacy空account角色ARN、Path=/和Tags=[]、全部原策略字串/Principal及错误预期、真实alt调用STS/临时凭据调用S3；expiry原DurationSeconds900且真实等900秒，不缩短时序；保留上游helper KeyError与成功分支未绑定局部变量缺陷',OPS,execute,ref,CEPH.replace('test_s3.py','test_sts.py')) for ref,execute in SPECS)


def register(registry):registry.add_suite(Suite('ceph_sts_basic',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
