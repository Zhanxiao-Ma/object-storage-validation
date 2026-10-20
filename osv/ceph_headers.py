"""Translate Ceph object lifecycle and explicit ETag/cache/expiry response-field assertions."""
import time
from email.utils import parsedate_to_datetime
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def write_headers(p,mode):
    """Write literal bar through the real SDK and validate source HTTP/metadata fields, including second-resolution Expires."""
    p.ctx.body.write_bytes(b"bar");options={}
    if mode=="cache":options["cache_control"]="public, max-age=14400"
    if mode=="expires":options["expires_ms"]=int(time.time()*1000)+6000000
    result=p.call("put_http",body_path=str(p.ctx.body),**options)
    if mode=="etag":
        assert_equal(result["status_code"],200)
        assert_equal(result["etag"],chr(34)+"37b51d194a7513e45b56f6524f2d51f2"+chr(34))
        return
    head=p.call("head_http")
    if mode=="cache":assert_equal(head["http_headers"].get("cache-control"),"public, max-age=14400")
    else:
        expiry=parsedate_to_datetime(head["http_headers"]["expires"])
        assert_equal(int(expiry.timestamp()),options["expires_ms"]//1000)

def zero(p):
    """Mirror the source zero-byte PUT/HEAD invariant using actual storage."""
    p.put(b"")
    assert_equal(p.call("head")["size"],0)

def update(p):
    """Mirror the literal bar to soup overwrite/read/delete lifecycle without relying on another case."""
    p.put(b"bar");assert_equal(p.read(),b"bar")
    p.put(b"soup");assert_equal(p.read(),b"soup")
    p.call("delete")

CASES=tuple(case("ceph_write_"+mode,"Ceph 对象响应字段 "+mode,"保留源用例的实际 ETag/Cache-Control/Expires 断言","put_http"+(" head_http" if mode!="etag" else ""),lambda p,mode=mode:write_headers(p,mode),reference,CEPH)
    for mode,reference in (("etag","test_object_write_check_etag"),("cache","test_object_write_cache_control"),("expires","test_object_write_expires")))
CASES+=(case("ceph_zero_head","零字节对象 HEAD","实际 ContentLength 为零","put head",zero,"test_object_head_zero_bytes",CEPH),case("ceph_literal_update","对象写读更新删除","bar 与 soup 两轮读取均匹配独立输入，删除成功","put get delete",update,"test_object_write_read_update_read_delete",CEPH))

def register(registry):
    registry.add_suite(Suite("ceph_headers",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
