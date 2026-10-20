"""Provide real official SDK clients and ownership scopes for additional pinned logging bodies."""
from contextlib import ExitStack
from contextvars import ContextVar
import io
import os
import re
from types import SimpleNamespace
import uuid
from .bucket_scope import BucketScope
from .ceph_iam_policy_scope import configured,credentials
from .ceph_sts_web_runtime import IamClient,ClientError
from .suites import CaseNotRun

_ACTIVE=ContextVar('logging_remaining_runtime')


class ParamValidationError(RuntimeError):
    """Represent only an actual official SDK model-field rejection, allowing the original source probe to take its normal rollover fallback."""


def response(result):
    if not result.get('ok'):
        if isinstance(result.get('status_code'),int) and result['status_code']>0 and result.get('error_code'):raise ClientError(result)
        raise RuntimeError('Actual logging SDK request failed: '+str(result.get('error')))
    return {**result.get('data',{}),'ResponseMetadata':{'HTTPStatusCode':result.get('status_code'),'HTTPHeaders':result.get('http_headers',{})}}


class OwnedBucket(BucketScope):
    """Retain the actual create RPC response while reusing shared UUID ownership and cleanup, without manufacturing source metadata."""
    def raw(self,operation,**values):
        actual=super().raw(operation,**values)
        if operation==self.create_operation:self.creation_response=actual
        return actual


class S3Client:
    """Forward original S3 operations to official SDK RPCs and retain actual log bodies/list order without emulating service authorization or delivery."""
    def __init__(self,r,p):self.r=r;self.p=p;self.claims={}
    def raw(self,operation,name,**values):return self.p.ctx.worker.call({'op':operation,'bucket':name,**values})
    def create_bucket(self,**values):
        name=values['Bucket']
        if name not in self.r.generated:raise RuntimeError('Source bucket is outside this run UUID namespace')
        if name in self.claims:raise RuntimeError('Source bucket already owned; refusing duplicate creation')
        scope=OwnedBucket(self.p,name=name);scope.__enter__();self.claims[name]=scope;self.r.scopes.append((self,scope))
        return response(scope.creation_response)
    def call(self,operation,name,**values):
        if name not in self.claims:raise RuntimeError('Source operation has no owned bucket: '+name)
        return self.claims[name].raw(operation,**values)
    def put_object(self,**values):
        body=values['Body'];self.p.ctx.body.write_bytes(body.encode() if isinstance(body,str) else body)
        return response(self.call('put_http',values['Bucket'],key=values['Key'],body_path=str(self.p.ctx.body)))
    def get_object(self,**values):
        result=self.call('get_http',values['Bucket'],key=values['Key'],output_path=str(self.p.ctx.output));data=response(result)
        data['Body']=io.BytesIO(self.p.ctx.output.read_bytes());return data
    def list_objects_v2(self,**values):
        result=self.call('list_v2_observed',values['Bucket']);data=response(result)
        data['Contents']=[{'Key':item['key']} for item in result['objects']];return data
    def put_bucket_policy(self,**values):return response(self.call('api.PutBucketPolicy',values['Bucket'],input={'Policy':values['Policy']}))
    def put_bucket_logging(self,**values):
        name=values['Bucket'];document=values['BucketLoggingStatus']
        if name not in self.claims and name not in self.r.generated:raise RuntimeError('Logging probe escaped generated namespace')
        result=self.raw('api.PutBucketLogging',name,input={'BucketLoggingStatus':document})
        # The source probes a non-existing name using LoggingType. Execute the
        # real SDK model reader first, then preserve only its proven rejection
        # as a client parameter error, never infer support from the service.
        if not result.get('ok') and re.search(r'SDK model has no member:? LoggingType',str(result.get('error',''))):
            self.r.actual_extension_rejections+=1
            raise ParamValidationError('Actual official SDK model rejects LoggingType')
        return response(result)


