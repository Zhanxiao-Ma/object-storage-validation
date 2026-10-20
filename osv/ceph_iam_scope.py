"""Isolate real IAM mutations and paginate native SDK results without rewriting their semantics."""
from contextlib import AbstractContextManager
import uuid
from .suites import CaseNotRun
from .scenarios import assert_equal


def rpc(p, operation, identity='iam_root', **values):
    return p.ctx.worker.call({'op':'api.iam.'+operation,'identity':identity,'input':values})


def checked(result):
    # Never include full results: CreateAccessKey can carry fresh credentials.
    if not result.get('ok'):raise AssertionError('IAM request failed: status='+str(result.get('status_code'))+' code='+str(result.get('error_code')))
    return result.get('data',{})


def error(result, code=None, status=None):
    assert_equal(result.get('ok'),False)
    if code is not None:assert_equal(result.get('error_code'),code)
    if status is not None:assert_equal(result.get('status_code'),status)


def pages(p, operation, field, identity='iam_root', **values):
    rows=[];seen=set()
    while True:
        data=checked(rpc(p,operation,identity,**values));rows.extend(data.get(field,[]))
        truncated=data.get('IsTruncated',False)
        assert_equal(isinstance(truncated,bool),True)
        if not truncated:return rows
        marker=data.get('Marker')
        if not isinstance(marker,str) or not marker or marker in seen:raise AssertionError('IAM pagination missing or repeated Marker')
        seen.add(marker);values={**values,'Marker':marker}


def authorized(p):
    if p.ctx.workload.get('iam_mutation_authorized') is not True:raise CaseNotRun('IAM mutations require explicit isolated-account authorization','workload.iam_mutation_authorized')


class IamScope(AbstractContextManager):
    """Track successful creations by returned immutable ID and delete only those actual owned resources."""
    def __init__(self,p,identity='iam_root'):
        if identity not in ('iam_root','iam_alt_root'):raise ValueError('Fresh account scope requires an explicit root identity')
        self.p=p;self.identity=identity;self.prefix='osv'+uuid.uuid4().hex[:18];self.path='/'+self.prefix+'/';self.resources={};self.keys=[]
    def __enter__(self):
        authorized(self.p)
        if 'identity_'+self.identity not in self.p.ctx.worker.capabilities:raise CaseNotRun('IAM root requires explicit IAM endpoint and corresponding root credentials','identity_'+self.identity)
        root=checked(self.rpc('GetUser'))['User']
        if not root['Arn'].endswith(':root'):raise CaseNotRun('Ceph iam_root fixture requires actual account root GetUser ARN','iam_root credentials')
        return self
    def name(self,suffix):return self.prefix+suffix
    def rpc(self,operation,**values):return rpc(self.p,operation,self.identity,**values)
    def raw(self,operation,**values):
        result=self.rpc(operation,**values)
        if result.get('ok'):
            data=result.get('data',{})
            for kind in ('User','Role','Group'):
                if operation=='Create'+kind:
                    item=data[kind];uid=item.get(kind+'Id');returned=item.get(kind+'Name');requested=values[kind+'Name']
                    if not isinstance(uid,str) or not uid or not isinstance(returned,str) or returned.lower()!=requested.lower():raise AssertionError('Fresh IAM creation response cannot prove requested resource ownership')
                    previous=self.resources.get((kind,uid))
                    if previous is not None and previous.lower()!=requested.lower():raise AssertionError('IAM creation returned a duplicate immutable ID for a different resource; refusing ambiguous ownership')
                    self.resources[(kind,uid)]=requested
            if operation=='UpdateUser' and 'NewUserName' in values:
                for key,name in list(self.resources.items()):
                    if key[0]=='User' and name.lower()==values['UserName'].lower():self.resources[key]=values['NewUserName']
            if operation=='UpdateGroup' and 'NewGroupName' in values:
                for key,name in list(self.resources.items()):
                    if key[0]=='Group' and name.lower()==values['GroupName'].lower():self.resources[key]=values['NewGroupName']
            if operation=='CreateAccessKey':
                for (kind,uid),name in reversed(list(self.resources.items())):
                    if kind=='User' and name.lower()==values['UserName'].lower():
                        self.keys.append((name,uid,data['AccessKey']['AccessKeyId']));break
        return result
    def call(self,operation,**values):return checked(self.raw(operation,**values))
    def create(self,kind,name,path=None,**values):
        if path is not None:values['Path']=path
        return self.call('Create'+kind,**{kind+'Name':name},**values)[kind]
    def __exit__(self,typ,value,traceback):
        failures=[]
        for name,uid,keyid in reversed(self.keys):
            current=self.rpc('GetUser',UserName=name)
            if not current.get('ok'):
                if current.get('error_code')!='NoSuchEntity':failures.append('GetUser before key cleanup')
                continue
            if checked(current)['User']['UserId']!=uid:continue
            result=self.rpc('DeleteAccessKey',UserName=name,AccessKeyId=keyid)
            if not result.get('ok') and result.get('error_code')!='NoSuchEntity':failures.append('DeleteAccessKey')
        for (kind,uid),name in reversed(list(self.resources.items())):
            current=self.rpc('Get'+kind,**{kind+'Name':name})
            if not current.get('ok'):
                if current.get('error_code')!='NoSuchEntity':failures.append('Get'+kind)
                continue
            if checked(current)[kind][kind+'Id']!=uid:continue
            result=self.rpc('Delete'+kind,**{kind+'Name':name})
            if not result.get('ok') and result.get('error_code')!='NoSuchEntity':failures.append('Delete'+kind)
        if failures:raise RuntimeError('IAM owned-resource cleanup failed: '+','.join(failures)) from value
        return False
