"""Translate actual cross-bucket multipart copy, malformed ranges and opaque source keys."""
from contextlib import ExitStack
import random
from .rejected_mutation import check_rejected_mutation
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_multipart import uploading,complete,prepare,read,assert_error,source_data,MIB
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def copied(source,destination,p,ident,key,size,part_size=5*MIB,range_enabled=True):
    parts=[]
    for index,start in enumerate(range(0,size,part_size)):
        options={"range_header":f"bytes={start}-{min(size,start+part_size)-1}"} if range_enabled else {}
        result=destination.call("mp_copy",key="mymultipart",source_bucket=source.name,source_key=key,upload_id=ident,part_number=index+1,**options)
        parts.append({"part_number":index+1,"etag":result["etag"]})
    complete(destination,ident,parts)
    result,body=read(destination,p)
    assert_equal(result["size"],size)
    # Expected bytes must come from the true source SDK read, not from another copy implementation.
    p.ctx.output.unlink(missing_ok=True)
    response=source.call("get",key=key,offset=0,length=size,output_path=str(p.ctx.output))
    assert_equal(body,p.ctx.output.read_bytes())

def invalid_range(p):
    """Isolate range bounds on a valid >5MiB source, prove a valid control copy, and reject both start/end overflow without publishing an object."""
    size=5*MIB+1
    with BucketScope(p) as bucket:
        bucket.put("source",b"A"*size)
        with uploading(bucket,p,key="range-control") as ident:
            part=bucket.call("mp_copy",key="range-control",source_bucket=bucket.name,source_key="source",upload_id=ident,part_number=1,range_header="bytes=0-4")
            bucket.call("mp_complete_observed",key="range-control",upload_id=ident,parts=[{"part_number":1,"etag":part["etag"]}])
            p.ctx.output.unlink(missing_ok=True)
            bucket.call("get_metadata",key="range-control",output_path=str(p.ctx.output))
            assert_equal(p.ctx.output.read_bytes(),b"AAAAA")
        for value in (f"bytes={size}-{size+5}",f"bytes=0-{size+5}"):
            with uploading(bucket,p,key="dest") as ident:
                response=bucket.raw("mp_copy",key="dest",source_bucket=bucket.name,source_key="source",upload_id=ident,part_number=1,range_header=value)
                # S3 invalid-argument and unsatisfiable-range errors are distinct
                # permitted rejection forms; unrelated errors and 200 still fail.
                status,code=(416,"InvalidRange") if response.get("status_code")==416 else (400,"InvalidArgument")
                try:
                    check_rejected_mutation(bucket,"dest",response,status,code,operation="UploadPartCopy",get_operation="get_metadata")
                except AssertionError as error:
                    error.evidence.update(request_range=value,source_size=size)
                    raise


def copy(p,mode):
    if mode=="invalid":return invalid_range(p)
    if mode=="improper":
        with BucketScope(p) as bucket:
            bucket.put("source",b"ABCDE")
            with uploading(bucket,p,key="dest") as ident:
                ranges=("0-2","bytes=0","bytes=hello-world","bytes=0-bar","bytes=hello-","bytes=0-2,3-5")
                for value in ranges:
                    result=bucket.raw("mp_copy",key="dest",source_bucket=bucket.name,source_key="source",upload_id=ident,part_number=1,range_header=value)
                    assert_error(result,400,"InvalidArgument")
        return
    with BucketScope(p) as source,BucketScope(p) as destination:
        size=12*MIB if mode=="sizes" else 10 if mode=="without_range" else 7*MIB
        keys=(" ","_","__","?versionId") if mode=="special" else ("source" if mode=="without_range" else "foo",)
        for key in keys:
            data=source_data(size,part_size=size,seed=607);source.put(key,data)
            sizes=(5*MIB,5*MIB+100*1024,5*MIB+600*1024,10*MIB+100*1024,10*MIB+600*1024,10*MIB) if mode=="sizes" else (10 if mode=="without_range" else 1,)
            for count in sizes:
                with uploading(destination,p) as ident:
                    copied(source,destination,p,ident,key,count,range_enabled=mode!="without_range")

def list_uploads(p):
    with BucketScope(p) as bucket,ExitStack() as uploads:
        first=uploads.enter_context(uploading(bucket,p))
        prepare(bucket,p,first,source_data(5*MIB,seed=701))
        second=uploads.enter_context(uploading(bucket,p))
        prepare(bucket,p,second,source_data(6*MIB,seed=702))
        third=uploads.enter_context(uploading(bucket,p,key="mymultipart2"))
        # Source helper uploads the third key; it cannot use the default-key helper.
        p.ctx.body.write_bytes(source_data(5*MIB,seed=703))
        bucket.call("mp_part",key="mymultipart2",upload_id=third,part_number=1,body_path=str(p.ctx.body))
        actual=[item["upload_id"] for item in bucket.call("mp_list")["uploads"]]
        for ident in (first,second,third):assert_equal(ident in actual,True)

BASE=SCOPE_OPERATIONS+" put get get_metadata mp_create_metadata mp_copy mp_complete_observed mp_abort_raw"
VECTORS=(("small","test_multipart_copy_small"),("invalid","test_multipart_copy_invalid_range"),("improper","test_multipart_copy_improper_range"),("without_range","test_multipart_copy_without_range"),("special","test_multipart_copy_special_names"),("sizes","test_multipart_copy_multiple_sizes"))
CASES=tuple(case("ceph_exact_multipart_copy_"+mode,"Ceph 分块复制 "+mode,"保留原始跨桶源对象、原始大小/特殊键、六种非法 Range、HTTP/错误码及源目标读取对照",BASE,lambda p,mode=mode:copy(p,mode),reference,CEPH) for mode,reference in VECTORS)
CASES+=(case("ceph_exact_multipart_list","Ceph 未完成上传清单","三个实际上传 ID 均可列出；结束时逐一取消并清理测试桶",SCOPE_OPERATIONS+" mp_create_metadata mp_part mp_abort_raw mp_list",list_uploads,"test_list_multipart_upload",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_multipart_copy",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
