"""Pinned tenant policy S3/IAM action vectors on the original admin and alternate credential chains."""
import random
import string
from .ceph_iam_inline import dedicated,NAMES
from .ceph_iam_scope import checked,error,authorized,rpc
from .ceph_iam_policy_scope import configured
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH,CaseNotRun


def operation(p,bucket,op,**values):return p.ctx.worker.call({'op':op,'bucket':bucket.name,**values})


def put(client,bucket,key,body):
    bucket.written_keys.add(key);client.ctx.body.write_bytes(body)
    return operation(client,bucket,'put_http',key=key,body_path=str(client.ctx.body))


def remove(client,bucket,key):return operation(client,bucket,'delete_version',key=key)


def fetch(client,bucket,key):
    client.ctx.output.unlink(missing_ok=True)
    return operation(client,bucket,'get_http',key=key,output_path=str(client.ctx.output))


def actions(p,kind,deny=False):
    authorized(p)
    if 'identity_iam' not in p.ctx.worker.capabilities:raise CaseNotRun('Source IAM admin is not explicitly configured','identity_iam')
    with configured(p,'alt') as alt,configured(p,'iam') as admin,dedicated(p,NAMES+('DenyAccessPolicy',)) as policy:
        owner=alt if deny or kind.startswith('conflict') else admin
        with BucketScope(owner) as bucket:
            upload=None;upload_owner=None
            try:
                resource='arn:aws:s3:::'+bucket.name
                if kind=='bucket':
                    if not deny:checked(put(admin,bucket,'foo',b'bar'))
                    document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:ListAllMyBuckets','s3:DeleteBucket'] if deny else ['s3:ListBucket','s3:DeleteBucket'],'Resource':'arn:aws:s3:::*' if deny else resource}}
                elif kind=='object':
                    if deny:checked(put(alt,bucket,'foo',b'bar'))
                    statement={'Effect':'Deny' if deny else 'Allow','Action':['s3:PutObject','s3:GetObject','s3:DeleteObject'],'Resource':resource+'/*'}
                    document={'Version':'2012-10-17','Statement':[statement,{'Effect':'Allow','Action':['s3:DeleteBucket'],'Resource':resource}] if deny else statement}
                elif kind=='multipart':document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:ListBucketMultipartUploads','s3:AbortMultipartUpload'],'Resource':'arn:aws:s3:::*'}}
                elif kind=='tagging':document={'Version':'2012-10-17','Statement':{'Effect':'Deny' if deny else 'Allow','Action':['s3:PutBucketTagging','s3:GetBucketTagging','s3:PutObjectTagging','s3:DeleteObjectTagging' if deny else 's3:GetObjectTagging'],'Resource':'arn:aws:s3:::*'}}
                elif kind=='conflict_statements':document={'Version':'2012-10-17','Statement':[{'Sid':'98AB54CG','Effect':'Allow','Action':'s3:ListBucket','Resource':resource},{'Sid':'98AB54CA','Effect':'Deny','Action':'s3:ListBucket','Resource':resource}]}
                else:document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CG','Effect':'Allow','Action':'s3:ListBucket','Resource':resource}}
                policy_name='DenyAccessPolicy' if deny or kind=='conflict_statements' else 'AllowAccessPolicy'
                policy('PutUserPolicy',policy_name,document=document,status=None if kind in ('object','tagging') and (deny or kind=='tagging') else 200)
                if kind=='bucket':
                    if deny:
                        error(alt.ctx.worker.call({'op':'api.ListBuckets','input':{}}),code='AccessDenied',status=403);error(operation(alt,bucket,'bucket_delete'),code='AccessDenied',status=403);policy('DeleteUserPolicy',policy_name,status=200);assert_equal(checked_result(operation(alt,bucket,'bucket_delete'))['status_code'],204)
                    else:
                        listed=checked(operation(alt,bucket,'api.ListObjects',input={}))['Contents'];assert_equal(any(row['Key']=='foo' for row in listed),True)
                        assert_equal(checked_result(remove(admin,bucket,'foo'))['status_code'],204);assert_equal(checked_result(operation(alt,bucket,'bucket_delete'))['status_code'],204)
                        # Upstream shadows bucket with each response dictionary;
                        # dict == dict['Name'] is always false. Preserve this defect.
                        for item in checked(admin.ctx.worker.call({'op':'api.ListBuckets','input':{}}))['Buckets']:
                            if item==item['Name']:raise AssertionError('deleted bucket is getting listed')
                        policy('DeleteUserPolicy',policy_name,status=200)
                elif kind=='object':
                    if deny:
                        error(put(alt,bucket,'foo',b''),code='AccessDenied',status=403);error(fetch(alt,bucket,'foo'),code='AccessDenied',status=403);error(remove(alt,bucket,'foo'),code='AccessDenied',status=403);policy('DeleteUserPolicy',policy_name,status=200)
                    else:
                        checked(put(alt,bucket,'foo',b'bar'));checked(fetch(alt,bucket,'foo'));assert_equal(alt.ctx.output.read_bytes().decode(),'bar');assert_equal(checked_result(remove(alt,bucket,'foo'))['status_code'],204);error(fetch(admin,bucket,'foo'),code='NoSuchKey',status=404);assert_equal(checked_result(operation(admin,bucket,'bucket_delete'))['status_code'],204);policy('DeleteUserPolicy',policy_name,status=200)
                elif kind=='multipart':
                    upload_owner=owner;upload=checked_result(operation(owner,bucket,'mp_create',key='mymultipart'))['upload_id'];chunk=''.join(random.choice(string.ascii_letters) for _ in range(1024)).encode();owner.ctx.body.write_bytes(chunk*(5*1024));part=checked_result(operation(owner,bucket,'mp_part',key='mymultipart',upload_id=upload,part_number=1,body_path=str(owner.ctx.body)));part['etag'].strip('"')
                    listing=operation(alt,bucket,'mp_list');aborted=lambda:operation(alt,bucket,'api.AbortMultipartUpload',input={'Key':'mymultipart','UploadId':upload})
                    if deny:error(listing,code='AccessDenied',status=403);error(aborted(),code='AccessDenied',status=403)
                    else:assert_equal(checked_result(listing)['status_code'],200);assert_equal(checked_result(aborted())['status_code'],204);upload=None
                    assert_equal(checked_result(operation(owner,bucket,'bucket_delete'))['status_code'],204);policy('DeleteUserPolicy',policy_name,status=200)
                elif kind=='tagging':
                    tags={'TagSet':[{'Key':'Hello','Value':'World'}]}
                    def tagging(op,key=None,write=False):
                        if key is not None:return operation(alt,bucket,{'PutObjectTagging':'tags_put','GetObjectTagging':'tags_get','DeleteObjectTagging':'tags_delete'}[op],key=key,**({'tags':[{'key':'Hello','value':'World'}]} if write else {}))
                        return operation(alt,bucket,'api.'+op,input={'Tagging':tags} if write else {})
                    if deny:
                        error(tagging('PutBucketTagging',write=True),code='AccessDenied',status=403);error(tagging('GetBucketTagging'),code='AccessDenied',status=403)
                    else:
                        assert_equal(checked_result(tagging('PutBucketTagging',write=True))['status_code'],200);result=checked_result(tagging('GetBucketTagging'));assert_equal(result['status_code'],200);assert_equal(result['data']['TagSet'][0]['Key'],'Hello');assert_equal(result['data']['TagSet'][0]['Value'],'World')
                    assert_equal(checked_result(put(owner,bucket,'obj',b'obj_body'))['status_code'],200)
                    if deny:error(tagging('PutObjectTagging','obj',True),code='AccessDenied',status=403);error(tagging('DeleteObjectTagging','obj'),code='AccessDenied',status=403)
                    else:
                        assert_equal(checked_result(tagging('PutObjectTagging','obj',True))['status_code'],200);result=checked_result(tagging('GetObjectTagging','obj'));assert_equal(result['status_code'],200);assert_equal([{'Key':row['key'],'Value':row['value']} for row in result['tags']],tags['TagSet'])
                    assert_equal(checked_result(remove(owner,bucket,'obj'))['status_code'],204);assert_equal(checked_result(operation(owner,bucket,'bucket_delete'))['status_code'],204);policy('DeleteUserPolicy',policy_name,status=200)
                else:
                    if kind=='conflict_policies':policy('PutUserPolicy','DenyAccessPolicy',document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CGZ','Effect':'Deny','Action':'s3:ListBucket','Resource':resource}},status=200)
                    error(operation(alt,bucket,'api.ListObjects',input={}),code='AccessDenied',status=403);policy('DeleteUserPolicy',policy_name,status=200)
                    if kind=='conflict_policies':policy('DeleteUserPolicy','DenyAccessPolicy',status=200)
            finally:
                # Remove any self-denial before owner S3 cleanup; the admin IAM
                # credential is never replaced with the denied alternate user.
                policy.cleanup()
                if upload is not None:
                    result=operation(upload_owner,bucket,'mp_abort',key='mymultipart',upload_id=upload)
                    if not result.get('ok') and result.get('error_code') not in ('NoSuchUpload','NoSuchBucket'):checked_result(result)


def checked_result(result):checked(result);return result


def allow_iam(p):
    with dedicated(p) as policy,configured(p,'alt',True) as alt:
        name=p.ctx.workload['iam_alt_user_name'];document={'Version':'2012-10-17','Statement':{'Sid':'98AB54CGA','Effect':'Allow','Action':['iam:PutUserPolicy','iam:GetUserPolicy','iam:ListUserPolicies','iam:DeleteUserPolicy'],'Resource':'arn:aws:iam:::user/'+name}}
        policy('PutUserPolicy','AllowAccessPolicy',document=document,status=200)
        for op,values in (('GetUserPolicy',{'PolicyName':'AllowAccessPolicy'}),('ListUserPolicies',{}),('DeleteUserPolicy',{'PolicyName':'AllowAccessPolicy'})):assert_equal(checked_result(rpc(alt,op,'iam',UserName=name,**values))['status_code'],200)


SPECS=[]
for kind in ('bucket','object','multipart','tagging'):
    for deny in (False,True):SPECS.append(('test_'+('deny' if deny else 'allow')+'_'+kind+'_actions_in_user_policy',lambda p,k=kind,d=deny:actions(p,k,d)))
SPECS.extend((('test_verify_conflicting_user_policy_statements',lambda p:actions(p,'conflict_statements')),('test_verify_conflicting_user_policies',lambda p:actions(p,'conflict_policies')),('test_verify_allow_iam_actions',allow_iam)))
BASE='identity_iam identity_alt api.iam.GetUser api.iam.GetUserPolicy api.iam.ListUserPolicies api.iam.PutUserPolicy api.iam.DeleteUserPolicy api.ListBuckets api.ListObjects put_http get_http delete_version '+SCOPE_OPERATIONS
def operations(reference):
    return BASE+(' api.AbortMultipartUpload mp_list mp_create mp_part mp_abort' if '_multipart_' in reference else ' api.PutBucketTagging api.GetBucketTagging tags_put tags_get tags_delete' if '_tagging_' in reference else '')
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_'),'Ceph租户IAM策略原始S3/IAM权限向量','原IAM admin/S3 IAM admin/独立alt真实身份、原固定策略/正文/5MiB multipart/标签/所有403与状态断言；保留上游ListBuckets变量遮蔽缺陷，先移除自拒绝策略再按原Owner清理',operations(ref),execute,ref,CEPH.replace('test_s3.py','test_iam.py')) for ref,execute in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_tenant_actions',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