class RootClient:
    """Expose the original IAM root GetUser response directly, so ARN-derived SourceAccount values come from the actual producer."""
    def __init__(self,r):self.r=r
    def get_user(self,**values):return response(self.r.p.ctx.worker.call({'op':'api.iam.GetUser','identity':'iam_root','input':values}))


class StsClient:
    """Use the configured source alternate identity for actual STS AssumeRole; returned credentials feed independent SDK consumers unchanged."""
    def __init__(self,r):self.p=r.stack.enter_context(configured(r.p,'alt',consumer_iam=True))
    def assume_role(self,**values):return response(self.p.ctx.worker.call({'op':'api.sts.AssumeRole','identity':'iam','input':values}))


class Runtime:
    """Own each actual creator session/bucket and role ID; stop all logging before deleting policies/data and before revoking temporary role permissions."""
    def __init__(self,p,mode):
        self.p=p;self.mode=mode;self.stack=ExitStack();self.generated=set();self.scopes=[];self.roles={};self.role_attempts={};self.role_policies=set();self.providers={};self.provider_attempts=set();self.existing_providers=set();self.actual_extension_rejections=0;self.parameter_counter=0;self.prefix='osv'+uuid.uuid4().hex[:18]
    def __enter__(self):
        identities=self.p.ctx.workload.get('identities',{})
        required=('main',) if self.mode=='account' else ('alt',)
        for identity in required:
            user=identities.get(identity,{}).get('user_id')
            if not isinstance(user,str) or not user:raise CaseNotRun('Original logging policy/requester needs independently configured user ID','workload.identities.'+identity+'.user_id')
        if self.mode=='account':
            if 'identity_iam_root' not in self.p.ctx.worker.capabilities:raise CaseNotRun('Original account vectors require actual IAM root identity and explicit transport','identity_iam_root')
            actual=RootClient(self).get_user()['User']
            if not actual['Arn'].endswith(':root'):raise CaseNotRun('Original account source requires actual root GetUser ARN','IAM root identity proof')
        else:
            environment={**os.environ,**self.p.ctx.worker.target.get('env',{})}
            if not environment.get('OSV_STS_ENDPOINT') or not environment.get('OSV_STS_REGION'):raise CaseNotRun('Requester source requires explicit actual STS endpoint/region','OSV_STS_ENDPOINT/REGION')
            if self.p.ctx.workload.get('iam_mutation_authorized') is not True:raise CaseNotRun('Original requester source creates role and policy on an isolated authorized account','workload.iam_mutation_authorized')
            for capability in ('identity_iam','identity_alt','api.sts.AssumeRole'):
                if capability not in self.p.ctx.worker.capabilities:raise CaseNotRun('Original requester requires independent actual IAM/STS identities',capability)
        try:
            self.main=S3Client(self,self.p);self.root_client=None
            self.token=_ACTIVE.set(self)
            return self
        except BaseException:self.stack.close();raise
    def new_bucket(self):
        name='osv-'+uuid.uuid4().hex;self.generated.add(name);return name
    def parameter(self):self.parameter_counter+=1;return self.prefix+'p'+str(self.parameter_counter)
    def root(self):
        if self.root_client is None:self.root_client=S3Client(self,self.stack.enter_context(configured(self.p,'iam_root')))
        return self.root_client
    def temporary(self,key):return S3Client(self,self.stack.enter_context(credentials(self.p,key)))
    def iam(self,operation,**values):return self.p.ctx.worker.call({'op':'api.iam.'+operation,'identity':'iam','input':values})
    def cleanup_logging(self):
        failures=[]
        # Do not revoke a role until its actual temporary SDK session has
        # disabled source loggers and cleaned all of its own buckets.
        for client,scope in self.scopes:
            try:response(scope.raw('api.PutBucketLogging',input={'BucketLoggingStatus':{}}))
            except Exception as error:failures.append('disable '+scope.name+': '+str(error))
        for client,scope in self.scopes:
            try:
                result=scope.raw('api.DeleteBucketPolicy',input={})
                if not result.get('ok') and result.get('status_code')!=404:response(result)
            except Exception as error:failures.append('policy '+scope.name+': '+str(error))
        for client,scope in reversed(list(self.scopes)):
            try:
                scope.__exit__(None,None,None);client.claims.pop(scope.name,None);self.scopes.remove((client,scope))
            except Exception as error:failures.append('bucket '+scope.name+': '+str(error))
        if failures:raise RuntimeError('Logging owned-resource cleanup failed: '+'; '.join(failures))
    def delete_role(self,name):
        if name not in self.roles:raise RuntimeError('Cannot delete a role without its actual created immutable ID')
        result=self.iam('GetRole',RoleName=name)
        if not result.get('ok') and result.get('error_code')=='NoSuchEntity':self.roles.pop(name,None);self.role_attempts.pop(name,None);return
        actual=response(result)['Role']
        if actual['RoleId']!=self.roles[name]:raise RuntimeError('Role ID changed; refusing cleanup')
        for role_name,policy in self.role_policies:
            if role_name==name:
                deleted=self.iam('DeleteRolePolicy',RoleName=name,PolicyName=policy)
                if not deleted.get('ok') and deleted.get('error_code')!='NoSuchEntity':response(deleted)
        response(self.iam('DeleteRole',RoleName=name))
        gone=self.iam('GetRole',RoleName=name)
        if gone.get('ok') or gone.get('error_code')!='NoSuchEntity':raise RuntimeError('Role deletion not confirmed')
        self.roles.pop(name,None);self.role_attempts.pop(name,None)
    def __exit__(self,typ,error,tb):
        failures=[]
        try:
            try:self.cleanup_logging()
            except Exception as problem:failures.append(str(problem))
            for name,path in self.role_attempts.items():
                if name in self.roles:continue
                try:
                    result=self.iam('GetRole',RoleName=name)
                    if not result.get('ok') and result.get('error_code')=='NoSuchEntity':continue
                    actual=response(result)['Role']
                    if actual['RoleName']!=name or actual['Path']!=path or not actual['RoleId']:raise RuntimeError('Ambiguous role ownership unresolved')
                    self.roles[name]=actual['RoleId']
                except Exception as problem:failures.append(str(problem))
            for name in list(self.roles):
                try:self.delete_role(name)
                except Exception as problem:failures.append(str(problem))
        finally:_ACTIVE.reset(self.token);self.stack.close()
        if failures:raise RuntimeError('Logging final cleanup failed: '+'; '.join(failures)) from error
        return False


