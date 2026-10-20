"""Preserve pinned Ceph policy-condition inputs and source oracles through real SDK identities."""
from contextlib import contextmanager
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_sse_customer import HEADERS as CUSTOMER_HEADERS
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH

ALGORITHM="x-amz-server-side-encryption"
KMS_KEY="x-amz-server-side-encryption-aws-kms-key-id"

def statement(bucket,action,condition=None,effect="Allow",principal=None):
    result={"Action":"s3:"+action,"Principal":{"AWS":"*"} if principal is None else principal,"Effect":effect,"Resource":"arn:aws:s3:::"+bucket.name+"/*"}
    if condition is not None:result["Condition"]=condition
    return result

@contextmanager
def policy(bucket,*statements):
    """Remove the actual owned bucket's policy before object cleanup, including failed assertions or SDK parse failures."""
    try:
        api(bucket,"PutBucketPolicy",Policy=json.dumps({"Version":"2012-10-17","Statement":list(statements)}))
        yield
    finally:
        result=raw(bucket,"DeleteBucketPolicy")
        if not result["ok"] and result.get("status_code")!=404:raise RuntimeError("policy cleanup failed: "+str(result))

def put(bucket,key,body=b"",headers=None,identity=None):
    bucket.probe.ctx.body.write_bytes(body)
    options={"key":key,"body_path":str(bucket.probe.ctx.body)}
    if headers is not None:options["wire_headers"]=headers
    if identity is not None:options["identity"]=identity
    return bucket.raw("put_http",**options)

def customer(p,mode):
    """Keep raw public SSE-C key/MD5, AES192 without key/body, source denied-write HTTP 403 and successful AES256 write."""
    condition={"Null":{"s3:x-amz-server-side-encryption-customer-algorithm":"true"}} if mode=="required" else {"StringNotEquals":{"s3:x-amz-server-side-encryption-customer-algorithm":"AES256"}}
    with BucketScope(p) as bucket,policy(bucket,statement(bucket,"PutObject",condition,"Deny")):
        rejected(put(bucket,"foo",b"bar" if mode=="required" else b"",None if mode=="required" else {"x-amz-server-side-encryption-customer-algorithm":"AES192"}),403)
        require_ok(put(bucket,"foo",b"bar",dict(CUSTOMER_HEADERS)))

def encryption(p,mode):
    """Retain five source SSE-S3/KMS policies, distinct absent/invalid headers, literal bodies, KMS fallback or configured-key skip, and exact response encryption headers."""
    kms=p.ctx.workload.get("kms_key_id")
    if mode=="kms_noenc" and kms is None:raise CaseNotRun("Ceph source skips KMS policy success without main kms_keyid","workload.kms_key_id")
    with BucketScope(p) as bucket:
        wanted="aws:kms" if mode.startswith("kms_") else "AES256"
        conditions=([{ "Null":{"s3:x-amz-server-side-encryption":"true"}}] if mode=="s3_noenc" else [{"StringNotEquals":{"s3:x-amz-server-side-encryption":wanted}}])
        if mode in ("s3_kms","kms_noenc","kms_s3"):conditions.append({"Null":{"s3:x-amz-server-side-encryption":"true"}})
        with policy(bucket,*(statement(bucket,"PutObject",condition,"Deny") for condition in conditions)):
            if mode=="kms_noenc":
                result=require_ok(put(bucket,"testobj",headers={ALGORITHM:"aws:kms",KMS_KEY:kms}))
                assert_equal(result["http_headers"][ALGORITHM],"aws:kms");assert_equal(result["http_headers"][KMS_KEY],kms)
                rejected(put(bucket,"unicorn",b"unicorn"),403)
            elif mode in ("s3_noenc","s3_incorrect_algo_sse_s3"):
                rejected(put(bucket,"testobj",b"testobj",None if mode=="s3_noenc" else {ALGORITHM:"AES192"}),403)
                result=require_ok(put(bucket,"testobj",headers={ALGORITHM:"AES256"}))
                assert_equal(result["http_headers"][ALGORITHM],"AES256")
            else:
                headers={ALGORITHM:"AES256"} if mode=="kms_s3" else {ALGORITHM:"aws:kms",KMS_KEY:kms if kms is not None else "fool-me-twice"}
                rejected(put(bucket,"testobj",b"testobj",headers),403)

