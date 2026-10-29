"""Preserve pinned Ceph SSE-C data, missing headers and service-error oracles."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_buckets import rejected
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

ALGORITHM="x-amz-server-side-encryption-customer-algorithm"
KEY="x-amz-server-side-encryption-customer-key"
MD5="x-amz-server-side-encryption-customer-key-md5"
HEADERS={ALGORITHM:"AES256",KEY:"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=",MD5:"DWygnHRtgiJ77HCm+1rvHw=="}
OTHER={ALGORITHM:"AES256",KEY:"6b+WOZ1T3cqZMxgThRcXAQBrS5mXKdDUphvpxptl9/4=",MD5:"arxBvwY2V4SiOne6yppVPQ=="}

def rejected_customer_key(bucket,p,key,headers,expected):
    """Require a real denial for a wrong key, with successful correct-key reads on both sides."""
    bucket.call("get_sse_c",key=key,sse_headers=HEADERS,output_path=str(p.ctx.output))
    assert_equal(p.ctx.output.read_bytes(),expected)
    response=bucket.raw("get_sse_c",key=key,sse_headers=headers,output_path=str(p.ctx.output))
    bucket.call("get_sse_c",key=key,sse_headers=HEADERS,output_path=str(p.ctx.output))
    assert_equal(p.ctx.output.read_bytes(),expected)
    assert_equal(response["ok"],False)
    assert_equal(response.get("error_kind"),"sdk_error")
    assert_equal((response.get("status_code"),response.get("error_code")) in
                 {(400,"InvalidRequest"),(400,"InvalidArgument"),(400,"AccessDenied"),(403,"AccessDenied")},True)

def encrypted(p,mode,size):
    """Use literal raw headers through genuine SDK signing/transport, then assert original bytes or actual service rejection; clean even failed assertions."""
    with BucketScope(p) as bucket:
        headers=dict(HEADERS)
        if mode=="invalid_md5":headers[MD5]="AAAAAAAAAAAAAAAAAAAAAA=="
        elif mode=="no_md5":del headers[MD5]
        elif mode=="no_key":headers={ALGORITHM:"AES256"}
        elif mode=="no_algorithm":del headers[ALGORITHM]
        p.ctx.body.write_bytes(b"A"*size)
        options={"key":"testobj","body_path":str(p.ctx.body),"sse_headers":headers}
        if mode in ("invalid_md5","no_md5","no_key","no_algorithm"):
            response=bucket.raw("put_sse_c",**options)
            if mode in ("no_md5","no_key"):
                # Source only asserts ClientError, without constraining status or error code.
                assert_equal(response["ok"],False)
                assert_equal(response.get("error_kind"),"sdk_error")
                if not isinstance(response.get("status_code"),int) or not 400<=response["status_code"]<600:
                    raise AssertionError("expected actual service ClientError: "+str(response))
            else:rejected(p,response,400)
        else:
            bucket.call("put_sse_c",**options)
            if mode in ("head","present","other"):
                operation="head_sse_c" if mode=="head" else "get_sse_c"
                options={"key":"testobj","sse_headers":OTHER if mode=="other" else {}}
                if mode!="head":options["output_path"]=str(p.ctx.output)
                if mode=="other":rejected_customer_key(bucket,p,"testobj",OTHER,b"A"*size)
                else:rejected(p,bucket.raw(operation,**options),400)
            if mode=="head":
                assert_equal(bucket.call("head_sse_c",key="testobj",sse_headers=HEADERS)["status_code"],200)
            elif mode=="transfer":
                bucket.call("get_sse_c",key="testobj",sse_headers=HEADERS,output_path=str(p.ctx.output))
                assert_equal(p.ctx.output.read_bytes(),b"A"*size)
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

VECTORS=(
    ("1b","transfer",1,"test_encrypted_transfer_1b"),
    ("1kb","transfer",1024,"test_encrypted_transfer_1kb"),
    ("1MB","transfer",1024*1024,"test_encrypted_transfer_1MB"),
    ("13b","transfer",13,"test_encrypted_transfer_13b"),
    ("head","head",1000,"test_encryption_sse_c_method_head"),
    ("present","present",1000,"test_encryption_sse_c_present"),
    ("other","other",100,"test_encryption_sse_c_other_key"),
    ("invalid_md5","invalid_md5",100,"test_encryption_sse_c_invalid_md5"),
    ("no_md5","no_md5",100,"test_encryption_sse_c_no_md5"),
    ("no_key","no_key",100,"test_encryption_sse_c_no_key"),
    ("no_algorithm","no_algorithm",100,"test_encryption_key_no_sse_c"),
)
CASES=tuple(case("ceph_sse_c_"+name,"Ceph SSE-C "+name,
    "保留原始 A 字节、AES256/密钥/MD5 请求头及缺失/错误请求头的实际服务端错误，不使用 SDK 模型自动补 MD5",
    SCOPE_OPERATIONS+" put_sse_c"+(" head_sse_c" if mode=="head" else " get_sse_c" if mode in ("transfer","present","other") else ""),
    lambda p,mode=mode,size=size:encrypted(p,mode,size),reference,CEPH)
    for name,mode,size,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_sse_customer",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
