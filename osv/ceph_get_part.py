"""Keep original PartNumber GET/HEAD sizes, object ETags and out-of-range errors."""
import random
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_multipart import uploading,prepare,complete,assert_error,source_data,MIB
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def get_part(p,mode):
    with BucketScope(p) as bucket:
        if mode=="ordinary":
            p.ctx.body.write_bytes(b"body")
            etag=bucket.call("put_http",key="singlepart",body_path=str(p.ctx.body))["etag"]
            assert_error(bucket.raw("get_part",key="singlepart",part_number=2,output_path=str(p.ctx.output)),400,"InvalidPart")
            result=bucket.call("get_part",key="singlepart",part_number=1,output_path=str(p.ctx.output))
            assert_equal(result["etag"],etag);assert_equal(p.ctx.output.read_bytes(),b"body")
            return
        sizes=(5*MIB,)*3+(MIB,) if mode=="multiple" else (5*MIB,)
        data=source_data(sum(sizes),seed=1501)
        with uploading(bucket,p) as ident:
            parts=prepare(bucket,p,ident,data,resend=(2,) if mode=="multiple" else ())
            assert_error(bucket.raw("get_part",key="mymultipart",part_number=1,output_path=str(p.ctx.output)),404,"NoSuchKey")
            result=complete(bucket,ident,parts);etag=result["etag"]
            assert_equal(len(parts),len(sizes));offset=0
            for number,size in enumerate(sizes,1):
                head=bucket.call("head_part",key="mymultipart",part_number=number)
                assert_equal(head["parts_count"],len(sizes));assert_equal(head["etag"],etag)
                result=bucket.call("get_part",key="mymultipart",part_number=number,output_path=str(p.ctx.output))
                assert_equal(result["parts_count"],len(sizes));assert_equal(result["etag"],etag);assert_equal(result["size"],size)
                assert_equal(p.ctx.output.read_bytes(),data[offset:offset+size]);offset+=size
            assert_error(bucket.raw("get_part",key="mymultipart",part_number=5,output_path=str(p.ctx.output)),400,"InvalidPart")

VECTORS=(("multiple","test_multipart_get_part"),("single","test_multipart_single_get_part"),("ordinary","test_non_multipart_get_part"))
CASES=tuple(case("ceph_get_part_"+mode,"Ceph 按分块编号读取 "+mode,"保留完成前不可见、PartsCount/ETag、各分块长度和字节、越界 400/InvalidPart",SCOPE_OPERATIONS+(" put_http get_part" if mode=="ordinary" else " mp_create_metadata mp_part mp_complete_observed mp_abort_raw get_part head_part"),lambda p,mode=mode:get_part(p,mode),reference,CEPH) for mode,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_get_part",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
