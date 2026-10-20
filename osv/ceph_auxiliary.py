"""Seven-file tail services: native SNS and account-level S3Control source semantics.

The source assertions remain exact. Additional preconditions and cleanup only
protect uniquely owned topics/users/buckets and explicitly isolated accounts.
"""
from contextlib import ExitStack
import json
import os
import uuid
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_iam_scope import rpc as iam_rpc,checked,error
from .ceph_iam_policy_scope import PolicyScope,configured,credentials
from .scenarios import Suite,assert_equal,require_ok
from .suites import CaseNotRun,case,run_cases,CEPH


def request(p,service,api,identity='iam_root',**values):
    return p.ctx.worker.call({'op':'api.'+service+'.'+api,'identity':identity,'input':values})


def call(p,service,api,identity='iam_root',**values):
    return checked(request(p,service,api,identity,**values))


def client_error(result,code=None):
    """Retain source ClientError/type assertions without strengthening comments into status checks."""
    assert_equal(result.get('ok'),False)
    assert result.get('error_kind') in ('sdk_error','not_found')
    assert isinstance(result.get('status_code'),int) and result['status_code']>=100
    if code is not None:assert_equal(result.get('error_code'),code)


def root_account(p,identity='iam_root'):
    cap='identity_'+identity
    if cap not in p.ctx.worker.capabilities:raise CaseNotRun('Source needs explicit real IAM root identity',cap)
    arn=checked(iam_rpc(p,'GetUser',identity))['User']['Arn']
    if not isinstance(arn,str) or not arn.startswith('arn:aws:iam::') or not arn.endswith(':root'):raise CaseNotRun('Source root fixture requires real account-root ARN',identity+' root ARN')
    return arn.split(':')[4]


def sns_prerequisites(p,alternate=False):
    if p.ctx.workload.get('sns_mutation_authorized') is not True:raise CaseNotRun('SNS mutations need an isolated authorized topic scope','workload.sns_mutation_authorized')
    for identity in ('iam_root','iam_alt_root') if alternate else ('iam_root',):
        cap='identity_sns_'+identity
        if cap not in p.ctx.worker.capabilities:raise CaseNotRun('Source SNS identity needs actual endpoint/region/credentials',cap)
        root_account(p,identity)


class TopicScope:
    """Clean only a topic with this UUID name and independently verified root account.

    Real list pagination recovers ambiguous CreateTopic results. Never reproduce
    upstream nuke_topics' accidental deletion of unrelated account topics.
    """
    def __init__(self,p):self.p=p;self.name='osv-'+uuid.uuid4().hex;self.account=None;self.arn=None;self.attempted=False
    def __enter__(self):
        self.account=root_account(self.p)
        seen=set();token=None
        while True:
            result=call(self.p,'sns','ListTopics',**({'NextToken':token} if token is not None else {}))
            if any(self.owned(row.get('TopicArn')) for row in result.get('Topics',[])):raise RuntimeError('Generated topic name already exists; refusing idempotent CreateTopic takeover')
            token=result.get('NextToken')
            if token is None or token=='':break
            if not isinstance(token,str) or token in seen:raise RuntimeError('SNS preflight pagination missing/repeated token')
            seen.add(token)
        return self
    def owned(self,arn):return isinstance(arn,str) and arn.startswith('arn:aws:sns:') and len(arn.split(':'))==6 and arn.split(':')[4]==self.account and arn.endswith(':'+self.name)
    def create(self):
        self.attempted=True;result=call(self.p,'sns','CreateTopic',Name=self.name);self.arn=result['TopicArn'];return self.arn
    def __exit__(self,typ,value,tb):
        if not self.attempted:return False
        arns=set();seen=set();token=None
        if self.owned(self.arn):arns.add(self.arn)
        failures=[]
        try:
            while True:
                result=call(self.p,'sns','ListTopics',**({'NextToken':token} if token is not None else {}))
                for row in result.get('Topics',[]):
                    if self.owned(row.get('TopicArn')):arns.add(row['TopicArn'])
                token=result.get('NextToken')
                if token is None or token=='':break
                if not isinstance(token,str) or token in seen:raise RuntimeError('SNS cleanup pagination missing/repeated token')
                seen.add(token)
        except Exception as error:failures.append(str(error))
        # A failed listing must not prevent cleanup of an already proven owned ARN.
        for arn in sorted(arns):
            try:call(self.p,'sns','DeleteTopic',TopicArn=arn)
            except Exception as error:failures.append(str(error))
        if failures:raise RuntimeError('SNS owned-topic cleanup failed: '+'; '.join(failures)) from value
        return False