PUBLIC=[{"key":"security","value":"public"},{"key":"foo","value":"bar"}]
PRIVATE=[{"key":"security","value":"private"}]
INVALID=[{"key":"security1","value":"public"}]

def tag_existing(p,mode):
    """Keep all three original key/body pairs, original ordered tags, alternate identity and the upstream GetACL case's unexpected GetObjectTagging negative calls."""
    with BucketScope(p) as bucket:
        for key in ("publictag","privatetag","invalidtag"):bucket.put(key,key.encode())
        action={"get":"GetObject","get_tagging":"GetObjectTagging","put_tagging":"PutObjectTagging","get_acl":"GetObjectAcl"}[mode]
        with policy(bucket,statement(bucket,action,{"StringEquals":{"s3:ExistingObjectTag/security":"public"}})):
            for key,tags in (("publictag",PUBLIC),("privatetag",PRIVATE)) + (() if mode=="put_tagging" else (("invalidtag",INVALID),)):
                assert_equal(bucket.call("tags_put",key=key,tags=tags)["status_code"],200)
            if mode=="put_tagging":
                assert_equal(bucket.call("tags_put",key="publictag",tags=PUBLIC,identity="alt")["status_code"],200)
                rejected(bucket.raw("tags_put",key="privatetag",tags=PUBLIC,identity="alt"),403)
                assert_equal(bucket.call("tags_put",key="publictag",tags=PRIVATE,identity="alt")["status_code"],200)
                rejected(bucket.raw("tags_put",key="publictag",tags=PUBLIC,identity="alt"),403)
                return
            def request(key,operation=None):
                operation=operation or mode
                if operation=="get":return bucket.raw("get_http",key=key,identity="alt",output_path=str(p.ctx.output))
                if operation=="get_acl":return bucket.raw("api.GetObjectAcl",input={"Key":key},identity="alt")
                return bucket.raw("tags_get",key=key,identity="alt")
            assert_equal(require_ok(request("publictag"))["status_code"],200)
            if mode!="get":rejected(request("publictag","get"),403)
            # Preserve the source's apparent GetACL/GetTagging copy-paste defect.
            for key in ("privatetag","invalidtag"):rejected(request(key,"get_tagging" if mode=="get_acl" else mode),403)

def request_tag(p):
    """Preserve fails_on_rgw and the source defect: its hook is installed on main while both writes use alt, so the second write still carries no tag header and is expected to succeed."""
    with BucketScope(p) as bucket,policy(bucket,statement(bucket,"PutObject",{"StringEquals":{"s3:RequestObjectTag/security":"public"}})):
        rejected(put(bucket,"testobj",b"testobj",identity="alt"),403)
        # Source attaches x-amz-tagging only to the unused main client.
        require_ok(put(bucket,"testobj",b"testobj",identity="alt"))

def referer(p):
    """Retain owner identity, StringLikeIfExists and all three Referer strings, including the source's owner-denial oracle and its final GetBucketPolicy call."""
    with BucketScope(p) as bucket:
        bucket.put("foo",b"")
        item=statement(bucket,"GetObject",{"StringLikeIfExists":{"aws:Referer":"http://www.example.com/*"}},principal="*")
        item["Sid"]="Allow Public Access to All Objects"
        with policy(bucket,item):
            for value in ("http://www.example.com/","http://www.example.com/index.html","http://example.com"):
                response=bucket.raw("get_http",key="foo",output_path=str(p.ctx.output),wire_headers={"referer":value})
                if value=="http://example.com":rejected(response,403)
                else:assert_equal(require_ok(response)["status_code"],200)
            api(bucket,"GetBucketPolicy")

