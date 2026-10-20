"""Migrate concrete Ceph metadata inputs and read-back assertions to real SDK operations."""
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def write_metadata(p,values,content=b"bar",content_type="application/octet-stream"):
    p.ctx.body.write_bytes(content)
    p.call("put_metadata",body_path=str(p.ctx.body),metadata=values,content_type=content_type)


def check_metadata(p,values,content=b"bar",content_type="application/octet-stream"):
    for op in ("head_metadata","get_metadata"):
        result=p.call(op,output_path=str(p.ctx.output))
        assert_equal(result["metadata"],values)
        assert_equal(result["content_type"],content_type)
        if "size" in result:assert_equal(result["size"],len(content))
    assert_equal(p.ctx.output.read_bytes(),content)


def set_get(p,value,old=None):
    """Read each producer's metadata before overwriting, including empty values which must remain present."""
    if old is not None:
        write_metadata(p,{"meta1":old});check_metadata(p,{"meta1":old})
    write_metadata(p,{"meta1":value});check_metadata(p,{"meta1":value})


def replaced_on_put(p):
    write_metadata(p,{"meta1":"bar"});check_metadata(p,{"meta1":"bar"})
    p.put(b"bar")
    # A plain PUT must not retain any old user metadata; SDK default Content-Type may vary.
    result=p.call("get_metadata",output_path=str(p.ctx.output))
    assert_equal(result["metadata"],{})
    assert_equal(p.ctx.output.read_bytes(),b"bar")


CASES=(
 case("ceph_metadata_value","写入后读取元数据","meta1 原值 mymeta 在 HEAD/GET 中保留","put_metadata head_metadata get_metadata",lambda p:set_get(p,"mymeta"),"test_object_set_get_metadata_none_to_good",CEPH),
 case("ceph_metadata_empty","空元数据值","meta1 键存在且值为空串，不能丢失该键","put_metadata head_metadata get_metadata",lambda p:set_get(p,""),"test_object_set_get_metadata_none_to_empty",CEPH),
 case("ceph_metadata_overwrite_empty","元数据覆盖为空值","旧值可读，覆盖后键仍存在且值为空","put_metadata head_metadata get_metadata",lambda p:set_get(p,"","oldmeta"),"test_object_set_get_metadata_overwrite_to_empty",CEPH),
 case("ceph_metadata_plain_put_replaces","普通覆盖写清除旧元数据","两次写入均为 bar，普通写入后，旧 meta1 不得残留","put put_metadata head_metadata get_metadata",replaced_on_put,"test_object_metadata_replaced_on_put",CEPH),
)


def register(registry):
    registry.add_suite(Suite("ceph_metadata",frozenset().union(*(c.operations for c in CASES)),
        lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES)))
