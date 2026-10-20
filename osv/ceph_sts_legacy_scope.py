"""Own fresh legacy IAM-admin roles without substituting account-root identities or repairing source helper errors."""
import uuid
from .ceph_iam_scope import authorized,rpc,checked,error
from .suites import CaseNotRun


def original_helper(result):
    """Preserve upstream's erroneous ClientError response['Code'] access for real service errors."""
    if not result.get('ok') and result.get('status_code') is not None:raise KeyError('Code')
    checked(result);return result


class LegacyRole:
    """Track only a successful exclusive fresh role and its own unique inline policy, checking RoleId before cleanup."""
    def __init__(self,p):
        self.p=p;self.name='osvrole'+uuid.uuid4().hex;self.policy='osvpolicy'+uuid.uuid4().hex;self.session='osvsession'+uuid.uuid4().hex;self.uid=None;self.attempted=False
    def __enter__(self):
        authorized(self.p)
        if 'identity_iam' not in self.p.ctx.worker.capabilities:raise CaseNotRun('Legacy source requires actual configured IAM admin','identity_iam')
        return self
    def create(self,trust):
        result=rpc(self.p,'CreateRole','iam',Path='/',RoleName=self.name,AssumeRolePolicyDocument=trust,Tags=[])
        if result.get('ok'):
            self.uid=result['data']['Role']['RoleId']
            if not isinstance(self.uid,str) or not self.uid:self.uid=None;raise AssertionError('Fresh role response has no immutable RoleId ownership proof')
        original_helper(result);role=result['data']['Role'];
        from .scenarios import assert_equal
        assert_equal(role['Arn'],'arn:aws:iam:::role/'+self.name)
        return role
    def put(self,document):
        self.attempted=True;return original_helper(rpc(self.p,'PutRolePolicy','iam',RoleName=self.name,PolicyName=self.policy,PolicyDocument=document))
    def __exit__(self,typ,value,tb):
        if self.uid is None:return False
        current=rpc(self.p,'GetRole','iam',RoleName=self.name)
        if not current.get('ok'):error(current,'NoSuchEntity');return False
        if checked(current)['Role']['RoleId']!=self.uid:raise RuntimeError('Legacy role immutable ownership changed; refusing cleanup')
        if self.attempted:
            result=rpc(self.p,'DeleteRolePolicy','iam',RoleName=self.name,PolicyName=self.policy)
            if not result.get('ok'):error(result,'NoSuchEntity')
        result=rpc(self.p,'DeleteRole','iam',RoleName=self.name)
        if not result.get('ok'):error(result,'NoSuchEntity')
        return False
