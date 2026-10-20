"""SDK-neutral translations of concrete Ceph listing, Range and multipart scenarios."""
import hashlib
import random

from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH


def listing(p,names,options,keys,prefixes=(),truncated=False):
    for name in names:p.put(name.encode(),name)
    args={"prefix":p.ctx.prefix+options.get("prefix",""),"max_keys":options.get("max_keys",1000)}
    if "marker" in options:args["marker"]=p.ctx.prefix+options["marker"]
    if "delimiter" in options:args["delimiter"]=options["delimiter"]
    result=p.call("list_page",**args)
    assert_equal(result["keys"],[p.ctx.prefix+key for key in keys])
    assert_equal(result["common_prefixes"],[p.ctx.prefix+prefix for prefix in prefixes])
    assert_equal(result["truncated"],truncated)
    # The SDK must preserve actual metadata; some native C callbacks do not expose echoed request fields.
    if "prefix" in result:assert_equal(result["prefix"],args["prefix"])
    if "delimiter" in result:assert_equal(result["delimiter"],options.get("delimiter",""))
    if "max_keys" in result:assert_equal(result["max_keys"],args["max_keys"])


def token_walk(p):
    names=["bar","baz","foo"]
    for name in names:p.put(name.encode(),name)
    first=p.call("list_page",prefix=p.ctx.prefix,max_keys=2)
    assert_equal(first["keys"],[p.ctx.prefix+"bar",p.ctx.prefix+"baz"])
    assert_equal(first["truncated"],True)
    token=first["next_token"]
    if not isinstance(token,str) or not token:raise AssertionError("missing continuation token")
    second=p.call("list_page",prefix=p.ctx.prefix,max_keys=2,token=token)
    assert_equal(second["keys"],[p.ctx.prefix+"foo"])
    assert_equal(second["truncated"],False)


