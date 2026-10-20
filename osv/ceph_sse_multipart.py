"""Preserve Ceph SSE-C multipart geometry, raw headers and original read/error checks."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_buckets import rejected
from .ceph_multipart import source_data,uploading,MIB
from .ceph_sse_customer import HEADERS,OTHER,MD5
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH

def write_parts(bucket,p,ident,key,data,step,headers,resend=()):
    """Send original parts in order, stopping only on a real SDK error; return actual service ETags and any error for source negative assertions."""
    parts=[]
    for index,start in enumerate(range(0,len(data),step)):
        p.ctx.body.write_bytes(data[start:start+step])
        options={"key":key,"upload_id":ident,"part_number":index+1,"body_path":str(p.ctx.body),"sse_headers":headers}
        result=bucket.raw("mp_part_sse_c",**options)
        if not result["ok"]:return parts,result
        parts.append({"part_number":index+1,"etag":result["etag"].strip(chr(34))})
        if index in resend:
            result=bucket.raw("mp_part_sse_c",**options)
            if not result["ok"]:return parts,result
    return parts,None

def read(bucket,p,key,headers,**options):
    p.ctx.output.unlink(missing_ok=True)
    result=bucket.call("get_sse_c",key=key,sse_headers=headers,output_path=str(p.ctx.output),**options)
    return result,p.ctx.output.read_bytes()

def multipart(p,mode):
    """Exercise actual 30-MiB multipart uploads or the original four-part read vector, with scoped abort and object/bucket cleanup on every failure."""
    key="mymultipart" if mode=="get_part" else "multipart_enc"
    step=5*MIB+(1 if mode=="unaligned" else 0)
    size=16*MIB if mode=="get_part" else 30*MIB
    data=source_data(size,step,seed=725)
    init_headers=HEADERS if mode=="get_part" else {**HEADERS,"content-type":"text/plain"}
    part_headers=OTHER if mode=="wrong_part_key" else {**HEADERS,MD5:"AAAAAAAAAAAAAAAAAAAAAA=="} if mode=="wrong_part_md5" else init_headers
    metadata={} if mode=="get_part" else {"foo":"bar"}
    with BucketScope(p) as bucket:
        with uploading(bucket,p,key,create_operation="mp_create_sse_c",sse_headers=init_headers,metadata=metadata) as ident:
            parts,error=write_parts(bucket,p,ident,key,data,step,part_headers,resend=(2,) if mode=="get_part" else ())
            if mode in ("wrong_part_key","wrong_part_md5"):
                if error is None:raise AssertionError("source expected multipart helper to raise a service error; all original chunks were accepted")
                rejected(p,error,400)
            else:
                if error is not None:require_ok(error)
                if mode=="get_part":
                    # Original pre-completion request has no SSE-C headers.
                    rejected(p,bucket.raw("get_part",key=key,part_number=1,output_path=str(p.ctx.output)),404,"NoSuchKey")
                result=bucket.call("mp_complete_sse_c",key=key,upload_id=ident,parts=parts,sse_headers=init_headers)
                if mode=="get_part":
                    assert_equal(len(parts),4)
                    offset=0
                    for part,part_size in zip(parts,[5*MIB]*3+[MIB]):
                        head=bucket.call("head_sse_c",key=key,sse_headers=HEADERS,part_number=part["part_number"])
                        assert_equal(head["parts_count"],4);assert_equal(head["etag"],result["etag"])
                        observed,body=read(bucket,p,key,HEADERS,part_number=part["part_number"])
                        assert_equal(observed["parts_count"],4);assert_equal(observed["etag"],result["etag"]);assert_equal(observed["size"],part_size)
                        assert_equal(body,data[offset:offset+part_size]);offset+=part_size
                    # Original out-of-range request also has no SSE-C headers.
                    rejected(p,bucket.raw("get_part",key=key,part_number=5,output_path=str(p.ctx.output)),400,"InvalidPart")
                else:
                    listing=bucket.call("list_v2_observed",prefix=key)
                    assert_equal(len(listing["objects"]),1);assert_equal(listing["objects"][0]["size"],size)
                    observed,body=read(bucket,p,key,init_headers)
                    assert_equal(observed["metadata"],metadata);assert_equal(observed["http_headers"]["content-type"],"text/plain")
                    if mode=="bad_download":rejected(p,bucket.raw("get_sse_c",key=key,sse_headers=OTHER,output_path=str(p.ctx.output)),400)
                    else:
                        assert_equal(body,data);assert_equal(len(body),observed["size"])
                        for stride in (1000000,10000000,step-1,step,step+1):
                            for offset in range(0,size,stride):
                                length=min(stride,size-offset)
                                observed,body=read(bucket,p,key,init_headers,range_header=f"bytes={offset}-{offset+length-1}")
                                assert_equal(observed["size"],length);assert_equal(body,data[offset:offset+length])
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

def ordinary(p):
    """Use original body, PartNumber=2 InvalidPart and PartNumber=1 whole-object ETag/byte checks through actual SSE-C SDK requests."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"body")
        etag=bucket.call("put_sse_c",key="singlepart",body_path=str(p.ctx.body),sse_headers=HEADERS)["etag"]
        rejected(p,bucket.raw("get_sse_c",key="singlepart",sse_headers=HEADERS,part_number=2,output_path=str(p.ctx.output)),400,"InvalidPart")
        observed,body=read(bucket,p,"singlepart",HEADERS,part_number=1)
        assert_equal(observed["etag"],etag);assert_equal(body,b"body")
    return {"isolated_bucket":bucket.name,"cleanup_confirmed":bucket.cleanup_completed}

VECTORS=(("full","test_encryption_sse_c_multipart_upload"),("unaligned","test_encryption_sse_c_unaligned_multipart_upload"),("wrong_part_key","test_encryption_sse_c_multipart_invalid_chunks_1"),("wrong_part_md5","test_encryption_sse_c_multipart_invalid_chunks_2"),("bad_download","test_encryption_sse_c_multipart_bad_download"),("get_part","test_multipart_sse_c_get_part"))
CASES=tuple(case("ceph_sse_multipart_"+mode,"Ceph SSE-C 分块 "+mode,"保留原始分块大小、密钥/MD5、元数据、Range 与服务端错误断言",SCOPE_OPERATIONS+" mp_create_sse_c mp_part_sse_c mp_complete_sse_c mp_abort_raw get_sse_c"+(" head_sse_c get_part" if mode=="get_part" else " list_v2_observed"),lambda p,mode=mode:multipart(p,mode),reference,CEPH) for mode,reference in VECTORS)
CASES+=(case("ceph_sse_multipart_ordinary","Ceph SSE-C 普通对象按分块编号读取","保留 body 字节、ETag 与原始越界 400/InvalidPart",SCOPE_OPERATIONS+" put_sse_c get_sse_c",ordinary,"test_non_multipart_sse_c_get_part",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_sse_multipart",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
