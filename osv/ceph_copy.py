"""Migrate Ceph same-bucket copy, metadata preservation/replacement and rejection scenarios."""
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def copied_bytes(p,data):
    p.put(data,"source")
    p.call("copy",source_key=p.ctx.prefix+"source")
    assert_equal(p.read(),data)
    assert_equal(p.call("head")["size"],len(data))


def copy_metadata(p,replacing=False):
    """Retain both upstream size vectors and verify actual copied payload, type, metadata and size."""
    for size in (3,1024*1024):
        payload=bytes(size);p.ctx.body.write_bytes(payload)
        original={"key1":"value1","key2":"value2"}
        p.call("put_metadata","source",body_path=str(p.ctx.body),metadata=original,content_type="audio/ogg")
        expected={"key3":"value3","key2":"value2"} if replacing else original
        content_type="audio/mpeg" if replacing else "audio/ogg"
        args={"source_key":p.ctx.prefix+"source"}
        if replacing:args.update(metadata=expected,content_type=content_type)
        p.call("copy_replace" if replacing else "copy",**args)
        result=p.call("get_metadata",output_path=str(p.ctx.output))
        assert_equal(result["metadata"],expected)
        assert_equal(result["content_type"],content_type)
        assert_equal(result["size"],size)
        assert_equal(p.ctx.output.read_bytes(),payload)


def content_type(p):
    p.ctx.body.write_bytes(b"foo")
    p.call("put_metadata","source",body_path=str(p.ctx.body),metadata={},content_type="text/bla")
    p.call("copy",source_key=p.ctx.prefix+"source")
    result=p.call("get_metadata",output_path=str(p.ctx.output))
    assert_equal(result["content_type"],"text/bla")
    assert_equal(p.ctx.output.read_bytes(),b"foo")


def self_copy(p,replacing):
    p.put(b"foo")
    args={"source_key":p.ctx.prefix+"object"}
    if replacing:
        p.call("copy_replace",**args,metadata={"foo":"bar"},content_type="application/octet-stream")
        response=p.call("get_metadata",output_path=str(p.ctx.output))
        assert_equal(response["metadata"],{"foo":"bar"})
        assert_equal(p.ctx.output.read_bytes(),b"foo")
    else:
        response=p.raw("copy",**args)
        assert_equal(response["ok"],False)
        assert_equal(response.get("error_code"),"InvalidRequest")
        if "status_code" in response:assert_equal(response["status_code"],400)
        assert_equal(p.read(),b"foo")


def missing_source(p):
    response=p.raw("copy",source_key=p.ctx.prefix+"foo123bar")
    assert_equal(response["ok"],False)
    assert_equal(response.get("error_kind"),"not_found")
    if "status_code" in response:assert_equal(response["status_code"],404)
    p.missing("head")


CASES=(
 case("ceph_copy_zero","复制零字节对象","复制对象长度为零且可完整读取","put copy get head",lambda p:copied_bytes(p,b""),"test_object_copy_zero_size",CEPH),
 case("ceph_copy_16m","复制 16 MiB 对象","目的对象长度正确且全部字节一致","put copy get head",lambda p:copied_bytes(p,bytes(16*1024*1024)),"test_object_copy_16m",CEPH),
 case("ceph_copy_same_bucket","同桶复制","复制后的完整内容为 foo","put copy get head",lambda p:copied_bytes(p,b"foo"),"test_object_copy_same_bucket",CEPH),
 case("ceph_copy_content_type","复制保留 Content-Type","text/bla 与原始 foo 内容均准确","put_metadata copy get_metadata",content_type,"test_object_copy_verify_contenttype",CEPH),
 case("ceph_copy_self_rejected","无元数据变更的原地复制","实际拒绝 InvalidRequest，可观测状态为 400，原对象不变","put copy get",lambda p:self_copy(p,False),"test_object_copy_to_itself",CEPH),
 case("ceph_copy_self_metadata","原地复制替换元数据","元数据为 foo=bar 且对象内容不变","put copy_replace get_metadata",lambda p:self_copy(p,True),"test_object_copy_to_itself_with_metadata",CEPH),
 case("ceph_copy_keep_metadata","复制保留元数据","3 字节和 1 MiB 两组均保留类型、元数据与长度","put_metadata copy get_metadata",copy_metadata,"test_object_copy_retaining_metadata",CEPH),
 case("ceph_copy_replace_metadata","复制替换元数据","两组大小均得到新 metadata 和 audio/mpeg，不混入旧字段","put_metadata copy_replace get_metadata",lambda p:copy_metadata(p,True),"test_object_copy_replacing_metadata",CEPH),
 case("ceph_copy_missing_key","复制不存在的来源对象","真实 not_found，不能发布目标对象","copy head",missing_source,"test_object_copy_key_not_found",CEPH),
)


def register(registry):
    registry.add_suite(Suite("ceph_copy",frozenset().union(*(c.operations for c in CASES)),
        lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES)))
