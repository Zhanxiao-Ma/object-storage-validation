"""Translate Ceph object-tagging count, length, replacement and deletion assertions."""
import random
import string
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def simple(count):
    return [{"key":str(i),"value":str(i)} for i in range(count)]

def long_tags(key_size,value_size):
    rng=random.Random(927)
    return [{"key":"".join(rng.choices(string.ascii_letters,k=key_size)),"value":"".join(rng.choices(string.ascii_letters,k=value_size))} for _ in range(10)]

def check_tags(actual,expected):
    """Compare complete tag sets without order dependence; duplicates and missing values fail."""
    def values(rows):
        result={}
        for row in rows:
            key,value=row["key"],row["value"]
            assert_equal(isinstance(key,str) and isinstance(value,str),True)
            assert_equal(key in result,False)
            result[key]=value
        return result
    assert_equal(values(actual),values(expected))

def tagging(p,mode):
    """Apply literal source tag vectors to a real stored object and preserve its actual HTTP/error verdict."""
    p.put(b"A"*(7*1024*1024))
    tags=simple(2)
    if mode=="max":tags=simple(10)
    elif mode=="excess":tags=simple(11)
    elif mode in ("max_size","key_overflow","value_overflow"):
        tags=long_tags(129 if mode=="key_overflow" else 128,257 if mode=="value_overflow" else 256)
    elif mode=="modify":tags=[{"key":"key","value":"val"},{"key":"key2","value":"val2"}]
    invalid=mode in ("excess","key_overflow","value_overflow")
    original=simple(2)
    if invalid:
        assert_equal(p.call("tags_put",tags=original)["status_code"],200)
        check_tags(p.call("tags_get")["tags"],original)
    result=p.raw("tags_put",tags=tags)
    if invalid:
        # Check state even when the invalid request was incorrectly accepted.
        check_tags(p.call("tags_get")["tags"],original)
        assert_equal(result["ok"],False)
        assert_equal(result.get("error_kind"),"local_error" if p.ctx.worker.info["api"]=="local" else "sdk_error")
        # S3 documents the count limit without assigning one exclusive error code.
        allowed={"InvalidTag","BadRequest","TooManyTaggings"} if mode=="excess" else {"InvalidTag"}
        assert_equal(result.get("error_code") in allowed,True)
        if p.ctx.worker.info["api"]!="local":assert_equal(result.get("status_code"),400)
        return
    assert_equal(result["ok"],True)
    assert_equal(result["status_code"],200)
    fetched=p.call("tags_get")["tags"]
    check_tags(fetched,tags)
    if mode=="modify":
        replacement=[{"key":"key3","value":"val3"}]
        assert_equal(p.call("tags_put",tags=replacement)["status_code"],200)
        check_tags(p.call("tags_get")["tags"],replacement)
    if mode=="delete":
        assert_equal(p.call("tags_delete")["status_code"],204)
        assert_equal(p.call("tags_get")["tags"],[])

VECTORS=(("get","test_get_obj_tagging"),("max","test_put_max_tags"),("excess","test_put_excess_tags"),("max_size","test_put_max_kvsize_tags"),("key_overflow","test_put_excess_key_tags"),("value_overflow","test_put_excess_val_tags"),("modify","test_put_modify_tags"),("delete","test_put_delete_tags"))
CASES=tuple(case("ceph_tags_"+mode,"Ceph 对象标签 "+mode,"验证标签边界、HTTP拒绝和非法覆盖后原标签保持不变","put tags_put tags_get"+(" tags_delete" if mode=="delete" else ""),lambda p,mode=mode:tagging(p,mode),reference,CEPH) for mode,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_tags",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
