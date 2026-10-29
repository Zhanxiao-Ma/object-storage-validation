"""Translate Ceph SSE-S3/KMS object and default-encryption source inputs."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api
from .ceph_buckets import rejected
from .scenarios import Suite,assert_equal,require_ok,SDKOperationError
from .suites import case,run_cases,CaseNotRun,CEPH

ALGORITHM="x-amz-server-side-encryption"
KMS_KEY="x-amz-server-side-encryption-aws-kms-key-id"

def key_id(p):
    value=p.ctx.workload.get("kms_key_id")
    if value is None:raise CaseNotRun("Ceph KMS object transfer requires a configured usable main KMS key","workload.kms_key_id")
    return value

def service_error(response):
    """The original assert_raises(ClientError) requires an actual HTTP service error, not a local SDK validation exception."""
    assert_equal(response["ok"],False)
    if response.get("error_kind") not in ("sdk_error","not_found") or not isinstance(response.get("status_code"),int) or not 400<=response["status_code"]<600:
        raise AssertionError("expected actual service ClientError: "+str(response))

def read(bucket,p,headers=None):
    result=bucket.call("get_encryption",key="testobj",encryption_headers=headers or {},output_path=str(p.ctx.output))
    return result,p.ctx.output.read_bytes()

def transfer(p,mode,size):
    """Keep original A bytes, encryption headers, head/read rejection inputs and bucket default configuration through real SDK operations."""
    kms=mode in ("kms_transfer","kms_head","kms_present","kms_default")
    ident=key_id(p) if kms else None
    headers={ALGORITHM:"aws:kms",KMS_KEY:ident} if kms else {ALGORITHM:"AES256"}
    with BucketScope(p) as bucket:
        if mode in ("s3_default","s3_head","kms_default"):
            rule={"SSEAlgorithm":"aws:kms" if kms else "AES256"}
            if kms:rule["KMSMasterKeyID"]=ident
            assert_equal(api(bucket,"PutBucketEncryption",ServerSideEncryptionConfiguration={"Rules":[{"ApplyServerSideEncryptionByDefault":rule}]})["status_code"],200)
            put_headers={}
        else:put_headers=headers
        p.ctx.body.write_bytes(b"A"*size)
        try:result=bucket.call("put_encryption",key="testobj",body_path=str(p.ctx.body),encryption_headers=put_headers)
        except SDKOperationError as error:
            # A client checksum rejection can happen after a persisted PUT. Preserve
            # the failure while collecting read-only evidence, never infer corruption.
            if "checksum" in str(error).lower():
                import hashlib
                observed=bucket.raw("get_encryption",key="testobj",encryption_headers={},output_path=str(p.ctx.output))
                error.evidence["post_failure_get"]={k:observed[k] for k in ("ok","http_headers","error","error_code") if k in observed}
                if observed["ok"]:
                    actual=p.ctx.output.read_bytes()
                    error.evidence["independent_readback"]={"size":len(actual),"sha256":hashlib.sha256(actual).hexdigest(),"matches_input":actual==b"A"*size}
            raise
        if mode in ("s3_default","s3_explicit","kms_default"):
            assert_equal(result["http_headers"][ALGORITHM],"aws:kms" if kms else "AES256")
            if kms:assert_equal(result["http_headers"][KMS_KEY],ident)
        if mode in ("kms_head","s3_head"):
            result=bucket.call("head_encryption",key="testobj",encryption_headers={})
            assert_equal(result["http_headers"][ALGORITHM],"aws:kms" if kms else "AES256")
            if kms:assert_equal(result["http_headers"][KMS_KEY],ident)
            rejected(p,bucket.raw("head_encryption",key="testobj",encryption_headers=headers),400)
        else:
            result,body=read(bucket,p)
            if mode in ("s3_default","s3_explicit","kms_default"):
                assert_equal(result["http_headers"][ALGORITHM],"aws:kms" if kms else "AES256")
                if kms:assert_equal(result["http_headers"][KMS_KEY],ident)
            assert_equal(body,b"A"*size)
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def invalid(p,mode):
    """Verify default KMS reads without a key ID; reject the separate invalid key declarations with actual service errors."""
    with BucketScope(p) as bucket:
        headers={ALGORITHM:"aws:kms"} if mode=="no_key" else {KMS_KEY:"testkey-2"}
        p.ctx.body.write_bytes(b"A"*100)
        if mode=="read_declare":
            bucket.call("put_encryption",key="testobj",body_path=str(p.ctx.body),encryption_headers={})
            response=bucket.raw("get_encryption",key="testobj",encryption_headers={ALGORITHM:"aws:kms",KMS_KEY:"testkey-1"},output_path=str(p.ctx.output))
        else:response=bucket.raw("put_encryption",key="testobj",body_path=str(p.ctx.body),encryption_headers=headers)
        if mode=="no_key":
            require_ok(response)
            assert_equal(response["http_headers"].get(ALGORITHM),"aws:kms")
            observed,body=read(bucket,p)
            assert_equal(observed["http_headers"].get(ALGORITHM),"aws:kms")
            assert_equal(body,b"A"*100)
        else:rejected(p,response,400)
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

SIZES=(("1b",1),("1kb",1024),("1mb",1024*1024),("8mb",8*1024*1024))
VECTORS=tuple((mode+"_"+name,mode,size,"test_"+stem+"_"+name) for mode,stem in (("s3_default","sse_s3_default_upload"),("s3_explicit","sse_s3_encrypted_upload"),("kms_default","sse_kms_default_upload")) for name,size in SIZES)
VECTORS+=tuple(("kms_transfer_"+name,"kms_transfer",size,"test_sse_kms_transfer_"+name) for name,size in (("1b",1),("1kb",1024),("1MB",1024*1024),("13b",13)))
VECTORS+=(("kms_head","kms_head",1000,"test_sse_kms_method_head"),("kms_present","kms_present",100,"test_sse_kms_present"),("s3_head","s3_head",1000,"test_sse_s3_default_method_head"))
INVALID=(("no_key","test_sse_kms_no_key"),("not_declared","test_sse_kms_not_declared"),("read_declare","test_sse_kms_read_declare"))
CASES=tuple(case("ceph_encryption_"+name,"Ceph SSE-S3/KMS "+name,"保留原始 A 字节长度、默认/显式加密、KMS key ID 与原始 HTTP 响应/拒绝断言",SCOPE_OPERATIONS+" put_encryption get_encryption head_encryption"+(" api.PutBucketEncryption" if mode in ("s3_default","kms_default","s3_head") else ""),lambda p,mode=mode,size=size:transfer(p,mode,size),reference,CEPH) for name,mode,size,reference in VECTORS)
CASES+=tuple(case("ceph_encryption_"+mode,"Ceph KMS 特殊请求 "+mode,"未提供KeyId时验证默认KMS密钥读写；其余无效声明验证真实服务拒绝",SCOPE_OPERATIONS+" put_encryption get_encryption",lambda p,mode=mode:invalid(p,mode),reference,CEPH) for mode,reference in INVALID)

def register(registry):
    registry.add_suite(Suite("ceph_encryption_objects",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
