"""Pinned cross-account policy/ACL order matrices and same-account resource-only grants."""
import json
from .ceph_iam_scope import checked,error
from .ceph_iam_policy_scope import PolicyScope,configured,credentials,retry
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun


def field(p,name):
    value=p.ctx.workload.get(name)
    if not isinstance(value,str) or not value:raise CaseNotRun('Source requires independently configured alternate-root metadata','workload.'+name)
    return value


def cross(p,resource='policy',principal='user_arn',first='bucket',root=False,same_account=False):
    metadata=field(p,'iam_alt_root_account_id') if principal=='account_id' else field(p,'iam_alt_root_user_id') if principal=='canonical_id' else field(p,'iam_alt_root_email') if principal=='account_email' else None
    with PolicyScope(p) as main:
        def run(alt):
            name=main.name('name' if same_account else 'AltUser');user=None
            if not root:
                user=alt.create('User',name,main.path)
                if same_account:assert_equal(user['Arn'].startswith('arn:aws:iam:'),True);assert_equal(user['Arn'].endswith(':user'+main.path+name),True)
            if resource=='policy':
                if principal=='user_arn':grantee=user['Arn']
                elif principal=='account_arn':grantee=alt.call('GetUser')['User']['Arn'] if root else user['Arn'].replace(':user'+main.path+name,':root')
                else:grantee=metadata
            else:grantee=('emailAddress=' if principal=='account_email' else 'id=')+(user['UserId'] if principal=='user_id' else metadata)
            def allow_user():
                alt.call('PutUserPolicy',UserName=name,PolicyName='AllowStar',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]}))
            if not root and not same_account and first=='user':allow_user()
            key=None if root else alt.call('CreateAccessKey',UserName=name)['AccessKey']
            with configured(p,'iam_root') as owner,(configured(p,'iam_alt_root') if root else credentials(p,key)) as consumer,BucketScope(owner) as bucket:
                policy_attempted=False
                def grant():
                    nonlocal policy_attempted
                    if resource=='policy':
                        policy_attempted=True;document={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':grantee},'Action':'s3:ListBucket','Resource':'arn:aws:s3:::'+bucket.name}]};checked(bucket.raw('api.PutBucketPolicy',input={'Policy':json.dumps(document)}))
                    else:checked(bucket.raw('api.PutBucketAcl',input={'GrantRead':grantee}))
                try:
                    if not root and not same_account and first=='bucket':grant()
                    result=consumer.ctx.worker.call({'op':'api.ListObjects','input':{'Bucket':bucket.name}}) if root else retry(consumer,'api.ListObjects','InvalidAccessKeyId',input={'Bucket':bucket.name})
                    error(result,code='AccessDenied',status=403)
                    if root or same_account or first=='user':grant()
                    else:allow_user()
                    checked(retry(consumer,'api.ListObjects','AccessDenied',input={'Bucket':bucket.name}))
                finally:
                    if policy_attempted:
                        result=bucket.raw('api.DeleteBucketPolicy',input={})
                        if not result.get('ok'):error(result,status=404)
        if same_account:run(main)
        else:
            with PolicyScope(p,'iam_alt_root') as alt:run(alt)


SPECS=[]
for principal in ('user_arn','account_arn','account_id'):
    for first in ('bucket','user'):
        reference='test_cross_account_'+('bucket_user' if first=='bucket' else 'user_bucket')+'_policy_allow_'+principal;SPECS.append((reference,lambda p,k=principal,f=first:cross(p,'policy',k,f),False))
for principal in ('user_id','canonical_id','account_email'):
    for first in ('bucket','user'):
        reference='test_cross_account_'+('bucket_acl_user_policy' if first=='bucket' else 'user_policy_bucket_acl')+'_grant_'+principal;SPECS.append((reference,lambda p,k=principal,f=first:cross(p,'acl',k,f),False))
for principal in ('account_arn','account_id'):SPECS.append(('test_cross_account_root_bucket_policy_allow_'+principal,lambda p,k=principal:cross(p,'policy',k,root=True),False))
for principal in ('canonical_id','account_email'):SPECS.append(('test_cross_account_root_bucket_acl_grant_'+principal,lambda p,k=principal:cross(p,'acl',k,root=True),False))
SPECS.append(('test_account_user_bucket_policy_allow',lambda p:cross(p,same_account=True),True))
BASE='identity_iam_root '+' '.join('api.iam.'+op for op in ('GetUser','CreateUser','DeleteUser','CreateAccessKey','DeleteAccessKey','PutUserPolicy','DeleteUserPolicy'))+' api.ListObjects api.PutBucketPolicy api.DeleteBucketPolicy api.PutBucketAcl '+SCOPE_OPERATIONS
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_').replace('cross_account','crossacct'),'Ceph跨账户资源授权原始矩阵','保留先用户策略/先桶策略或ACL的原执行顺序、原userARN/accountARN/独立accountID/userID/canonicalID/emailAddress原字面grant、精确两阶段403与十次原退避；真实fresh key直入原生S3消费者，严格原root身份清理',BASE+('' if same else ' identity_iam_alt_root'),execute,ref,CEPH.replace('test_s3.py','test_iam.py')) for ref,execute,same in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_cross_resources',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
