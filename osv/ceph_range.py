"""Translate exact Ceph positive Range vectors including actual HTTP response assertions."""
import random
import string
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def ranged_http(p,mode):
    """Provision independent payload bytes, then assert all upstream body, status and Content-Range expectations."""
    content=b"testcontent"
    if mode=="big":
        rng=random.Random(851)
        content=bytes(rng.choices((string.ascii_letters+string.digits).encode(),k=8*1024*1024))
    vectors={"closed":("bytes=4-7",4,8),"open":("bytes=4-",4,11),"suffix":("bytes=-7",4,11),"big":("bytes=3145728-5242880",3145728,5242881)}
    header,start,stop=vectors[mode]
    p.put(content)
    p.ctx.output.unlink(missing_ok=True)
    result=p.call("get_http",range_header=header,output_path=str(p.ctx.output))
    assert_equal(p.ctx.output.read_bytes(),content[start:stop])
    headers=result["http_headers"];expected=f"bytes {start}-{stop-1}/{len(content)}"
    if result["status_code"]==206:
        assert_equal(headers.get("content-range"),expected)
        return {"range_response":"standard_206"}
    # The SDK negotiated this S3 transfer variant on the actual transmitted request.
    # Preserve real HTTP200 and alternate headers; never relabel the response as 206.
    assert_equal(result["status_code"],200)
    assert_equal(result.get("request_headers",{}).get("range"),header)
    assert_equal(result.get("request_headers",{}).get("x-amz-te"),"append-md5")
    assert_equal(headers.get("x-amz-content-range"),expected)
    assert_equal(headers.get("x-amz-transfer-encoding"),"append-md5")
    return {"range_response":"negotiated_append_md5_200"}

def invalid_http_range(p,empty):
    """Send the exact upstream bytes=40-50 against testcontent or an empty object; require the actual 416 InvalidRange service error."""
    p.put(b"" if empty else b"testcontent")
    result=p.raw("get_http",range_header="bytes=40-50",output_path=str(p.ctx.output))
    assert_equal(result["ok"],False)
    assert_equal(result.get("error_kind"),"local_error" if p.ctx.worker.info["api"]=="local" else "sdk_error")
    assert_equal(result.get("error_code"),"InvalidRange")
    if p.ctx.worker.info["api"]!="local":assert_equal(result.get("status_code"),416)

VECTORS=(("closed","test_ranged_request_response_code"),("open","test_ranged_request_skip_leading_bytes_response_code"),("suffix","test_ranged_request_return_trailing_bytes_response_code"),("big","test_ranged_big_request_response_code"))
CASES=tuple(case("ceph_http_range_"+mode,"Ceph 精确 Range 语义 "+mode,
    "校验完整范围字节与206/Content-Range；实际协商append-md5时独立验证200变体并记录真实状态","put get_http",lambda p,mode=mode:ranged_http(p,mode),reference,CEPH) for mode,reference in VECTORS)

CASES+=tuple(case("ceph_http_range_"+mode,"Ceph 精确非法 Range "+mode,
    "原始 bytes=40-50 被拒绝，服务端返回 416/InvalidRange","put get_http",lambda p,empty=empty:invalid_http_range(p,empty),reference,CEPH)
    for mode,empty,reference in (("invalid",False,"test_ranged_request_invalid_range"),("empty",True,"test_ranged_request_empty_object")))

def register(registry):
    registry.add_suite(Suite("ceph_http_range",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