def acl_condition(p):
    """Keep public wildcard Deny plus unconditional PutObject allow, exact source body/key strings, alternate credentials and public-read denial."""
    with BucketScope(p) as bucket,policy(bucket,statement(bucket,"PutObject"),statement(bucket,"PutObject",{"StringLike":{"s3:x-amz-acl":"public*"}},"Deny")):
        assert_equal(require_ok(put(bucket,"private-key",b"private-key",identity="alt"))["status_code"],200)
        rejected(put(bucket,"public-key",b"public-key",{"x-amz-acl":"public-read"},"alt"),403)

def copy_condition(p,metadata):
    """Use two independently owned buckets, actual alternate CopyObject/GetObject, literal public/private payloads, source prefix or metadata-directive conditions and final forbidden copy."""
    with BucketScope(p) as source,BucketScope(p) as destination:
        for key in (("public/foo","public/bar") if metadata else ("public/foo","public/bar","private/foo")):source.put(key,key.encode())
        condition={"StringEquals":{"s3:x-amz-metadata-directive":"COPY"}} if metadata else {"StringLike":{"s3:x-amz-copy-source":source.name+"/public/*"}}
        with policy(source,statement(source,"GetObject")),policy(destination,statement(destination,"PutObject",condition)):
            for key,target in (("public/foo","new_foo"),) if metadata else (("public/foo","new_foo"),("public/bar","new_foo2")):
                options={"wire_headers":{"x-amz-metadata-directive":"COPY"}} if metadata else {}
                destination.call("copy",key=target,source_bucket=source.name,source_key=key,identity="alt",**options)
                destination.call("get_http",key=target,output_path=str(p.ctx.output),identity="alt");assert_equal(p.ctx.output.read_bytes(),key.encode())
            options={"wire_headers":{"x-amz-meta-foo":"bar","x-amz-metadata-directive":None}} if metadata else {}
            rejected(destination.raw("copy",key="new_foo2",source_bucket=source.name,source_key="public/bar" if metadata else "private/foo",identity="alt",**options),403)

def grant_condition(p):
    """Preserve uploader ownership and exact id= grant condition; compare first raw ACL grant IDs to independently configured main/alt IDs, never inferred response values."""
    identities=p.ctx.workload.get("identities",{})
    main=identities.get("main",{}).get("canonical_id");alt=identities.get("alt",{}).get("canonical_id")
    if not main or not alt:raise CaseNotRun("Ceph source needs main and alternate configured user IDs","workload.identities.main.canonical_id","workload.identities.alt.canonical_id")
    with BucketScope(p) as first,BucketScope(p) as second:
        grant="id="+main
        with policy(first,statement(first,"PutObject",{"StringEquals":{"s3:x-amz-grant-full-control":grant}})),policy(second,statement(second,"PutObject")):
            assert_equal(require_ok(put(first,"key1",b"key1",{"x-amz-grant-full-control":grant},"alt"))["status_code"],200)
            assert_equal(require_ok(put(second,"key2",b"key2",identity="alt"))["status_code"],200)
            first_acl=api(first,"GetObjectAcl",Key="key1")
            rejected(raw(second,"GetObjectAcl",Key="key2"),403)
            second_acl=require_ok(second.raw("api.GetObjectAcl",input={"Key":"key2"},identity="alt"))
            assert_equal(first_acl["data"]["Grants"][0]["Grantee"]["ID"],main)
            assert_equal(second_acl["data"]["Grants"][0]["Grantee"]["ID"],alt)