def topic(p):
    sns_prerequisites(p)
    with TopicScope(p) as scope:
        arn=scope.create();assert arn.startswith('arn:aws:sns:');assert arn.endswith(':'+scope.name)
        response=call(p,'sns','ListTopics');assert arn in [row['TopicArn'] for row in response['Topics']]
        call(p,'sns','SetTopicAttributes',TopicArn=arn,AttributeName='Policy',AttributeValue='')
        response=call(p,'sns','GetTopicAttributes',TopicArn=arn);assert 'Attributes' in response
        call(p,'sns','DeleteTopic',TopicArn=arn)
        response=call(p,'sns','ListTopics');assert arn not in [row['TopicArn'] for row in response['Topics']]
        client_error(request(p,'sns','GetTopicAttributes',TopicArn=arn),'NotFound')
        call(p,'sns','DeleteTopic',TopicArn=arn)


def cross_topic(p):
    sns_prerequisites(p,True)
    with TopicScope(p) as scope:
        arn=scope.create()
        for operation,values in (('GetTopicAttributes',{}),('SetTopicAttributes',{'AttributeName':'Policy','AttributeValue':''}),('DeleteTopic',{})):
            client_error(request(p,'sns',operation,'iam_alt_root',TopicArn=arn,**values),'AuthorizationError')
        call(p,'sns','DeleteTopic',TopicArn=arn)
        response=call(p,'sns','ListTopics','iam_alt_root');assert arn not in [row['TopicArn'] for row in response['Topics']]


def user_policy(p):
    sns_prerequisites(p)
    with PolicyScope(p) as iam:
        user=iam.create('User',iam.name('User'),iam.path);user_arn=user['Arn']
        key=iam.call('CreateAccessKey',UserName=user['UserName'])['AccessKey']
        with credentials(p,key) as fresh:
            client_error(request(fresh,'sns','ListTopics','main'))
            iam.call('AttachUserPolicy',UserName=user['UserName'],PolicyArn='arn:aws:iam::aws:policy/AmazonSNSReadOnlyAccess')
            call(fresh,'sns','ListTopics','main')


def publish(p,cross=False):
    sns_prerequisites(p,cross)
    with TopicScope(p) as scope,configured(p,'iam_alt_root' if cross else 'iam_root') as s3:
        arn=scope.create()
        with BucketScope(s3) as bucket:
            config={'TopicConfigurations':[{'Id':'id','TopicArn':arn,'Events':['s3:ObjectCreated:*']}]}
            def notify():return bucket.raw('api.PutBucketNotificationConfiguration',input={'NotificationConfiguration':config})
            if cross:
                error(notify(),'AccessDenied',403)
                principal=checked(iam_rpc(p,'GetUser','iam_alt_root'))['User']['Arn']
                policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':principal},'Action':'sns:Publish','Resource':arn}]})
                call(p,'sns','SetTopicAttributes',TopicArn=arn,AttributeName='Policy',AttributeValue=policy)
            require_ok(notify())


