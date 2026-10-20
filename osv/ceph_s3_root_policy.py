"""Preserve pinned Ceph root policy recovery and ConfirmRemoveSelf source assertions."""
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_iam_scope import IamScope
from .ceph_iam_policy_scope import configured,credentials
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def deny_self(p,confirmed):
    """Deny policy management to a fresh IAM user; expect root recovery unless original explicit ConfirmRemoveSelf header also locks out root."""
    with IamScope(p) as iam,configured(p,'iam_root') as root:
        with BucketScope(root) as bucket:
            document=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Deny','Principal':'*','Action':['s3:PutBucketPolicy','s3:GetBucketPolicy','s3:DeleteBucketPolicy'],'Resource':['arn:aws:s3:::'+bucket.name,'arn:aws:s3:::'+bucket.name+'/*']}]})
            api(bucket,'PutBucketPolicy',Policy=document,**({'ConfirmRemoveSelfBucketAccess':True} if confirmed else {}))
            user=iam.create('User',iam.name('policy-user'),iam.path)
            key=iam.call('CreateAccessKey',UserName=user['UserName'])['AccessKey']
            with credentials(p,key) as fresh:
                for operation in ('GetBucketPolicy','DeleteBucketPolicy','PutBucketPolicy'):
                    result=fresh.ctx.worker.call({'op':'api.'+operation,'bucket':bucket.name,'input':{'Policy':document} if operation=='PutBucketPolicy' else {}})
                    rejected(result,403)
            if confirmed:
                for operation in ('GetBucketPolicy','DeleteBucketPolicy','PutBucketPolicy'):
                    rejected(raw(bucket,operation,**({'Policy':document} if operation=='PutBucketPolicy' else {})),403)
            else:
                assert_equal(api(bucket,'GetBucketPolicy')['data']['Policy'],document)
                api(bucket,'DeleteBucketPolicy');api(bucket,'PutBucketPolicy',Policy=document)
            # Original Deny covers only policy operations. Cleanup deletes the
            # actual empty, UUID-owned bucket; it never assumes root can remove
            # the confirmed policy or touches an existing account bucket.
    return {'root_fixture':'actual_iam_root','fresh_user_credentials':'actual_CreateAccessKey','confirm_remove_self_input':confirmed}


OPS=SCOPE_OPERATIONS+' identity_iam_root api.iam.GetUser api.iam.CreateUser api.iam.CreateAccessKey api.iam.DeleteAccessKey api.iam.DeleteUser api.PutBucketPolicy api.GetBucketPolicy api.DeleteBucketPolicy'
CASES=tuple(case('ceph_root_policy_deny_self'+('_confirm' if confirmed else ''),'Ceph root policy self-denial','保留源策略原文、fresh IAM用户实际凭据链、三项403及root原文恢复/ConfirmRemoveSelf三项403断言；仅独立UUID桶和实际UserId清理',OPS,lambda p,c=confirmed:deny_self(p,c),'test_bucket_policy_deny_self_denied_policy'+('_confirm_header' if confirmed else ''),CEPH) for confirmed in (False,True))


def register(registry):
    registry.add_suite(Suite('ceph_s3_root_policy',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
