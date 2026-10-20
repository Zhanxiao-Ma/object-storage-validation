"""Bind statically migrated web-identity sources to real SDK workers and owned-resource cleanup."""
from contextlib import ExitStack
from contextvars import ContextVar
from types import SimpleNamespace
import os
import uuid
from .ceph_iam_policy_scope import credentials,configured
from .scenarios import require_ok
from .suites import CaseNotRun

_ACTIVE=ContextVar('sts_web_runtime')


class ClientError(RuntimeError):
    """Expose actual SDK service errors in the source's original nested response shape; never add its erroneous top-level Code lookup."""
    def __init__(self,result):
        self.response={'Error':{'Code':result.get('error_code'),'Message':result.get('error_message','')},'ResponseMetadata':{'HTTPStatusCode':result.get('status_code')}}
        super().__init__('Actual SDK service error: '+str(result.get('status_code'))+' '+str(result.get('error_code')))


def response(result):
    if not result.get('ok'):
        if isinstance(result.get('status_code'),int) and result['status_code']>0 and result.get('error_code'):raise ClientError(result)
        raise RuntimeError('Actual SDK transport failed: '+str(result.get('error_kind')))
    return {**result.get('data',{}),'ResponseMetadata':{'HTTPStatusCode':result.get('status_code')}}


class IamClient:
    """Dispatch exact source IAM operations using independent explicit IAM credentials and retain real immutable ownership identifiers."""
    APIS={'create_role':'CreateRole','put_role_policy':'PutRolePolicy','create_open_id_connect_provider':'CreateOpenIDConnectProvider','get_open_id_connect_provider':'GetOpenIDConnectProvider','delete_open_id_connect_provider':'DeleteOpenIDConnectProvider'}
    def __init__(self,runtime):self.r=runtime
    def __getattr__(self,name):
        if name not in self.APIS:raise AttributeError(name)
        operation=self.APIS[name]
        def call(**values):
            if operation=='CreateRole':
                existing=self.r.iam('GetRole',RoleName=values['RoleName'])
                if existing.get('ok'):raise CaseNotRun('Generated role already exists; refusing reuse','fresh owned role')
                if existing.get('error_code')!='NoSuchEntity':raise CaseNotRun('Cannot establish generated role absence before mutation','GetRole NoSuchEntity ownership proof')
                self.r.role_attempts[values['RoleName']]=values['Path']
            if operation=='CreateOpenIDConnectProvider':
                listed=self.r.iam('ListOpenIDConnectProviders')
                if not listed.get('ok'):raise CaseNotRun('Cannot establish original fixed provider absence before mutation','ListOpenIDConnectProviders ownership proof')
                before=response(listed).get('OpenIDConnectProviderList',[])
                existing={item['Arn'] for item in before};self.r.existing_providers.update(existing)
                suffix=':oidc-provider/'+values['Url'].split('://',1)[-1]
                if any(arn.endswith(suffix) for arn in existing):raise CaseNotRun('Original fixed issuer already has a provider; refusing replacement or deletion','fresh dedicated OIDC issuer')
                self.r.provider_attempts.add(values['Url'])
            if operation=='DeleteOpenIDConnectProvider':self.r.verify_provider(values['OpenIDConnectProviderArn'])
            if operation=='PutRolePolicy':self.r.role_policies.add((values['RoleName'],values['PolicyName']))
            result=self.r.iam(operation,**values);data=response(result)
            if operation=='CreateRole':
                role=data['Role']
                if role['RoleName']!=values['RoleName'] or not role['RoleId']:raise RuntimeError('SDK returned a different role; ownership unresolved')
                self.r.roles[role['RoleName']]=role['RoleId']
            elif operation=='CreateOpenIDConnectProvider':
                arn=data['OpenIDConnectProviderArn']
                if arn in self.r.existing_providers:raise RuntimeError('Provider creation returned a pre-existing ARN; refusing ownership')
                self.r.providers[arn]=values['Url'];self.r.provider_attempts.discard(values['Url'])
            elif operation=='DeleteOpenIDConnectProvider':
                gone=self.r.iam('GetOpenIDConnectProvider',OpenIDConnectProviderArn=values['OpenIDConnectProviderArn'])
                if gone.get('ok') or gone.get('error_code')!='NoSuchEntity':raise RuntimeError('Provider deletion was not confirmed')
                self.r.providers.pop(values['OpenIDConnectProviderArn'],None)
            return data
        return call