def account_block(p):
    if p.ctx.workload.get('isolated_account_mutation_authorized') is not True:raise CaseNotRun('Account-wide public ACL changes require an exclusively isolated account','workload.isolated_account_mutation_authorized')
    account=p.ctx.workload.get('account_id')
    if not isinstance(account,str) or not account:raise CaseNotRun('Source requires independent account ID','workload.account_id')
    if 'identity_s3control_iam_root' not in p.ctx.worker.capabilities:raise CaseNotRun('Account control requires real independent endpoint/root credentials','identity_s3control_iam_root')
    environment={**os.environ,**p.ctx.worker.target.get('env',{})}
    if environment.get('OSV_S3CONTROL_REGION')!='us-east-1':raise CaseNotRun('Original S3Control source explicitly uses us-east-1','OSV_S3CONTROL_REGION=us-east-1')
    assert_equal(root_account(p),account)
    def raw(api,**values):return request(p,'s3control',api,AccountId=account,**values)
    snapshot=raw('GetPublicAccessBlock')
    if snapshot.get('ok'):previous=checked(snapshot)['PublicAccessBlockConfiguration']
    else:error(snapshot,'NoSuchPublicAccessBlockConfiguration',404);previous=None
    try:
        response=raw('DeletePublicAccessBlock');require_ok(response);assert_equal(response['status_code'],204)
        response=raw('DeletePublicAccessBlock');require_ok(response);assert_equal(response['status_code'],204)
        error(raw('GetPublicAccessBlock'),'NoSuchPublicAccessBlockConfiguration',404)
        require_ok(raw('PutPublicAccessBlock',PublicAccessBlockConfiguration={'BlockPublicAcls':True,'IgnorePublicAcls':False,'BlockPublicPolicy':False,'RestrictPublicBuckets':False}))
        response=checked(raw('GetPublicAccessBlock'))['PublicAccessBlockConfiguration']
        assert response['BlockPublicAcls'];assert not response['IgnorePublicAcls'];assert not response['BlockPublicPolicy'];assert not response['RestrictPublicBuckets']
        with configured(p,'iam_root') as s3,ExitStack() as cleanup:
            bucket=BucketScope(s3);cleanup.callback(bucket.__exit__,None,None,None)
            absence=bucket.raw('bucket_head');assert_equal((absence.get('error_kind'),absence.get('status_code')),('not_found',404))
            response=bucket.raw('bucket_create_acl',acl='public-read')
            if response.get('ok'):bucket.created=True
            error(response,'AccessDenied',403)
            bucket.__enter__()
            error(bucket.raw('api.PutBucketAcl',input={'ACL':'public-read'}),'AccessDenied',403)
            require_ok(bucket.raw('api.PutPublicAccessBlock',input={'PublicAccessBlockConfiguration':{'BlockPublicAcls':False,'IgnorePublicAcls':False,'BlockPublicPolicy':True,'RestrictPublicBuckets':False}}))
            policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':'*'},'Action':'*','Resource':['arn:aws:s3:::'+bucket.name,'arn:aws:s3:::'+bucket.name+'/*']}]})
            error(bucket.raw('api.PutBucketPolicy',input={'Policy':policy}),'AccessDenied',403)
    finally:
        require_ok(raw('DeletePublicAccessBlock') if previous is None else raw('PutPublicAccessBlock',PublicAccessBlockConfiguration=previous))


SNS=' '.join('api.sns.'+x for x in ('CreateTopic','ListTopics','SetTopicAttributes','GetTopicAttributes','DeleteTopic'))
IAM='identity_iam_root api.iam.GetUser'
SPECS=(('account_topic',topic,'test_account_topic',SNS+' '+IAM),('cross_account_topic',cross_topic,'test_cross_account_topic',SNS+' '+IAM+' identity_iam_alt_root'),('account_user_policy_list_topics',user_policy,'test_account_user_policy_list_topics',SNS+' '+IAM+' api.iam.CreateUser api.iam.GetUser api.iam.CreateAccessKey api.iam.DeleteAccessKey api.iam.DeleteUser api.iam.AttachUserPolicy api.iam.DetachUserPolicy'),('account_topic_publish',lambda p:publish(p),'test_account_topic_publish',SNS+' '+IAM+' '+SCOPE_OPERATIONS+' api.PutBucketNotificationConfiguration'),('cross_account_topic_publish',lambda p:publish(p,True),'test_cross_account_topic_publish',SNS+' '+IAM+' identity_iam_alt_root '+SCOPE_OPERATIONS+' api.PutBucketNotificationConfiguration'))
CASES=tuple(case('ceph_sns_'+mode,'Ceph原始SNS '+mode,'原TopicArn/Topics/Attributes存在性、真实错误类、IAM真实key到SNS与notification权限链全部断言；只清理UUID自有topic与真实IAM资源',ops,execute,ref,CEPH.replace('test_s3.py','test_sns.py')) for mode,execute,ref,ops in SPECS)+(case('ceph_s3control_account_public_access_block','Ceph账号级公共访问阻断','保留us-east-1、两次Delete204/Get404、四布尔值与S3桶ACL/Policy联动403；显式隔离账号授权和独立account_id，实际快照finally恢复','api.s3control.GetPublicAccessBlock api.s3control.PutPublicAccessBlock api.s3control.DeletePublicAccessBlock '+IAM+' '+SCOPE_OPERATIONS+' bucket_create_acl api.PutBucketAcl api.PutPublicAccessBlock api.PutBucketPolicy',account_block,'test_account_public_access_block',CEPH.replace('test_s3.py','test_s3control.py')),)


def register(registry):
    registry.add_suite(Suite('ceph_auxiliary',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
