"""Clean policies and memberships only on fresh immutable IAM identities belonging to this case."""
from dataclasses import replace
from contextlib import contextmanager
import time
import os
from .ceph_iam_scope import IamScope,rpc,checked
from .worker import Worker
from .suites import Probe


class PolicyScope(IamScope):
    """Bind each policy/membership mutation to fresh IDs, then remove dependencies before deleting owned entities."""
    def __init__(self,p,identity='iam_root'):super().__init__(p,identity);self.dependencies=[]
    def owned(self,kind,name):
        return next(((kind,uid) for (k,uid),n in reversed(list(self.resources.items())) if k==kind and n.lower()==name.lower()),None)
    def raw(self,operation,**values):
        dependencies=[]
        for kind in ('User','Group','Role'):
            if kind+'Name' not in values:continue
            owned=self.owned(kind,values[kind+'Name'])
            if owned is None:continue
            if operation in ('Put'+kind+'Policy','Attach'+kind+'Policy'):
                dependencies=[(owned,'Delete'+kind+'Policy' if operation.startswith('Put') else 'Detach'+kind+'Policy',{'PolicyName':values['PolicyName']} if operation.startswith('Put') else {'PolicyArn':values['PolicyArn']})]
        if operation=='AddUserToGroup':
            group=self.owned('Group',values['GroupName']);user=self.owned('User',values['UserName'])
            if group and user:dependencies=[(group,'RemoveUserFromGroup',{'owned_user':user})]
        # Register before the SDK operation: a transport error can follow a
        # successful mutation; the pre-existing resource was proven fresh.
        self.dependencies.extend(dependencies)
        return super().raw(operation,**values)
    def exists(self,owned):
        kind,uid=owned;name=self.resources[owned];result=self.rpc('Get'+kind,**{kind+'Name':name})
        if not result.get('ok'):
            if result.get('error_code')=='NoSuchEntity':return False
            checked(result)
        return checked(result)[kind][kind+'Id']==uid
    def __exit__(self,typ,value,traceback):
        failures=[];seen=set()
        for owned,operation,values in reversed(self.dependencies):
            marker=(owned,operation,repr(values))
            if marker in seen:continue
            seen.add(marker)
            try:
                if not self.exists(owned):continue
                fields=dict(values)
                if 'owned_user' in fields:
                    user=fields.pop('owned_user')
                    if not self.exists(user):continue
                    fields['UserName']=self.resources[user]
                fields[owned[0]+'Name']=self.resources[owned];result=self.rpc(operation,**fields)
                if not result.get('ok') and result.get('error_code')!='NoSuchEntity':checked(result)
            except Exception as exc:failures.append(operation+': '+str(exc))
        try:super().__exit__(typ,value,traceback)
        except Exception as exc:failures.append(str(exc))
        if failures:raise RuntimeError('IAM dependency cleanup failed: '+'; '.join(failures)) from value
        return False


@contextmanager
def credentials(p,key,iam=False):
    """Feed actual native CreateAccessKey/STS credentials straight into a new official SDK session."""
    env={'OSV_ACCESS_KEY':key['AccessKeyId'],'OSV_SECRET_KEY':key['SecretAccessKey'],'OSV_SESSION_TOKEN':key.get('SessionToken','')}
    if iam:env.update({'OSV_IAM_ACCESS_KEY':key['AccessKeyId'],'OSV_IAM_SECRET_KEY':key['SecretAccessKey'],'OSV_IAM_SESSION_TOKEN':key.get('SessionToken','')})
    target=dict(p.ctx.worker.target);target['env']={**target.get('env',{}),**env}
    worker=Worker(target,p.ctx.worker.timeout,secret_registry=p.ctx.worker.secret_registry)
    try:yield Probe(replace(p.ctx,worker=worker))
    finally:worker.close()


def retry(p,operation,codes,identity=None,**values):
    """Keep the source's ten actual attempts and linear 0..8 second waits, without retrying other failures."""
    for index in range(10):
        request={'op':operation,**values}
        if identity is not None:request['identity']=identity
        result=p.ctx.worker.call(request)
        if result.get('ok') or index==9 or result.get('error_code') not in codes:return result
        time.sleep(index)


def scoped_retry(scope,operation,codes,**values):
    """Retain a successful retry's actual owned entity even if later assertions fail."""
    for index in range(10):
        result=scope.raw(operation,**values)
        if result.get('ok') or index==9 or result.get('error_code') not in codes:return result
        time.sleep(index)


@contextmanager
def configured(p,identity='iam',consumer_iam=False):
    """Explicitly reuse the source's configured identity in another real SDK service, without falling back to main."""
    from .suites import CaseNotRun
    prefixes={'iam':'OSV_IAM_','iam_root':'OSV_IAM_ROOT_','iam_alt_root':'OSV_IAM_ALT_ROOT_','alt':'OSV_ALT_'}
    capability='identity_'+identity
    if capability not in p.ctx.worker.capabilities:raise CaseNotRun('Source configured identity is absent; refusing a main/admin fallback',capability)
    environment={**os.environ,**p.ctx.worker.target.get('env',{})};prefix=prefixes[identity]
    access=environment.get(prefix+'ACCESS_KEY');secret=environment.get(prefix+'SECRET_KEY')
    if not access or not secret:raise CaseNotRun('Configured source identity requires its actual credential pair',prefix+'ACCESS_KEY/SECRET_KEY')
    with credentials(p,{'AccessKeyId':access,'SecretAccessKey':secret,'SessionToken':environment.get(prefix+'SESSION_TOKEN','')},consumer_iam) as actual:yield actual