class StsClient:
    """Pass the source's actual supplied token/policy/role/session directly to the official STS SDK, with configured alternate credentials."""
    def __init__(self,runtime):self.r=runtime;self.p=runtime.stack.enter_context(configured(runtime.p,'alt',consumer_iam=True))
    def assume_role_with_web_identity(self,**values):
        return response(self.p.ctx.worker.call({'op':'api.sts.AssumeRoleWithWebIdentity','identity':'iam','input':values}))


class S3Client:
    """Translate source request arguments to official S3 RPCs; real STS outputs configure each temporary client without replacing signing or permissions."""
    def __init__(self,runtime,probe):self.r=runtime;self.p=probe
    def raw(self,operation,bucket,**values):return self.p.ctx.worker.call({'op':operation,'bucket':bucket,**values})
    def create_bucket(self,**values):
        name=values['Bucket'];self.r.prove_bucket_absent(name)
        result=self.raw('bucket_create_raw',name)
        if result.get('ok'):self.r.buckets[name]=set()
        elif result.get('status_code') is None or result.get('status_code',0)<0 or result.get('status_code',0)>=500:self.r.ambiguous_buckets.add(name)
        return response(result)
    def delete_bucket(self,**values):
        name=values['Bucket']
        if name not in self.r.buckets:raise RuntimeError('Refusing deletion of a bucket not created by this source')
        result=self.raw('bucket_delete',name);data=response(result)
        gone=self.r.owner.raw('bucket_head',name)
        if gone.get('status_code')!=404 or gone.get('error_kind')!='not_found':raise RuntimeError('Source bucket deletion was not confirmed')
        self.r.buckets.pop(name,None);return data
    def put_object(self,**values):
        name=values['Bucket'];key=values['Key']
        if name not in self.r.buckets and name not in self.r.absent_literals:raise RuntimeError('Refusing write outside owned or proven-absent source namespace')
        if name in self.r.buckets:self.r.buckets[name].add(key)
        body=values['Body'];self.p.ctx.body.write_bytes(body.encode() if isinstance(body,str) else body)
        headers={'x-amz-tagging':values['Tagging']} if 'Tagging' in values else {}
        options={'wire_headers':headers} if headers else {}
        return response(self.raw('put_http',name,key=key,body_path=str(self.p.ctx.body),**options))
    def get_object(self,**values):return response(self.raw('get_http',values['Bucket'],key=values['Key'],output_path=str(self.p.ctx.output)))
    def put_bucket_policy(self,**values):return response(self.raw('api.PutBucketPolicy',values['Bucket'],input={'Policy':values['Policy']}))
    def copy(self,source,bucket,key):
        # boto3's managed copy first issues HeadObject; preserve that real
        # permission/metadata producer before the official copy consumer.
        head=self.raw('head_http',source['Bucket'],key=source['Key']);response(head)
        size=head.get('size')
        if not isinstance(size,int) or size<0:raise RuntimeError('Actual managed-copy source size is missing or invalid')
        if size>=8*1024*1024:raise CaseNotRun('Original managed copy selected multipart; this source uses a small literal body','managed multipart copy')
        self.r.buckets[bucket].add(key)
        response(self.raw('copy',bucket,key=key,source_bucket=source['Bucket'],source_key=source['Key']))
    def put_bucket_tagging(self,**values):return response(self.raw('api.PutBucketTagging',values['Bucket'],input={'Tagging':values['Tagging']}))