def notprincipal(p):
    """Preserve literal tenant ARN and unsupported Allow/NotPrincipal source expectation: HTTP 400 with either InvalidArgument or MalformedPolicy."""
    with BucketScope(p) as bucket:
        document={"Version":"2012-10-17","Statement":[{"Effect":"Allow","NotPrincipal":{"AWS":"arn:aws:iam::s3tenant1:root"},"Action":"s3:ListBucket","Resource":["arn:aws:s3:::"+bucket.name,"arn:aws:s3:::"+bucket.name+"/*"]}]}
        try:
            result=raw(bucket,"PutBucketPolicy",Policy=json.dumps(document));rejected(result,400)
            if result.get("error_code") not in ("InvalidArgument","MalformedPolicy"):raise AssertionError("source permits only InvalidArgument or MalformedPolicy: "+str(result))
        finally:
            result=raw(bucket,"DeleteBucketPolicy")
            if not result["ok"] and result.get("status_code")!=404:raise RuntimeError("policy cleanup failed: "+str(result))

def head_prefix(p):
    """Keep no initialized objects, bucket-ARN ListBucket permission with public/* prefix, alternate HEAD public/object 404 and private/object 403."""
    with BucketScope(p) as bucket:
        item=statement(bucket,"ListBucket",{"StringLike":{"s3:prefix":"public/*"}});item["Resource"]="arn:aws:s3:::"+bucket.name
        with policy(bucket,item):
            rejected(bucket.raw("head_http",key="public/object",identity="alt"),404)
            rejected(bucket.raw("head_http",key="private/object",identity="alt"),403)

def multipart(p):
    """Keep original rejection/success oracles while aborting every SDK-produced upload ID in finally, including unexpectedly authorized creation."""
    def client_error(response):
        if response.get("ok") or response.get("error_kind") not in ("sdk_error","not_found") or not isinstance(response.get("status_code"),int) or not 400<=response["status_code"]<600:raise AssertionError("expected original service ClientError: "+str(response))
    with BucketScope(p) as bucket:
        uploads=[]
        def create():
            response=bucket.raw("mp_create",key="mpobj",identity="alt")
            if response.get("ok"):
                upload=response.get("upload_id")
                if not isinstance(upload,str) or not upload:raise RuntimeError("successful multipart creation omitted its upload ID; cannot identify pending upload for cleanup")
                uploads.append(upload)
            return response
        try:
            client_error(create())
            item=statement(bucket,"PutObject");item["Resource"]="arn:aws:s3:::"+bucket.name
            with policy(bucket,item):
                client_error(create())
                item["Resource"]="arn:aws:s3:::"+bucket.name+"/mpobj"
                api(bucket,"PutBucketPolicy",Policy=json.dumps({"Version":"2012-10-17","Statement":[item]}))
                require_ok(create())
        finally:
            failures=[]
            for upload in uploads:
                try:
                    result=bucket.raw("mp_abort",key="mpobj",identity="alt",upload_id=upload)
                    # Cleanup may observe a concurrently/repeatedly aborted upload.
                    # This exception never weakens any source CreateMultipartUpload oracle.
                    if not result.get("ok") and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):failures.append(result)
                except Exception as error:failures.append({"upload_id":upload,"error":str(error)})
            if failures:raise RuntimeError("pending multipart upload cleanup failed: "+str(failures))

