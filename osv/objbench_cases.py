"""Objbench object checks with their actual data shapes and independent byte oracles."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import random
import time
from urllib.parse import unquote

from .scenarios import Suite, assert_equal, require_ok
from .suites import case, run_cases, JUICE


def bucket_create(p):
    p.call("bucket_create")
    p.call("bucket_create")


def hello_ranges(p):
    p.put(b"hello")
    for offset,length,expected in ((0,5,b"hello"),(0,1,b"h"),(4,1,b"o"),(2,3,b"llo"),(2,2,b"ll")):
        assert_equal(p.read(offset=offset,length=length),expected)


def start_overflow_warning(p):
    """Retain JuiceFS's empty-success preference and warning severity separately from strict raw-S3 error assertions."""
    p.put(b"hello")
    result=p.raw("get",offset=6,length=2,output_path=str(p.ctx.output))
    observed={"ok":result["ok"]}
    if result["ok"]:
        actual=p.ctx.output.read_bytes()
        # Successful content must still be correct; a nonempty response indicates an integrity failure.
        assert_equal(actual,b"")
        observed["bytes"]=0
    else:observed.update({key:result[key] for key in ("error_kind","error_code","status_code","error") if key in result})
    return {"source_expected":"successful read with empty bytes","source_severity":"warning",
            "source_matches":result["ok"],"observed":observed,
            "scope":"JuiceFS ObjectStorage abstraction; strict Ceph S3 InvalidRange is a separate case"}


def list_fields(p):
    before=int(time.time()*1000)-30000
    p.put(b"hello","test")
    page=p.call("list_page",prefix=p.ctx.prefix,max_keys=1)
    assert_equal(page["keys"],[p.ctx.prefix+"test"])
    assert_equal(len(page["objects"]),1)
    obj=page["objects"][0]
    assert_equal(obj["key"],p.ctx.prefix+"test")
    assert_equal(obj["size"],5)
    if type(obj.get("last_modified_ms")) is not int or not before<=obj["last_modified_ms"]<=time.time()*1000+30000:
        raise AssertionError("LIST last-modified absent or outside the 30-second window")
    head=p.call("head","test")
    assert_equal(head["key"],p.ctx.prefix+"test")
    assert_equal(head["size"],5)


def list_marker(p):
    p.put(b"hello","test")
    page=p.call("list_page",prefix=p.ctx.prefix,marker=p.ctx.prefix+"test2",max_keys=1)
    assert_equal(page["keys"],[])
    assert_equal(page["truncated"],False)


def list_sorted(p):
    names=[f"hashKey{n}" for n in range(100)]
    for name in names: p.put(b"hello",name)
    page=p.call("list_page",prefix=p.ctx.prefix+"hashKey",max_keys=100)
    assert_equal(page["keys"],sorted(p.ctx.prefix+name for name in names))
    assert_equal(page["truncated"],False)


def special_control(p):
    name="测试编码文件"+'{"name":"juicefs"}'+chr(31)+"%uFF081%uFF09.jpg"
    p.put(b"1",name)
    try:
        assert_equal(p.read(name),b"1")
        page=p.call("list_page",prefix=p.ctx.prefix,marker=p.ctx.prefix+"测试编码文件",encoding_type="url",max_keys=1)
        actual=page["keys"]
        # SDKs may expose URL-encoded XML keys or decode internally; compare their lossless representation.
        if actual != [p.ctx.prefix+name]: actual=[unquote(key) for key in actual]
        assert_equal(actual,[p.ctx.prefix+name])
    finally:
        # Do not leave XML-control keys for an unrelated unencoded LIST/cleanup operation.
        p.call("delete",name)



def large_put(p):
    size=p.ctx.workload["large_object_size"]
    block=random.Random(1024).randbytes(min(4*1024*1024,size))
    with p.ctx.body.open("wb") as stream:
        remaining=size
        while remaining:
            count=min(remaining,len(block)); stream.write(block[:count]); remaining-=count
    p.call("put",body_path=str(p.ctx.body))
    assert_equal(p.call("head")["size"],size)
    return f"bytes={size}; reference_bytes={256*1024*1024}; scaled={size!=256*1024*1024}"