class Runtime:
    """Retain fixed issuer/literal names, prove absence before mutation and clean only actual newly returned roles/providers/buckets on all assertion paths."""
    def __init__(self,p,needs,literals=()):
        self.p=p;self.needs=needs;self.literals=literals;self.stack=ExitStack();self.roles={};self.role_attempts={};self.role_policies=set();self.providers={};self.provider_attempts=set();self.existing_providers=set();self.buckets={};self.ambiguous_buckets=set();self.absent_literals=set();self.generated=set();self.count=0;self.prefix='osv'+uuid.uuid4().hex[:18]
    def iam(self,operation,**values):return self.p.ctx.worker.call({'op':'api.iam.'+operation,'identity':'iam','input':values})
    def __enter__(self):
        self.cfg=self.p.ctx.workload.get('sts_web_identity')
        if not isinstance(self.cfg,dict):raise CaseNotRun('WebIdentity requires original issuer metadata and real externally supplied token','workload.sts_web_identity')
        if self.p.ctx.workload.get('iam_mutation_authorized') is not True:raise CaseNotRun('WebIdentity mutates an explicitly isolated IAM account','workload.iam_mutation_authorized')
        self.env={**os.environ,**self.p.ctx.worker.target.get('env',{})}
        for field in self.needs:
            if field in ('token','user_token'):
                name=self.cfg.get(field+'_env','OSV_WEB_IDENTITY_'+field.upper())
                if not isinstance(name,str) or not name or not self.env.get(name):raise CaseNotRun('Real source token is absent; no JWT is manufactured','sts_web_identity.'+field+'_env')
            elif not isinstance(self.cfg.get(field),str) or not self.cfg[field]:raise CaseNotRun('Original source issuer claim metadata is missing','sts_web_identity.'+field)
        for cap in ('identity_iam','identity_alt'):
            if cap not in self.p.ctx.worker.capabilities:raise CaseNotRun('Source requires explicitly configured IAM and alternate credentials',cap)
        for key in ('OSV_STS_ENDPOINT','OSV_STS_REGION'):
            if not self.env.get(key):raise CaseNotRun('STS endpoint/region must be explicit',key)
        if self.literals and self.p.ctx.workload.get('ceph_literal_bucket_names_authorized') is not True:raise CaseNotRun('Fixed test1/test2 names require a dedicated exclusive endpoint','workload.ceph_literal_bucket_names_authorized')
        try:
            self.owner=S3Client(self,self.stack.enter_context(configured(self.p,'iam')))
            for name in self.literals:self.prove_bucket_absent(name);self.absent_literals.add(name)
            self.token=_ACTIVE.set(self)
            return self
        except BaseException:self.stack.close();raise
    def prove_bucket_absent(self,name):
        if name in self.buckets:raise RuntimeError('Source attempted to create an already owned bucket')
        if name not in self.generated and name not in self.literals:raise RuntimeError('Bucket name is outside the source owned namespace')
        if name in self.absent_literals:return
        result=self.owner.raw('bucket_head',name)
        if result.get('ok'):raise CaseNotRun('Source bucket already exists; refusing reuse or deletion','fresh source bucket '+name)
        if result.get('status_code')!=404 or result.get('error_kind')!='not_found':raise CaseNotRun('Cannot prove source bucket absence','HEAD404 '+name)
    def verify_provider(self,arn):
        if arn not in self.providers:raise RuntimeError('Refusing deletion of an unowned OIDC provider')
        data=response(self.iam('GetOpenIDConnectProvider',OpenIDConnectProviderArn=arn));url=self.providers[arn]
        if data['Url'] not in (url,url.split('://',1)[-1]):raise RuntimeError('OIDC provider URL differs from actual source creation; refusing cleanup')
    def __exit__(self,typ,error,tb):
        failures=[]
        try:
            # A server may commit a create before its SDK response is lost.
            # Reconcile only pre-proven-absent UUID names and newly listed ARNs,
            # consuming actual GetRole/GetProvider identity output before cleanup.
            for name,path in self.role_attempts.items():
                if name in self.roles:continue
                try:
                    result=self.iam('GetRole',RoleName=name)
                    if not result.get('ok') and result.get('error_code')=='NoSuchEntity':continue
                    actual=response(result)['Role']
                    if actual['RoleName']!=name or actual['Path']!=path or not actual['RoleId']:raise RuntimeError('Ambiguous role ownership could not be established')
                    self.roles[name]=actual['RoleId']
                except Exception as exc:failures.append('ambiguous role '+name+': '+str(exc))
            for url in self.provider_attempts:
                try:
                    listed=response(self.iam('ListOpenIDConnectProviders')).get('OpenIDConnectProviderList',[])
                    matching=[item['Arn'] for item in listed if item['Arn'] not in self.existing_providers and item['Arn'].endswith(':oidc-provider/'+url.split('://',1)[-1])]
                    if len(matching)>1:raise RuntimeError('Ambiguous provider ownership could not be established')
                    for arn in matching:
                        actual=response(self.iam('GetOpenIDConnectProvider',OpenIDConnectProviderArn=arn))
                        if actual['Url'] not in (url,url.split('://',1)[-1]):raise RuntimeError('Ambiguous provider URL changed')
                        self.providers[arn]=url
                except Exception as exc:failures.append('ambiguous provider: '+str(exc))
            for name,keys in reversed(list(self.buckets.items())):
                try:
                    for key in sorted(keys):response(self.owner.raw('delete',name,key=key))
                    response(self.owner.raw('bucket_delete',name))
                    gone=self.owner.raw('bucket_head',name)
                    if gone.get('status_code')!=404 or gone.get('error_kind')!='not_found':raise RuntimeError('Bucket deletion was not confirmed')
                except Exception as exc:failures.append('bucket '+name+': '+str(exc))
            for name,uid in reversed(list(self.roles.items())):
                try:
                    result=self.iam('GetRole',RoleName=name)
                    if not result.get('ok') and result.get('error_code')=='NoSuchEntity':continue
                    role=response(result)['Role']
                    if role['RoleId']!=uid:raise RuntimeError('Role immutable ID changed; refusing cleanup')
                    for role_name,policy in self.role_policies:
                        if role_name==name:
                            deleted=self.iam('DeleteRolePolicy',RoleName=name,PolicyName=policy)
                            if not deleted.get('ok') and deleted.get('error_code')!='NoSuchEntity':response(deleted)
                    response(self.iam('DeleteRole',RoleName=name))
                    gone=self.iam('GetRole',RoleName=name)
                    if gone.get('ok') or gone.get('error_code')!='NoSuchEntity':raise RuntimeError('Role deletion was not confirmed')
                except Exception as exc:failures.append('role '+name+': '+str(exc))
            for arn in list(self.providers):
                try:
                    self.verify_provider(arn);response(self.iam('DeleteOpenIDConnectProvider',OpenIDConnectProviderArn=arn))
                    gone=self.iam('GetOpenIDConnectProvider',OpenIDConnectProviderArn=arn)
                    if gone.get('ok') or gone.get('error_code')!='NoSuchEntity':raise RuntimeError('Provider deletion was not confirmed')
                except Exception as exc:failures.append('provider: '+str(exc))
            for name in self.ambiguous_buckets:failures.append('ambiguous create; ownership unresolved, inspect '+name)
        finally:_ACTIVE.reset(self.token);self.stack.close()
        if failures:raise RuntimeError('WebIdentity owned-resource cleanup failed: '+'; '.join(failures)) from error
        return False
    def parameter(self):self.count+=1;return self.prefix+'p'+str(self.count)
    def new_bucket(self):
        name='osv-'+uuid.uuid4().hex;self.generated.add(name);return name
    def s3(self,key):return S3Client(self,self.stack.enter_context(credentials(self.p,key)))


