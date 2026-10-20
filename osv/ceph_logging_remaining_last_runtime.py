"""Bind the final tenant/locked-metadata logging sources to actual SDK namespace identities and proven same-key governance cleanup."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import time
from .ceph_logging_remaining_extension_runtime import ExtensionRuntime,ExtensionClient
from .ceph_logging_remaining_runtime import current,response
from .ceph_object_locks import LockScope,BYPASS
from .version_scope import VersionScope
from .suites import Probe,CaseNotRun


class TenantWorker:
    """Select the actual configured tenant SDK client for every namespace operation; retain the main endpoint and refuse any implicit main-identity fallback."""
    def __init__(self,worker):self.worker=worker
    def __getattr__(self,name):return getattr(self.worker,name)
    def call(self,request):return self.worker.call({**request,'identity':'tenant'})


class OwnedLockScope(LockScope):
    """Reuse real version/hold/governance cleanup while binding exactly the original generated bucket name and retaining its actual create response."""
    def __init__(self,probe,name):
        VersionScope.__init__(self,probe,name);self.create_operation='bucket_create_locked';self.compliance_expiry=0;self.allow_unlocked=False
    def raw(self,operation,**values):
        actual=super().raw(operation,**values)
        if operation==self.create_operation:self.creation_response=actual
        return actual
    def prove_source_key_cleanup(self):
        self.probe.ctx.body.write_bytes(b'actual same-key cleanup prerequisite')
        actual=self.call('put_version',key='myobject',body_path=str(self.probe.ctx.body));version=actual.get('version_id')
        if not isinstance(version,str) or not version:raise CaseNotRun('Same-key cleanup probe has no actual version ID','actual SDK PutObject version')
        expiry=int((datetime.now(timezone.utc)+timedelta(seconds=2)).timestamp()*1000)
        retained=self.raw('api.PutObjectRetention',input={'Key':'myobject','VersionId':version,'Retention':{'Mode':'GOVERNANCE','RetainUntilDate':expiry}})
        deleted=self.raw('delete_version',key='myobject',version_id=version,wire_headers=BYPASS) if retained.get('ok') else {'ok':False}
        if not retained.get('ok') or not deleted.get('ok'):
            delay=expiry/1000-time.time()+.15
            if delay>0:time.sleep(delay)
            cleaned=self.raw('delete_version',key='myobject',version_id=version)
            response(cleaned)
            raise CaseNotRun('Actual same-bucket/myobject governance bypass proof failed; short probe expired and was removed','successful real governance retention and bypass deletion')


class MetadataClient(ExtensionClient):
    """Preserve original version/ACL/tag/hold/retention arguments through official typed models; prove cleanup before the source installs its original sixty-day governance retention."""
    def create_bucket(self,**values):
        if 'ObjectLockEnabledForBucket' not in values:return super().create_bucket(**values)
        if values['ObjectLockEnabledForBucket'] is not True:raise ValueError('original source requires lock-enabled creation')
        name=values['Bucket']
        if name not in self.r.generated or name in self.claims:raise RuntimeError('Lock bucket outside fresh generated source namespace')
        scope=OwnedLockScope(self.p,name);scope.__enter__();self.claims[name]=scope;self.r.scopes.append((self,scope));scope.prove_source_key_cleanup()
        return response(scope.creation_response)
    def put_object(self,**values):
        if not isinstance(self.claims.get(values['Bucket']),OwnedLockScope):return super().put_object(**values)
        body=values['Body'];self.p.ctx.body.write_bytes(body.encode() if isinstance(body,str) else body)
        actual=self.call('put_version',values['Bucket'],key=values['Key'],body_path=str(self.p.ctx.body));out=response(actual)
        if 'version_id' in actual:out['VersionId']=actual['version_id']
        return out
    def object_api(self,operation,values):
        inputs={key:value for key,value in values.items() if key!='Bucket'}
        if operation=='PutObjectRetention':
            document=inputs['Retention'];date=document['RetainUntilDate']
            if not isinstance(date,datetime):raise TypeError('original retention input must be actual datetime producer')
            inputs['Retention']={**document,'RetainUntilDate':int(date.timestamp()*1000)}
        return response(self.call('api.'+operation,values['Bucket'],input=inputs))
    def put_object_acl(self,**values):return self.object_api('PutObjectAcl',values)
    def put_object_tagging(self,**values):
        tags=[{'key':tag['Key'],'value':tag['Value']} for tag in values['Tagging']['TagSet']]
        return response(self.call('tags_put',values['Bucket'],key=values['Key'],tags=tags,wire_query={'versionId':values['VersionId']}))
    def delete_object_tagging(self,**values):return response(self.call('tags_delete',values['Bucket'],key=values['Key'],wire_query={'versionId':values['VersionId']}))
    def put_object_legal_hold(self,**values):return self.object_api('PutObjectLegalHold',values)
    def put_object_retention(self,**values):return self.object_api('PutObjectRetention',values)
    def delete_object(self,**values):
        if type(values.get('BypassGovernanceRetention')) is not bool:raise TypeError('Original bypass condition must remain boolean')
        return response(self.call('delete_version',values['Bucket'],key=values['Key'],version_id=values['VersionId'],**({'wire_headers':BYPASS} if values['BypassGovernanceRetention'] else {})))


class LastRuntime(ExtensionRuntime):
    """Require real tenant metadata/credentials or explicit isolated governance authorization before any source probes, preserving complete ownership across all original branches."""
    def __enter__(self):
        if self.mode=='tenant':
            work=self.p.ctx.workload
            if not isinstance(work.get('tenant_name'),str) or not work['tenant_name']:raise CaseNotRun('Original namespace source needs actual tenant name','workload.tenant_name')
            if not isinstance(work.get('identities',{}).get('tenant',{}).get('user_id'),str) or not work['identities']['tenant']['user_id']:raise CaseNotRun('Original tenant policy needs independently configured tenant user ID','workload.identities.tenant.user_id')
            if 'identity_tenant' not in self.p.ctx.worker.capabilities:raise CaseNotRun('Original tenant client cannot fall back to main identity','identity_tenant')
        elif self.mode=='metadata' and self.p.ctx.workload.get('object_lock_cleanup_authorized') is not True:raise CaseNotRun('Original hold/60-day governance writes need explicitly authorized isolated cleanup','workload.object_lock_cleanup_authorized=true')
        super().__enter__();self.tenant_client=None
        if self.mode=='metadata':self.main=MetadataClient(self,self.p)
        return self
    def tenant(self):
        if self.tenant_client is None:self.tenant_client=ExtensionClient(self,Probe(replace(self.p.ctx,worker=TenantWorker(self.p.ctx.worker))))
        return self.tenant_client


def get_tenant_client():return current().tenant()
def get_tenant_name():return current().p.ctx.workload['tenant_name']
def get_tenant_user_id():return current().p.ctx.workload['identities']['tenant']['user_id']