def current():return _ACTIVE.get()
def get_new_bucket_name():return current().new_bucket()
def get_client():return current().main
def get_iam_root_s3client():return current().root()
def get_iam_root_client():return RootClient(current())
def get_iam_client():return IamClient(current())
def get_sts_client():return StsClient(current())
def get_main_user_id():return current().p.ctx.workload['identities']['main']['user_id']
def get_alt_user_id():return current().p.ctx.workload['identities']['alt']['user_id']
def get_config_endpoint():return current().p.ctx.worker.info['endpoint']
def get_parameter_name():return current().parameter()
def get_new_bucket(client=None,name=None):
    client=client or get_client();name=name or get_new_bucket_name();client.create_bucket(Bucket=name);return name

def get_new_bucket_resource(name=None):
    name=get_new_bucket(name=name);return SimpleNamespace(name=name)

def nuke_role(client,name):
    current().cleanup_logging();current().delete_role(name)


class SdkClientFactory:
    """Create the original temporary S3 client using actual STS response credentials and unchanged explicit endpoint."""
    def client(self,service,**values):
        if service!='s3' or values['endpoint_url']!=get_config_endpoint():raise ValueError('Temporary logging client endpoint changed')
        return current().temporary({'AccessKeyId':values['aws_access_key_id'],'SecretAccessKey':values['aws_secret_access_key'],'SessionToken':values['aws_session_token']})


boto3=SdkClientFactory()