def current():return _ACTIVE.get()
def check_webidentity():return None # Runtime validates every prerequisite before the first source operation.
def get_iam_client():return IamClient(current())
def get_sts_client():return StsClient(current())
def get_config_endpoint():return current().p.ctx.worker.info['endpoint']
def get_parameter_name():return current().parameter()
def get_new_bucket_name():return current().new_bucket()
def get_thumbprint():return current().cfg['thumbprint']
def get_realm_name():return current().cfg['realm']
def get_aud():return current().cfg['aud']
def get_sub():return current().cfg['sub']
def get_azp():return current().cfg['azp']
def get_token():return current().env[current().cfg.get('token_env','OSV_WEB_IDENTITY_TOKEN')]
def get_user_token():return current().env[current().cfg.get('user_token_env','OSV_WEB_IDENTITY_USER_TOKEN')]
def get_iam_access_key():return current().env['OSV_IAM_ACCESS_KEY']
def get_iam_secret_key():return current().env['OSV_IAM_SECRET_KEY']
def get_s3_client_using_iam_creds():return current().owner


class S3Resource:
    """Expose only original BucketTagging resource calls; route actual duplicate tag lists directly into the typed SDK."""
    def __init__(self,r):self.r=r;self.meta=SimpleNamespace(client=r.owner)
    def BucketTagging(self,name):return SimpleNamespace(put=lambda **values:self.r.owner.put_bucket_tagging(Bucket=name,**values))


def get_s3_resource_using_iam_creds():return S3Resource(current())


class SdkClientFactory:
    """Construct genuine temporary S3 SDK workers from actual STS output; reject endpoint rewriting or credential fallback."""
    def client(self,service,**values):
        if service!='s3' or values['endpoint_url']!=get_config_endpoint():raise ValueError('Source temporary client must keep its explicit S3 endpoint')
        return current().s3({'AccessKeyId':values['aws_access_key_id'],'SecretAccessKey':values['aws_secret_access_key'],'SessionToken':values['aws_session_token']})


boto3=SdkClientFactory()
