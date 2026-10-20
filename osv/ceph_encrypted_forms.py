"""Retain Ceph encrypted multipart-form HTTP while actual SDKs configure, read and clean owned buckets."""
from collections import OrderedDict
from datetime import datetime,timedelta,timezone
import base64
import hashlib
import hmac
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api
from .ceph_encryption_objects import key_id,ALGORITHM,KMS_KEY
from .ceph_raw_http import SourceHttp
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

SSE_C={"x-amz-server-side-encryption-customer-algorithm":"AES256","x-amz-server-side-encryption-customer-key":"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=","x-amz-server-side-encryption-customer-key-md5":"DWygnHRtgiJ77HCm+1rvHw=="}

def encrypted_form(p,mode):
    """Keep original ordered files, encryption policy conditions and independent SDK GET content/header assertions; pre-track all possible writes before HTTP."""
    kms=mode in ("kms","kms_default")
    ident=key_id(p) if kms else None
    transport=SourceHttp(p)
    with BucketScope(p) as bucket:
        if mode in ("s3_default","kms_default"):
            rule={"SSEAlgorithm":"aws:kms" if kms else "AES256"}
            if kms:rule["KMSMasterKeyID"]=ident
            assert_equal(api(bucket,"PutBucketEncryption",ServerSideEncryptionConfiguration={"Rules":[{"ApplyServerSideEncryptionByDefault":rule}]})["status_code"],200)
        conditions=[{"bucket":bucket.name},["starts-with","$key","foo"],{"acl":"private"},["starts-with","$Content-Type","text/plain"]]
        encryption=SSE_C if mode=="customer" else {ALGORITHM:"aws:kms",KMS_KEY:ident} if mode=="kms" else {}
        for field in encryption or {ALGORITHM:""}:conditions.append(["starts-with","$"+field,""])
        conditions.append(["content-length-range",0,1024])
        policy_document={"expiration":(datetime.now(timezone.utc)+timedelta(seconds=6000)).strftime("%Y-%m-%dT%H:%M:%SZ"),"conditions":conditions}
        policy=base64.b64encode(json.JSONEncoder().encode(policy_document).encode("utf-8"))
        signature=base64.b64encode(hmac.new(transport.secret.encode("utf-8"),policy,hashlib.sha1).digest())
        fields=OrderedDict([("key","foo.txt"),("AWSAccessKeyId",transport.access),("acl","private"),("signature",signature),("policy",policy),("Content-Type","text/plain"),*encryption.items(),("file","bar")])
        bucket.written_keys.add("foo.txt")
        response=transport.request("POST",transport.url(bucket.name),files=fields)
        assert_equal(response.status_code,204)
        result=bucket.call("get_encryption",key="foo.txt",encryption_headers=SSE_C if mode=="customer" else {},output_path=str(p.ctx.output))
        if mode in ("s3_default","kms_default"):
            assert_equal(result["http_headers"][ALGORITHM],"aws:kms" if kms else "AES256")
            if kms:assert_equal(result["http_headers"][KMS_KEY],ident)
        assert_equal(p.ctx.output.read_bytes(),b"bar")
    return transport.evidence()

VECTORS=(("customer","test_encryption_sse_c_post_object_authenticated_request"),("kms","test_sse_kms_post_object_authenticated_request"),("s3_default","test_sse_s3_default_post_object_authenticated_request"),("kms_default","test_sse_kms_default_post_object_authenticated_request"))
CASES=tuple(case("ceph_encrypted_form_"+mode,"Ceph 加密表单 "+mode,"保留原HTTP policy/HMAC和files顺序；真实SDK默认加密配置、GET正文/响应头和所有路径资源清理；raw HTTP不计SDK性能",SCOPE_OPERATIONS+" get_encryption"+(" api.PutBucketEncryption" if mode.endswith("_default") else ""),lambda p,mode=mode:encrypted_form(p,mode),reference,CEPH) for mode,reference in VECTORS)
def register(registry):
    registry.add_suite(Suite("ceph_encrypted_forms",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
