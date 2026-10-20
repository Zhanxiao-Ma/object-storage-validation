"""Route explicitly enabled Ceph logging wire extensions through official SDK public hooks; preserve actual response XML presence."""
import base64
from xml.etree import ElementTree as ET
from .ceph_logging_remaining_runtime import Runtime,S3Client,_ACTIVE,response,configured
from .suites import CaseNotRun


def captured(result):
    """Decode only bytes captured from the final actual SDK response; never recreate extension values from request inputs or stock-model defaults."""
    metadata=response(result)['ResponseMetadata']
    if result.get('response_body_capture_error'):raise RuntimeError(result['response_body_capture_error'])
    if 'response_body_b64' not in result:raise RuntimeError('Ceph logging extension requires actual complete captured response')
    body=base64.b64decode(result['response_body_b64'],validate=True)
    if not body:return {'ResponseMetadata':metadata}
    root=ET.fromstring(body)
    for node in root.iter():node.tag=node.tag.rsplit('}',1)[-1]
    data={'ResponseMetadata':metadata}
    def children(node):
        value={}
        for item in node:
            if item.tag in ('ObjectRollTime','RecordsBatchSize'):value[item.tag]=int(item.text or '')
            elif item.tag=='Filter':
                key=item.find('S3Key')
                if key is not None:value['Filter']={'Key':{'FilterRules':[children(rule) for rule in key.findall('FilterRule')]}}
                else:value['Filter']={}
            elif len(item):value[item.tag]=children(item)
            elif item.tag in ('SimplePrefix','PartitionedPrefix'):value[item.tag]={}
            else:value[item.tag]=item.text or ''
        return value
    if root.tag=='LoggingEnabled':data['LoggingEnabled']=children(root)
    else:data.update(children(root))
    return data


class ExtensionClient(S3Client):
    """Keep normal data operations in the actual official SDK and opt in only logging RPCs to the named public-hook protocol adapter."""
    def logging(self,operation,values):
        name=values['Bucket']
        if name not in self.claims and name not in self.r.generated:raise RuntimeError('Ceph logging request escaped owned/generated namespace')
        inputs={key:value for key,value in values.items() if key!='Bucket'}
        return captured(self.raw('api.ceph.'+operation,name,input=inputs,capture_response_body=True))
    def head_object(self,**values):return response(self.call('head_version',values['Bucket'],key=values['Key']))
    def delete_object(self,**values):return response(self.call('delete',values['Bucket'],key=values['Key']))
    def delete_bucket(self,**values):
        name=values['Bucket']
        if name not in self.claims:raise RuntimeError('Cannot delete a bucket without this actual creator claim')
        scope=self.claims[name];actual=scope.raw('bucket_delete');result=response(actual)
        # A confirmed successful DELETE ends this creator's ownership before
        # looking again. A subsequent HEAD may observe another actor's bucket;
        # retain no cleanup claim capable of deleting that replacement.
        scope.created=False;scope.cleanup_completed=True;self.claims.pop(name);self.r.scopes.remove((self,scope))
        gone=scope.raw('bucket_head')
        if gone.get('ok') or (gone.get('error_kind'),gone.get('status_code'))!=('not_found',404):raise RuntimeError('Source bucket deletion not confirmed; refusing ownership transfer/recreation')
        return result
    def put_bucket_logging(self,**values):return self.logging('PutBucketLogging',values)
    def get_bucket_logging(self,**values):return self.logging('GetBucketLogging',values)
    def post_bucket_logging(self,**values):return self.logging('PostBucketLogging',values)


class ExtensionRuntime(Runtime):
    """Require explicit protocol-adapter opt-in and genuine bridge capability before original extension sources create owned UUID buckets."""
    def __enter__(self):
        if self.p.ctx.workload.get('ceph_logging_wire_extensions') is not True:raise CaseNotRun('Source requires explicit Ceph logging wire extensions; stock AWS SDK has no such fields/POST operation','workload.ceph_logging_wire_extensions')
        if 'ceph_logging_wire_extensions' not in self.p.ctx.worker.capabilities:raise CaseNotRun('Actual worker lacks official SDK public-hook Ceph logging adapter','ceph_logging_wire_extensions')
        user=self.p.ctx.workload.get('identities',{}).get('main',{}).get('user_id')
        if not isinstance(user,str) or not user:raise CaseNotRun('Original policy requires independently configured main user ID','workload.identities.main.user_id')
        if self.mode=='account':
            super().__enter__();self.main=ExtensionClient(self,self.p);return self
        self.main=ExtensionClient(self,self.p);self.root_client=None;self.token=_ACTIVE.set(self)
        return self

    def root(self):
        if self.root_client is None:self.root_client=ExtensionClient(self,self.stack.enter_context(configured(self.p,'iam_root')))
        return self.root_client
