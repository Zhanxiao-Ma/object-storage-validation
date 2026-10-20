"""Pinned real IAM-user to STS-role to S3/IAM authorization transitions, including cross-account resources."""
from contextlib import ExitStack
import json
from .ceph_iam_scope import checked,error
from .ceph_iam_policy_scope import PolicyScope,credentials,configured,retry,scoped_retry
from .ceph_sts_callers import transport
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun


def create_request(client,bucket):
    response=client.ctx.worker.call({'op':'bucket_create_raw','bucket':bucket.name,'wire_headers':{'x-amz-object-ownership':'ObjectWriter','x-amz-acl':'private'}})
    if response.get('ok'):bucket.created=True
    return response


def create_retry(client,bucket):
    import time
    for index in range(10):
        result=create_request(client,bucket)
        if result.get('ok') or result.get('error_code')!='AccessDenied' or index==9:return result
        time.sleep(index)


def assumed(p,mode):
    transport(p)
    account=p.ctx.workload.get('iam_root_account_id')
    if mode=='create_bucket' and (not isinstance(account,str) or not account):raise CaseNotRun('Source ACL owner oracle requires independently configured root account ID','workload.iam_root_account_id')
    with ExitStack() as stack:
        main=stack.enter_context(PolicyScope(p));alt=main if mode=='list_buckets' else stack.enter_context(PolicyScope(p,'iam_alt_root'))
        name=main.name('MyUser' if mode=='list_buckets' else 'AltUser');role_name=main.name('MyRole');user=alt.create('User',name,main.path)
        key=None
        if mode!='list_buckets':key=alt.call('CreateAccessKey',UserName=name)['AccessKey']
        bucket=None;owner=None
        if mode in ('same_account','cross_account','create_bucket'):
            owner=stack.enter_context(configured(p,'iam_alt_root' if mode=='cross_account' else 'iam_root'))
            bucket=BucketScope(owner)
            if mode=='create_bucket':
                absent=bucket.raw('bucket_head')
                if absent.get('ok') or (absent.get('error_kind'),absent.get('status_code'))!=('not_found',404):raise RuntimeError('Cannot establish generated bucket absence before role-created bucket')
                # Enter only the actual owned cleanup callback. Creation remains
                # the original denied-then-allowed calls through role credentials.
                stack.push(bucket)
            else:stack.enter_context(bucket)
        trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':user['Arn']}}]})
        role=checked(scoped_retry(main,'CreateRole','MalformedPolicyDocument',RoleName=role_name,Path=main.path,AssumeRolePolicyDocument=trust))['Role']
        if key is None:key=main.call('CreateAccessKey',UserName=name)['AccessKey']
        client=stack.enter_context(credentials(p,key,True));session=checked(retry(client,'api.sts.AssumeRole',('InvalidClientTokenId','AccessDenied'),identity='iam',input={'RoleArn':role['Arn'],'RoleSessionName':'MySession'}))['Credentials'];temporary=stack.enter_context(credentials(p,session,mode=='get_role'))
        operation='api.ListBuckets' if mode=='list_buckets' else 'api.ListObjects' if mode in ('same_account','cross_account') else 'api.iam.GetRole'
        values={} if mode=='list_buckets' else {'Bucket':bucket.name} if bucket is not None else {'RoleName':role_name}
        if mode=='create_bucket':result=create_request(temporary,bucket)
        else:result=temporary.ctx.worker.call({'op':operation,**({'identity':'iam'} if mode=='get_role' else {}),'input':values})
        error(result,code='AccessDenied',status=403)
        action='s3:ListAllMyBuckets' if mode=='list_buckets' else 's3:ListBucket' if mode in ('same_account','cross_account') else ['s3:CreateBucket','s3:PutBucketAcl'] if mode=='create_bucket' else 'iam:GetRole'
        policy_name='AllowListAllMyBuckets' if mode=='list_buckets' else 'AllowListBucket' if mode in ('same_account','cross_account') else 'AllowCreateBucket' if mode=='create_bucket' else 'AllowGetRole'
        document=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':action,'Resource':'*'}]});main.call('PutRolePolicy',RoleName=role_name,PolicyName=policy_name,PolicyDocument=document)
        configured_policy=False
        try:
            if mode=='cross_account':
                error(temporary.ctx.worker.call({'op':operation,'input':values}),code='AccessDenied',status=403)
                main_arn=main.call('GetUser')['User']['Arn'];policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':main_arn},'Action':'s3:ListBucket','Resource':'arn:aws:s3:::'+bucket.name}]});configured_policy=True
                checked(bucket.raw('api.PutBucketPolicy',input={'Policy':policy}))
            if mode=='create_bucket':
                checked(create_retry(temporary,bucket));acl=checked(bucket.raw('api.GetBucketAcl',input={}))
                assert_equal(acl['Owner']['ID'],account);assert_equal(acl['Grants'][0]['Grantee']['ID'],account)
            else:checked(retry(temporary,operation,'AccessDenied',identity='iam' if mode=='get_role' else None,input=values))
        finally:
            if configured_policy:
                result=bucket.raw('api.DeleteBucketPolicy',input={})
                if not result.get('ok'):error(result,status=404)


SPECS=(('test_account_role_policy_allow','list_buckets'),('test_same_account_role_policy_allow','same_account'),('test_cross_account_role_policy_allow','cross_account'),('test_account_role_policy_allow_create_bucket','create_bucket'),('test_account_role_policy_allow_get_role','get_role'))
BASE='identity_iam_root '+' '.join('api.iam.'+op for op in ('GetUser','CreateUser','DeleteUser','CreateAccessKey','DeleteAccessKey','CreateRole','GetRole','DeleteRole','PutRolePolicy','DeleteRolePolicy'))+' api.sts.AssumeRole'
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_'),'Ceph真实AssumeRole权限变更','真实新IAM key→STS临时凭据→实际S3/IAM调用；保留原source账号/资源/策略/403两阶段、十次原退避和Owner/Grants账户ID断言；所有实际资源按原身份清理',BASE+('' if mode=='list_buckets' else ' identity_iam_alt_root')+' api.ListBuckets api.ListObjects api.GetBucketAcl api.PutBucketPolicy api.DeleteBucketPolicy '+SCOPE_OPERATIONS,lambda p,m=mode:assumed(p,m),ref,CEPH.replace('test_s3.py','test_iam.py')) for ref,mode in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_assumed_roles',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