BASE=SCOPE_OPERATIONS+" api.PutBucketPolicy api.DeleteBucketPolicy"
CASES=tuple(case("ceph_policy_condition_ssec_"+mode,"Ceph SSE-C 策略 "+mode,"原始条件、AES192/空正文、公开 SSE-C 密钥和 403；finally 删除桶策略",BASE+" put_http wire_headers",lambda p,mode=mode:customer(p,mode),reference,CEPH) for mode,reference in (("required","test_encryption_sse_c_enforced_with_bucket_policy"),("algorithm","test_encryption_sse_c_deny_algo_with_bucket_policy")))
CASES+=tuple(case("ceph_policy_condition_"+mode,"Ceph 加密策略 "+mode,"保留缺失/非法算法、原始正文、原始 KMS 配置前提及回退键、实际响应加密头",BASE+" put_http wire_headers",lambda p,mode=mode:encryption(p,mode),"test_bucket_policy_put_obj_"+mode,CEPH) for mode in ("s3_noenc","s3_incorrect_algo_sse_s3","s3_kms","kms_noenc","kms_s3"))
CASES+=tuple(case("ceph_policy_condition_existing_"+mode,"Ceph 已有标签策略 "+mode,"保留三个原始键/正文、全部标签、真实另一身份和每次状态断言；不修正 GetACL 源负例操作",BASE+" put identity_alt tags_put tags_get get_http"+(" api.GetObjectAcl" if mode=="get_acl" else ""),lambda p,mode=mode:tag_existing(p,mode),reference,CEPH) for mode,reference in (("get","test_bucket_policy_get_obj_existing_tag"),("get_tagging","test_bucket_policy_get_obj_tagging_existing_tag"),("put_tagging","test_bucket_policy_put_obj_tagging_existing_tag"),("get_acl","test_bucket_policy_get_obj_acl_existing_tag")))
CASES+=(case("ceph_policy_condition_request_tag","Ceph 请求标签策略","保留 fails_on_rgw 以及 hook 注册 main、请求发往 alt 的上游缺陷；第二次仍无标签但源期待成功",BASE+" put_http identity_alt",request_tag,"test_bucket_policy_put_obj_request_obj_tag",CEPH),case("ceph_policy_condition_ifexists","Ceph IfExists Referer 策略","保留主身份、三个 Referer、源期待最后 403、末尾 GetBucketPolicy 和 fails_on_rgw",BASE+" put get_http wire_headers api.GetBucketPolicy",referer,"test_bucket_policy_set_condition_operator_end_with_IfExists",CEPH),case("ceph_policy_condition_acl","Ceph ACL 策略条件","真实另一身份，无 ACL 上传成功，public-read 被 public* Deny 拒绝 403",BASE+" put_http wire_headers identity_alt",acl_condition,"test_bucket_policy_put_obj_acl",CEPH),case("ceph_policy_condition_grant","Ceph full-control 授权策略","保留 id=主用户条件、不同桶的上传者所有权、主用户拒绝和首个授权 ID 断言",BASE+" put_http wire_headers identity_alt api.GetObjectAcl",grant_condition,"test_bucket_policy_put_obj_grant",CEPH))
CASES+=tuple(case("ceph_policy_condition_copy_"+("metadata" if metadata else "source"),"Ceph 复制策略条件 "+("元数据" if metadata else "源路径"),"原始对象键/正文，两桶真实 SDK 复制/读取，保留 COPY 请求头移除和 Metadata 输入或 public/* 前缀与 403",BASE+" put copy get_http identity_alt"+(" wire_headers" if metadata else ""),lambda p,metadata=metadata:copy_condition(p,metadata),reference,CEPH) for metadata,reference in ((False,"test_bucket_policy_put_obj_copy_source"),(True,"test_bucket_policy_put_obj_copy_source_meta")))

CASES+=(case("ceph_policy_condition_notprincipal","Ceph Allow NotPrincipal 策略","保留固定租户 ARN，源期待 400 和 InvalidArgument/MalformedPolicy 二选一",BASE,notprincipal,"test_bucket_policy_allow_notprincipal",CEPH),case("ceph_policy_condition_head_prefix","Ceph 策略前缀 HEAD 错误","另一真实身份 public/object 404、private/object 403，空桶和桶 ARN 策略",BASE+" head_http identity_alt",head_prefix,"test_head_object_404_with_policy_prefix",CEPH),case("ceph_policy_condition_multipart","Ceph 分块上传策略资源","无策略/仅桶 ARN 的服务端 ClientError，精确对象 ARN 成功，真实 upload_id 最后 abort 清理",BASE+" mp_create mp_abort identity_alt",multipart,"test_bucket_policy_multipart",CEPH))

def register(registry):
    registry.add_suite(Suite("ceph_policy_conditions",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