def slash_key(p):
    p.put(b"1","slash_test_file/")
    assert_equal(p.read("slash_test_file/"),b"1")
    assert_equal(p.call("list_page",prefix=p.ctx.prefix)["keys"],[p.ctx.prefix+"slash_test_file/"])


def multipart_objbench(p):
    upload=p.call("mp_create")["upload_id"]
    size=p.ctx.workload["part_size"]
    seed=random.Random(1701).randbytes(size)
    contents=[seed,seed[1:]+seed[:1],seed[2:]+seed[:2]]
    parts={}
    completed=False
    children=[]
    try:
        def send(number,data,worker):
            path=p.ctx.directory/f"objbench-part-{number}.bin"
            path.write_bytes(data)
            result=require_ok(worker.call({"op":"mp_part","key":p.ctx.prefix+"object","upload_id":upload,
                                           "part_number":number,"body_path":str(path)}))
            return {"part_number":number,"etag":result["etag"]}
        for _ in range(3): children.append(p.ctx.worker.fork())
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures=[executor.submit(send,n+1,data,children[n]) for n,data in enumerate(contents)]
            for future in futures:
                part=future.result(); parts[part["part_number"]]=part
        contents[0]=seed+seed
        contents[2]=b"hello"
        parts[1]=send(1,contents[0],p.ctx.worker)
        parts[3]=send(3,contents[2],p.ctx.worker)
        p.call("mp_complete",upload_id=upload,parts=[parts[n] for n in (1,2,3)])
        completed=True
        assert_equal(hashlib.sha256(p.read()).digest(),hashlib.sha256(b"".join(contents)).digest())
    finally:
        for worker in children: worker.close()
        if not completed: p.raw("mp_abort",upload_id=upload)


CASES=(
    case("juicefs_range_start_overflow","JuiceFS 起点越界的 warning 预期","记录是否成功返回空内容；416 不得混记为符合 JuiceFS 预期",
         "put get",start_overflow_warning,"get partial object",JUICE,True),
    case("bucket_create_idempotent","创建桶操作重复调用","不存在则创建，已有桶不报错","bucket_create",bucket_create,"create a bucket"),
    case("hello_partial_ranges","原版 hello 四种部分读取组合","h/o/llo/ll 四组内容准确","put get",hello_ranges,"get partial object"),
    case("list_object_fields","列举键、大小、时间与 HEAD 信息","键和大小正确，修改时间在 30 秒窗口内","put list_page head",list_fields,"list objects; head an object"),
    case("list_after_marker","marker 超过最后一个键","返回空列表且不截断","put list_page",list_marker,"list objects"),
    case("list_sorted_100","100 个键按字典序完整列举","精确顺序和完整集合","put list_page",list_sorted,"list objects"),
    case("special_control_key","原版中文/JSON/控制字符/百分号键","内容与 URL 编码后的列举键无损","put get list_page delete",special_control,"special key"),
    case("large_object_put","原版 256 MiB 单对象上传","上传成功且 HEAD 大小准确；缩小参数单独记录","put head",large_put,"put a big object"),
    case("slash_suffixed_key","斜线结尾的对象键","上传、读取与列举均保留末尾斜线","put get list_page",slash_key,"put an empty object"),
    case("multipart_grow_shrink","并发三分块后首块加倍、末块缩短","完成后内容与最新分块拼接一致","mp_create mp_part mp_complete mp_abort get",multipart_objbench,"multipart upload"),
)


def register(registry):
    registry.add_suite(Suite("objbench_objects",frozenset().union(*(case.operations for case in CASES)),
        lambda ctx:run_cases(ctx,CASES),tuple(case.metadata() for case in CASES)))
