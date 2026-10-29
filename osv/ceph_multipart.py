"""Preserve concrete Ceph multipart sizes, retry requests, metadata, byte ranges and error oracles."""
from contextlib import contextmanager
import random
from .rejected_mutation import check_rejected_mutation
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH

MIB=1024*1024

def source_data(size,part_size=5*MIB,seed=101):
    """Keep Ceph's ASCII-letter, per-part repeated-1-KiB data shape with an independent reproducible seed."""
    rng=random.Random(seed);parts=[]
    for offset in range(0,size,part_size):
        pattern=bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(1024))
        count=min(size-offset,part_size)
        parts.append(pattern*(count//1024)+pattern[:count%1024])
    return b"".join(parts)

@contextmanager
def uploading(bucket,p,key="mymultipart",create_operation="mp_create_metadata",**options):
    """Abort the actual upload even when an assertion fails; surface cleanup errors rather than leaking parts."""
    if create_operation not in ("mp_create_metadata","mp_create_sse_c"):raise ValueError("unsupported scoped multipart creation operation")
    ident=bucket.call(create_operation,key=key,**options)["upload_id"]
    primary=None
    try:yield ident
    except Exception as error:
        primary=error
        raise
    finally:
        try:response=bucket.raw("mp_abort_raw",key=key,upload_id=ident)
        except Exception as error:
            raise RuntimeError((str(primary)+"; " if primary else "")+"multipart cleanup transport failure: "+str(error)) from primary
        if not response["ok"] and (response.get("status_code"),response.get("error_code"))!=(404,"NoSuchUpload"):
            raise RuntimeError((str(primary)+"; " if primary else "")+"multipart cleanup failed for "+bucket.name+" upload "+ident+": "+str(response))

def part(bucket,p,ident,number,data):
    p.ctx.body.write_bytes(data)
    result=bucket.call("mp_part",key="mymultipart",upload_id=ident,part_number=number,body_path=str(p.ctx.body))
    return {"part_number":number,"etag":result["etag"].strip(chr(34))}

def prepare(bucket,p,ident,data,part_size=5*MIB,resend=()):
    parts=[]
    for index,start in enumerate(range(0,len(data),part_size)):
        payload=data[start:start+part_size]
        parts.append(part(bucket,p,ident,index+1,payload))
        if index in resend:parts[-1]=part(bucket,p,ident,index+1,payload)
    return parts

def complete(bucket,ident,parts):
    return bucket.call("mp_complete_observed",key="mymultipart",upload_id=ident,parts=parts)

def repeat_complete(bucket,ident,parts,first):
    """A successful completion consumes the upload; retain either identical success or explicit NoSuchUpload, never arbitrary failure."""
    response=bucket.raw("mp_complete_observed",key="mymultipart",upload_id=ident,parts=parts)
    if response["ok"]:assert_equal(response["etag"],first["etag"])
    else:assert_error(response,404,"NoSuchUpload")
    return {"repeat_status":response.get("status_code"),"repeat_error":response.get("error_code")}

def read(bucket,p,**options):
    p.ctx.output.unlink(missing_ok=True)
    result=bucket.call("get_metadata",key="mymultipart",output_path=str(p.ctx.output),**options)
    return result,p.ctx.output.read_bytes()

def check_data(bucket,p,data,metadata=False,ranges=False):
    result,body=read(bucket,p)
    assert_equal(result["size"],len(body));assert_equal(body,data)
    if metadata:
        assert_equal(result["content_type"],"text/bla")
        assert_equal(result["metadata"],{"foo":"bar"})
    if ranges:
        for step in (1000000,10000000):
            for offset in range(0,len(data),step):
                length=min(step,len(data)-offset)
                result,body=read(bucket,p,offset=offset,length=length)
                assert_equal(result["size"],length)
                assert_equal(body,data[offset:offset+length])

def upload(p,mode):
    with BucketScope(p) as bucket:
        if mode=="sizes":
            for size in (5*MIB,5*MIB+100*1024,5*MIB+600*1024,10*MIB+100*1024,10*MIB+600*1024,10*MIB):
                with uploading(bucket,p) as ident:
                    complete(bucket,ident,prepare(bucket,p,ident,source_data(size,seed=size)))
            return
        if mode=="resend":
            for resend in ((0,),(1,),(2,),(1,2),(0,1,2,3,4,5)):
                data=source_data(30*MIB,seed=103)
                with uploading(bucket,p,content_type="text/bla",metadata={"foo":"bar"}) as ident:
                    complete(bucket,ident,prepare(bucket,p,ident,data,resend=resend))
                    check_data(bucket,p,data,metadata=True,ranges=True)
            return
        if mode=="small":
            with uploading(bucket,p) as ident:
                data=source_data(1);parts=prepare(bucket,p,ident,data)
                first=complete(bucket,ident,parts)
                repeat_complete(bucket,ident,parts,first)
                check_data(bucket,p,data)
            return
        if mode in ("contents","overwrite"):
            payload=b"ABCDE"*MIB if mode=="contents" else b"12345"*MIB
            count=3 if mode=="contents" else 2
            if mode=="overwrite":bucket.put("mymultipart",payload)
            with uploading(bucket,p) as ident:
                parts=[part(bucket,p,ident,i+1,payload) for i in range(count)]
                data=payload*count
                if mode=="contents":
                    tail=b"123"*MIB;data+=tail;parts.append(part(bucket,p,ident,count+1,tail))
                result=complete(bucket,ident,parts)
                if mode=="contents":assert_equal(bool(result["etag"]),True)
                assert_equal(read(bucket,p)[1],data)
            return
        if mode=="full":
            data=source_data(30*MIB)
            with uploading(bucket,p,content_type="text/bla",metadata={"foo":"bar"}) as ident:
                parts=prepare(bucket,p,ident,data)
                first=complete(bucket,ident,parts);repeat_complete(bucket,ident,parts,first)
                listing=bucket.call("list_v2_observed",prefix="mymultipart")
                assert_equal(len(listing["objects"]),1);assert_equal(listing["objects"][0]["size"],30*MIB)
                check_data(bucket,p,data,metadata=True,ranges=True)
            return
        if mode=="abort":
            with uploading(bucket,p) as ident:
                prepare(bucket,p,ident,source_data(10*MIB,seed=102))
                bucket.call("mp_abort_raw",key="mymultipart",upload_id=ident)
                assert_equal("Contents" in bucket.call("list_v2_observed",prefix="mymultipart")["fields"],False)
            return
        if mode=="abort_missing":
            bucket.put("mymultipart",b"")
            # A real ID belonging to another key avoids malformed IDs and
            # the separate idempotent repeat-abort behavior.
            with uploading(bucket,p,key="upload-id-source") as ident:
                result=bucket.raw("mp_abort_raw",key="mymultipart",upload_id=ident)
                check_rejected_mutation(bucket,"mymultipart",result,404,"NoSuchUpload",expected=b"",
                                        operation="AbortMultipartUpload",get_operation="get_metadata")
                listing=bucket.call("mp_list",prefix="upload-id-source")
                preserved=any(row["key"]==bucket.key("upload-id-source") and row["upload_id"]==ident for row in listing["uploads"])
                assert_equal(("source upload preserved",preserved),("source upload preserved",True))
            return
        if mode=="missing_upload":
            with uploading(bucket,p) as ident:
                parts=[part(bucket,p,ident,1,b"probe")]
                bucket.call("mp_abort_raw",key="mymultipart",upload_id=ident)
                result=bucket.raw("mp_complete_observed",key="mymultipart",upload_id=ident,parts=parts)
                check_rejected_mutation(bucket,"mymultipart",result,404,"NoSuchUpload",
                                        operation="CompleteMultipartUpload",get_operation="get_metadata")
            return
        with uploading(bucket,p) as ident:
            if mode=="empty":result=bucket.raw("mp_complete_empty",key="mymultipart",upload_id=ident)
            else:
                data=b"\x00" if mode in ("missing_part","etag") else source_data(100*1024,10*1024,104)
                parts=prepare(bucket,p,ident,data,10*1024 if mode=="too_small" else 5*MIB)
                if mode=="missing_part":parts[0]["part_number"]=9999
                elif mode=="etag":parts[0]["etag"]="ffffffffffffffffffffffffffffffff"
                result=bucket.raw("mp_complete_observed",key="mymultipart",upload_id=ident,parts=parts)
            check_rejected_mutation(bucket,"mymultipart",result,400,"MalformedXML" if mode=="empty" else "EntityTooSmall" if mode=="too_small" else "InvalidPart",operation="CompleteMultipartUpload",get_operation="get_metadata")

def assert_error(result,status,code):
    assert_equal(result["ok"],False)
    assert_equal(result.get("status_code"),status)
    assert_equal(result.get("error_code"),code)

VECTORS=(
 ("full","test_multipart_upload"),("small","test_multipart_upload_small"),
 ("resend","test_multipart_upload_resend_part"),("sizes","test_multipart_upload_multiple_sizes"),
 ("contents","test_multipart_upload_contents"),("overwrite","test_multipart_upload_overwrite_existing_object"),
 ("abort","test_abort_multipart_upload"),("abort_missing","test_abort_multipart_upload_not_found"),
 ("empty","test_multipart_upload_empty"),("missing_upload","test_multipart_upload_complete_without_create"),
 ("too_small","test_multipart_upload_size_too_small"),("missing_part","test_multipart_upload_missing_part"),
 ("etag","test_multipart_upload_incorrect_etag"))

# The bucket lifecycle and multipart cleanup are required even for negative cases.
BASE=SCOPE_OPERATIONS+" mp_create_metadata mp_part mp_complete_observed mp_abort_raw"
def operations(mode):
    extras={"full":"get_metadata list_v2_observed","small":"get_metadata","resend":"get_metadata","contents":"get_metadata","overwrite":"put get_metadata","abort":"list_v2_observed","abort_missing":"put mp_list","empty":"mp_complete_empty"}
    return BASE+" "+extras.get(mode,"")+(" get_metadata" if mode in ("empty","too_small","missing_part","etag","abort_missing","missing_upload") else "")

CASES=tuple(case("ceph_exact_multipart_"+mode,"Ceph 精确分块上传 "+mode,"保留原始大小、重传组合、元数据、完整字节/Range 和 HTTP/错误码断言",operations(mode),lambda p,mode=mode:upload(p,mode),reference,CEPH) for mode,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_exact_multipart",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