def multipart_negative(p,mode):
    upload=p.call("mp_create")["upload_id"]
    try:
        data=b"x"*max(1,min(10*1024,p.ctx.workload["part_size"]//2))
        p.ctx.body.write_bytes(data)
        part=p.call("mp_part",upload_id=upload,part_number=1,body_path=str(p.ctx.body))
        parts=[{"part_number":1,"etag":part["etag"]}]
        expected="InvalidPart"
        if mode=="missing":parts[0]["part_number"]=9999
        elif mode=="bad_etag":parts[0]["etag"]="ffffffffffffffffffffffffffffffff"
        elif mode=="small":
            p.ctx.body.write_bytes(b"tail")
            tail=p.call("mp_part",upload_id=upload,part_number=2,body_path=str(p.ctx.body))
            parts.append({"part_number":2,"etag":tail["etag"]});expected="EntityTooSmall"
        response=p.raw("mp_complete",upload_id=upload,parts=parts)
        assert_equal(response["ok"],False)
        assert_equal(response.get("error_kind"),"local_error" if p.ctx.worker.info.get("api")=="local" else "sdk_error")
        assert_equal(response.get("error_code"),expected)
        if "status_code" in response:assert_equal(response["status_code"],400)
        p.missing("head")
    finally:p.raw("mp_abort",upload_id=upload)


def invalid_range(p,empty=False):
    p.put(b"" if empty else b"test")
    response=p.raw("get",offset=0 if empty else 4,length=1,output_path=str(p.ctx.output))
    assert_equal(response["ok"],False)
    assert_equal(response.get("error_code"),"InvalidRange")
    if "status_code" in response:assert_equal(response["status_code"],416)


def range_pattern(p,size):
    expected=random.Random(431+size).randbytes(size)
    p.put(expected)
    for offset,length in ((0,1),(size-1,1),(0,size),(size//2,size-size//2)):
        assert_equal(p.read(offset=offset,length=length),expected[offset:offset+length])


# Literal datasets/expected key groups are taken from the referenced upstream scenarios.
LIST_CASES=(
    ("empty",[],{},[],[],False,"test_bucket_list_empty"),
    ("many_first",["foo","bar","baz"],{"max_keys":2},["bar","baz"],[],True,"test_bucket_listv2_many"),
    ("many_after",["foo","bar","baz"],{"max_keys":2,"marker":"baz"},["foo"],[],False,"test_bucket_listv2_many"),
    ("delimiter_basic",["foo/bar","foo/bar/xyzzy","quux/thud","asdf"],{"delimiter":"/"},["asdf"],["foo/","quux/"],False,"test_bucket_listv2_delimiter_basic"),
    ("prefix_basic",["foo/bar","foo/baz","quux"],{"prefix":"foo/"},["foo/bar","foo/baz"],[],False,"test_bucket_listv2_prefix_basic"),
    ("prefix_alt",["bar","baz","foo"],{"prefix":"ba"},["bar","baz"],[],False,"test_bucket_listv2_prefix_alt"),
    ("prefix_empty",["foo/bar","foo/baz","quux"],{"prefix":""},["foo/bar","foo/baz","quux"],[],False,"test_bucket_listv2_prefix_empty"),
    ("prefix_missing",["foo/bar","foo/baz","quux"],{"prefix":"d"},[],[],False,"test_bucket_listv2_prefix_not_exist"),
    ("delimiter_missing",["bar","baz","cab","foo"],{"delimiter":"/"},["bar","baz","cab","foo"],[],False,"test_bucket_listv2_delimiter_not_exist"),
    ("maxkeys_zero",["bar","baz","foo"],{"max_keys":0},[],[],False,"test_bucket_listv2_maxkeys_zero"),
    ("maxkeys_one",["bar","baz","foo"],{"max_keys":1},["bar"],[],True,"test_bucket_listv2_maxkeys_one"),
    ("marker_after",["bar","baz","foo"],{"marker":"zzz"},[],[],False,"test_bucket_listv2_startafter_after_list"),
    ("marker_between",["bar","baz","foo"],{"marker":"bat"},["baz","foo"],[],False,"test_bucket_listv2_startafter_not_in_list"),
    ("prefix_delimiter",["foo/bar","foo/baz","foo/quux/one","quux"],{"prefix":"foo/","delimiter":"/"},["foo/bar","foo/baz"],["foo/quux/"],False,"test_bucket_listv2_prefix_delimiter_basic"),
    ("delimiter_empty",["foo/bar","foo/baz","quux"],{"delimiter":""},["foo/bar","foo/baz","quux"],[],False,"test_bucket_listv2_delimiter_empty"),
)

# Preserve concrete upstream vectors instead of synthesizing one generic prefix check.
LIST_CASES+=(
    ("delimiter_alt",["bar","baz","cab","foo"],{"delimiter":"a"},["foo"],["ba","ca"],False,"test_bucket_listv2_delimiter_alt"),
    ("delimiter_percent",["b%ar","b%az","c%ab","foo"],{"delimiter":"%"},["foo"],["b%","c%"],False,"test_bucket_listv2_delimiter_percentage"),
    ("delimiter_space",["b ar","b az","c ab","foo"],{"delimiter":" "},["foo"],["b ","c "],False,"test_bucket_listv2_delimiter_whitespace"),
    ("delimiter_dot",["b.ar","b.az","c.ab","foo"],{"delimiter":"."},["foo"],["b.","c."],False,"test_bucket_listv2_delimiter_dot"),
    ("delimiter_newline",["bar","baz","cab","foo"],{"delimiter":"\n"},["bar","baz","cab","foo"],[],False,"test_bucket_listv2_delimiter_unreadable"),
    ("prefix_trailing_delimiter",["asdf/"],{"prefix":"asdf/","delimiter":"/"},["asdf/"],[],False,"test_bucket_listv2_delimiter_prefix_ends_with_delimiter"),
    ("prefix_newline",["foo/bar","foo/baz","quux"],{"prefix":"\n"},[],[],False,"test_bucket_listv2_prefix_unreadable"),
    ("prefix_delimiter_alt",["bar","bazar","cab","foo"],{"prefix":"ba","delimiter":"a"},["bar"],["baza"],False,"test_bucket_listv2_prefix_delimiter_alt"),
    ("prefix_delimiter_missing_prefix",["b/a/r","b/a/c","b/a/g","g"],{"prefix":"/","delimiter":"d"},[],[],False,"test_bucket_listv2_prefix_delimiter_prefix_not_exist"),
    ("prefix_delimiter_missing_delimiter",["b/a/c","b/a/g","b/a/r","g"],{"prefix":"b","delimiter":"z"},["b/a/c","b/a/g","b/a/r"],[],False,"test_bucket_listv2_prefix_delimiter_delimiter_not_exist"),
    ("prefix_delimiter_missing_both",["b/a/c","b/a/g","b/a/r","g"],{"prefix":"y","delimiter":"z"},[],[],False,"test_bucket_listv2_prefix_delimiter_prefix_delimiter_not_exist"),
)


def delimiter_page_matrix(p,underscore=False):
    """Migrate the upstream page-size 1/2 walks, including pages containing only common prefixes."""
    names=(["_obj1_","_under1/bar","_under1/baz/xyzzy","_under2/thud","_under2/bla"] if underscore
           else ["asdf","boo/bar","boo/baz/xyzzy","cquux/thud","cquux/bla"])
    for name in names:p.put(name.encode(),name)
    top="_obj1_" if underscore else "asdf"
    first="_under1/" if underscore else "boo/"
    second="_under2/" if underscore else "cquux/"
    walks=[("",1,[([top],[],True),([],[first],True),([],[second],False)]),
           ("",2,[([top],[first],True),([],[second],False)]),
           (first,1,[([first+"bar"],[],True),([],[first+"baz/"],False)]),
           (first,2,[([first+"bar"],[first+"baz/"],False)])]
    for prefix,limit,pages in walks:
        token=None;seen=set()
        for keys,prefixes,truncated in pages:
            args={"prefix":p.ctx.prefix+prefix,"delimiter":"/","max_keys":limit}
            if token is not None:args["token"]=token
            response=p.call("list_page",**args)
            assert_equal(response["keys"],[p.ctx.prefix+k for k in keys])
            assert_equal(response["common_prefixes"],[p.ctx.prefix+k for k in prefixes])
            assert_equal(response["truncated"],truncated)
            if truncated:
                token=response.get("next_token")
                if not token or token in seen:raise AssertionError("missing or repeated continuation token")
                seen.add(token)


CASES=tuple(case("ceph_list_"+name,"Ceph 列举参数组合 "+name,"返回键、公共前缀与截断状态准确",
                "put list_page",lambda p,names=names,opts=opts,keys=keys,prefixes=prefixes,truncated=truncated:listing(p,names,opts,keys,prefixes,truncated),
                reference,CEPH) for name,names,opts,keys,prefixes,truncated,reference in LIST_CASES)
CASES+=(case("ceph_list_token_walk","续传令牌遍历","第二页准确接续且最终不截断","put list_page",token_walk,"test_bucket_listv2_continuationtoken",CEPH),)
CASES+=tuple(case("ceph_multipart_"+mode,"分块完成负面场景 "+mode,"服务端拒绝并返回具体错误码，不能发布对象",
                 "mp_create mp_part mp_complete mp_abort head",lambda p,mode=mode:multipart_negative(p,mode),reference,CEPH)
             for mode,reference in (("missing","test_multipart_upload_missing_part"),("bad_etag","test_multipart_upload_incorrect_etag"),("small","test_multipart_upload_size_too_small")))
CASES+=tuple(case("ceph_range_size_"+str(size),"不同长度的二进制对象 Range","首尾、完整范围和后半段字节正确",
                 "put get",lambda p,size=size:range_pattern(p,size),"test_ranged_request_response_code; translated size matrix",CEPH) for size in (1,2,17,4096,65537))
CASES+=(case("ceph_range_invalid_start","Range 起点等于对象大小","失败且错误码 InvalidRange；可观测 HTTP 状态应为 416","put get",invalid_range,"test_ranged_request_invalid_range",CEPH),
        case("ceph_range_empty","空对象 Range 的严格错误语义","失败且错误码 InvalidRange；可观测 HTTP 状态应为 416","put get",lambda p:invalid_range(p,True),"test_ranged_request_empty_object",CEPH))

CASES+=tuple(case("ceph_list_delimiter_walk"+("_underscore" if underscore else ""),
                 "混合对象与公共前缀的四组分页遍历","页大小 1/2、前缀筛选、纯公共前缀页与最终截断状态准确",
                 "put list_page",lambda p,underscore=underscore:delimiter_page_matrix(p,underscore),
                 "test_bucket_listv2_delimiter_prefix_underscore" if underscore else "test_bucket_listv2_delimiter_prefix",CEPH)
             for underscore in (False,True))


def register(registry):
    registry.add_suite(Suite("ceph_object_boundaries",frozenset().union(*(c.operations for c in CASES)),
        lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
