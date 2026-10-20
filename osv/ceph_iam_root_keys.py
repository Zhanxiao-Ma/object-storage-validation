"""Preserve root-key vectors while touching only keys demonstrably absent before this case."""
from .ceph_iam_scope import IamScope,rpc,checked,error,pages
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun

FAKE='abcdefghijklmnopqrstu'


def current_key(p,mode):
    with IamScope(p):
        before={row['AccessKeyId'] for row in pages(p,'ListAccessKeys','AccessKeyMetadata')}
        if FAKE in before and mode in ('update','delete'):raise CaseNotRun('Source fixed negative key exists on root; refusing to touch it','isolated root without source negative AccessKeyId')
        keyid=None
        try:
            if mode in ('update','delete'):error(rpc(p,'UpdateAccessKey' if mode=='update' else 'DeleteAccessKey',AccessKeyId=FAKE,**({'Status':'Active'} if mode=='update' else {})),'NoSuchEntity')
            key=checked(rpc(p,'CreateAccessKey'))['AccessKey'];keyid=key['AccessKeyId']
            if keyid in before:raise AssertionError('CreateAccessKey returned a pre-existing root key; refusing to mutate it')
            if mode in ('create','update'):assert_equal(bool(keyid),True)
            if mode=='create':assert_equal(bool(key['SecretAccessKey']),True);assert_equal(key['Status'],'Active');assert_equal(key['CreateDate']>0,True)
            elif mode=='update':
                for status in ('Active','Inactive'):checked(rpc(p,'UpdateAccessKey',AccessKeyId=keyid,Status=status))
                found=False
                for row in pages(p,'ListAccessKeys','AccessKeyMetadata'):
                    if row['AccessKeyId']==keyid:assert_equal(row['Status'],'Inactive');found=True;break
                assert_equal(found,True)
            else:
                checked(rpc(p,'DeleteAccessKey',AccessKeyId=keyid));error(rpc(p,'DeleteAccessKey',AccessKeyId=keyid),'NoSuchEntity')
                for row in pages(p,'ListAccessKeys','AccessKeyMetadata'):assert_equal(row['AccessKeyId']!=keyid,True)
        finally:
            if keyid is not None and keyid not in before:
                result=rpc(p,'DeleteAccessKey',AccessKeyId=keyid)
                if not result.get('ok'):error(result,'NoSuchEntity')


OPS='identity_iam_root '+' '.join('api.iam.'+op for op in ('GetUser','ListAccessKeys','CreateAccessKey','UpdateAccessKey','DeleteAccessKey'))
CASES=tuple(case('ceph_iam_account_current_user_access_key_'+mode,'Ceph IAM root新建密钥原始向量','保留省略UserName、真实新key到更新/分页/删除的链路；先列既有root key，只操作不在既有集合中的真实新ID，所有失败finally清理且绝不触及预存key',OPS,lambda p,m=mode:current_key(p,m),'test_account_current_user_access_key_'+mode,CEPH.replace('test_s3.py','test_iam.py')) for mode in ('create','update','delete'))


def register(registry):registry.add_suite(Suite('ceph_iam_root_keys',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
