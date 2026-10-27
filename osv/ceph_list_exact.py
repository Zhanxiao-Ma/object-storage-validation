"""Translate exact Ceph V1/V2 listing vectors in genuinely empty owned buckets."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

OPS={1:"list_v1_page",2:"list_v2_observed"}
FOUR=["bar","baz","cab","foo"]
PREFIX=["foo/bar","foo/baz","quux"]
MARKERS=["bar","baz","foo","quxx"]

# name, literal source keys, request, expected keys/groups, explicit scalar checks, absent fields.
VECTORS=(
 ("delimiter_basic",["foo/bar","foo/bar/xyzzy","quux/thud","asdf"],{"delimiter":"/"},["asdf"],["foo/","quux/"],{"delimiter":"/"},()),
 ("delimiter_alt",FOUR,{"delimiter":"a"},["foo"],["ba","ca"],{"delimiter":"a"},()),
 ("delimiter_percentage",["b%ar","b%az","c%ab","foo"],{"delimiter":"%"},["foo"],["b%","c%"],{"delimiter":"%"},()),
 ("delimiter_whitespace",["b ar","b az","c ab","foo"],{"delimiter":" "},["foo"],["b ","c "],{"delimiter":" "},()),
 ("delimiter_dot",["b.ar","b.az","c.ab","foo"],{"delimiter":"."},["foo"],["b.","c."],{"delimiter":"."},()),
 ("delimiter_unreadable",FOUR,{"delimiter":"\n"},FOUR,[],{"delimiter":"\n"},()),
 ("delimiter_empty",FOUR,{"delimiter":""},FOUR,[],{},("Delimiter",)),
 ("delimiter_none",FOUR,{},FOUR,[],{},("Delimiter",)),
 ("delimiter_not_exist",FOUR,{"delimiter":"/"},FOUR,[],{"delimiter":"/"},()),
 ("prefix_basic",PREFIX,{"prefix":"foo/"},["foo/bar","foo/baz"],[],{"prefix":"foo/"},()),
 ("prefix_alt",["bar","baz","foo"],{"prefix":"ba"},["bar","baz"],[],{"prefix":"ba"},()),
 ("prefix_empty",PREFIX,{"prefix":""},PREFIX,[],{"prefix":""},()),
 ("prefix_none",PREFIX,{"prefix":""},PREFIX,[],{"prefix":""},()),
 ("prefix_not_exist",PREFIX,{"prefix":"d"},[],[],{"prefix":"d"},()),
 ("prefix_unreadable",PREFIX,{"prefix":"\n"},[],[],{"prefix":"\n"},()),
 ("prefix_delimiter_basic",["foo/bar","foo/baz/xyzzy","quux/thud","asdf"],{"prefix":"foo/","delimiter":"/"},["foo/bar"],["foo/baz/"],{"prefix":"foo/","delimiter":"/"},()),
 ("prefix_delimiter_alt",["bar","bazar","cab","foo"],{"prefix":"ba","delimiter":"a"},["bar"],["baza"],{"prefix":"ba","delimiter":"a"},()),
 ("prefix_delimiter_prefix_not_exist",["b/a/r","b/a/c","b/a/g","g"],{"prefix":"/","delimiter":"d"},[],[],{},()),
 ("prefix_delimiter_delimiter_not_exist",["b/a/c","b/a/g","b/a/r","g"],{"prefix":"b","delimiter":"z"},["b/a/c","b/a/g","b/a/r"],[],{},()),
 ("prefix_delimiter_prefix_delimiter_not_exist",["b/a/c","b/a/g","b/a/r","g"],{"prefix":"y","delimiter":"z"},[],[],{},()),
 ("maxkeys_zero",MARKERS,{"max_keys":0},[],None,{"truncated":False},()),
 ("maxkeys_none",MARKERS,{},MARKERS,None,{"truncated":False,"max_keys":1000},()),
 ("encoding_basic",["foo+1/bar","foo/bar/xyzzy","quux ab/thud","asdf+b"],{"delimiter":"/","encoding_type":"url"},["asdf%2Bb"],["foo%2B1/","foo/","quux%20ab/"],{"delimiter":"/"},()),
)

def populate(bucket,names):
    for name in names:bucket.put(name,name.encode())

def checked_page(bucket,version,options,keys=None,prefixes=None,checks=None,absent=()):
    result=bucket.call(OPS[version],**options)
    if keys is not None:assert_equal(("keys",result["keys"]),("keys",[bucket.key(key) for key in keys]))
    if prefixes is not None:assert_equal(("common_prefixes",result["common_prefixes"]),("common_prefixes",[bucket.key(key) for key in prefixes]))
    for name,value in (checks or {}).items():
        if name=='prefix' or name in ('marker','start_after') and value:value=bucket.key(value)
        assert_equal((name,result.get(name)),(name,value))
    for name in absent:assert_equal((name,name in result["fields"]),(name,False))
    return result

def vector(p,version,data):
    """Preserve source datasets and every explicit field assertion, without replacing empty request prefixes with a case namespace."""
    name,names,options,keys,prefixes,checks,absent=data
    with BucketScope(p) as bucket:
        populate(bucket,names)
        result=checked_page(bucket,version,options,keys,prefixes,checks,absent)
        if version==2 and name=="delimiter_basic":assert_equal(result["key_count"],3)

def many(p,version,one):
    """Keep both upstream requests, including the second read after a literal marker/start-after rather than a continuation token."""
    names=MARKERS if one else ["foo","bar","baz"]
    with BucketScope(p) as bucket:
        populate(bucket,names)
        checked_page(bucket,version,{"max_keys":1 if one else 2},["bar"] if one else ["bar","baz"],checks={"truncated":True})
        options={"marker" if version==1 else "start_after":"bar" if one else "baz"}
        if not one:options["max_keys"]=2
        checked_page(bucket,version,options,["baz","foo","quxx"] if one else ["foo"],checks={"truncated":False})

def delimiter_walk(p,version,underscore=False,trailing=False):
    """Replay all source page-size/prefix walks with exact V1 NextMarker and absent final V2 tokens."""
    names=(["_obj1_","_under1/bar","_under1/baz/xyzzy","_under2/thud","_under2/bla"] if underscore else ["asdf","boo/bar","boo/baz/xyzzy","cquux/thud","cquux/bla"])
    top,first,second=("_obj1_","_under1/","_under2/") if underscore else ("asdf","boo/","cquux/")
    walks=[("",1,[([top],[],True,top),([],[first],True,first),([],[second],False,None)]),
           ("",2,[([top],[first],True,first),([],[second],False,None)]),
           (first,1,[([first+"bar"],[],True,first+"bar"),([],[first+"baz/"],False,None)]),
           (first,2,[([first+"bar"],[first+"baz/"],False,None)])]
    if trailing:names=["asdf/"];walks=[("asdf/",1000,[(["asdf/"],[],False,None)])]
    with BucketScope(p) as bucket:
        populate(bucket,names)
        for prefix,maximum,pages in walks:
            cursor=None
            for keys,groups,truncated,next_marker in pages:
                options={"prefix":prefix,"delimiter":"/","max_keys":maximum}
                if version==1:options["marker"]=cursor or ""
                elif cursor is None:options["start_after"]=""
                else:options["token"]=cursor
                result=checked_page(bucket,version,options,keys,groups,{"truncated":truncated})
                if version==1:assert_equal(result["next_marker"],bucket.key(next_marker) if next_marker is not None else None);cursor=result["next_marker"]
                else:
                    if not truncated:assert_equal(result["next_token"],None)
                    cursor=result["next_token"]

MARKER_VECTORS=(("marker_none",1,{},None,{"marker":""}),
 ("marker_empty",1,{"marker":""},MARKERS,{"marker":"","truncated":False}),
 ("marker_unreadable",1,{"marker":"\n"},MARKERS,{"marker":"\n","truncated":False}),
 ("marker_not_in_list",1,{"marker":"blah"},["foo","quxx"],{"marker":"blah"}),
 ("marker_after_list",1,{"marker":"zzz"},[],{"marker":"zzz","truncated":False}),
 ("startafter_unreadable",2,{"start_after":"\n"},MARKERS,{"start_after":"\n","truncated":False}),
 ("startafter_not_in_list",2,{"start_after":"blah"},["foo","quxx"],{"start_after":"blah"}),
 ("startafter_after_list",2,{"start_after":"zzz"},None,{"start_after":"zzz","truncated":False}))

def marker_vector(p,data):
    """Observe the actual echoed marker/start-after, preserving the source empty and newline request values."""
    name,version,options,keys,checks=data
    with BucketScope(p) as bucket:
        populate(bucket,MARKERS);checked_page(bucket,version,options,keys,checks=checks)

def token_vector(p,mode):
    """Use real returned cursors, require exact echoed token and start-after fields, and preserve source empty-token behavior."""
    with BucketScope(p) as bucket:
        populate(bucket,MARKERS)
        if mode=="empty":checked_page(bucket,2,{"token":""},MARKERS,checks={"token":"","truncated":False});return
        options={"max_keys":1}
        if mode=="both":options["start_after"]="bar"
        first=bucket.call(OPS[2],**options)
        assert_equal("NextContinuationToken" in first["fields"],True)
        options={"token":first["next_token"]}
        if mode=="both":options["start_after"]="bar"
        result=checked_page(bucket,2,options,["foo","quxx"] if mode=="both" else ["baz","foo","quxx"],checks={"token":first["next_token"],"truncated":False})
        if mode=="both":assert_equal(result["start_after"],bucket.key("bar"))

def owner(p,mode):
    """Assert actual decoded Owner presence for true, false and omitted FetchOwner requests."""
    with BucketScope(p) as bucket:
        populate(bucket,PREFIX);options={} if mode=="defaultempty" else {"fetch_owner":mode=="notempty"}
        result=bucket.call(OPS[2],**options)
        assert_equal(("owner_present","owner" in result["objects"][0]),("owner_present",mode=="notempty"))

def basic_count(p):
    """Preserve the source five numeric object names and assert the actual KeyCount field."""
    with BucketScope(p) as bucket:
        populate(bucket,[str(i) for i in range(5)]);assert_equal(bucket.call(OPS[2])["key_count"],5)

def empty_bucket(p,distinct=False):
    """Create truly empty buckets; the distinct case writes only to the first bucket and verifies the second independently."""
    with BucketScope(p) as first:
        if not distinct:assert_equal(first.call(OPS[1])["keys"],[]);return
        first.put("asdf",b"str")
        with BucketScope(p) as second:assert_equal(second.call(OPS[1])["keys"],[])

def boundary_special(p):
    """Preserve the 1000 nested-key common prefix plus four following special keys from the V1 source regression."""
    with BucketScope(p) as bucket:
        populate(bucket,["0/"]+["0/"+str(i) for i in range(1000,1999)]+["1999","1999#","1999+","2000"])
        checked_page(bucket,1,{"delimiter":"/"},["1999","1999#","1999+","2000"],["0/"],{"delimiter":"/"})

def make_case(name,version,execute,reference,needs_put=True):
    return case("ceph_exact_list_v"+str(version)+"_"+name,"Ceph 原始 LIST V"+str(version)+" "+name,"独立桶中的原始参数、键/公共前缀、字段存在性及源断言均准确",SCOPE_OPERATIONS+" "+OPS[version]+(" put" if needs_put else ""),execute,reference,CEPH)

CASES=tuple(make_case(data[0],version,lambda p,version=version,data=data:vector(p,version,data),"test_bucket_list"+("v2" if version==2 else "")+"_"+data[0]) for version in (1,2) for data in VECTORS)
CASES+=tuple(make_case("maxkeys_one" if one else "many",version,lambda p,version=version,one=one:many(p,version,one),"test_bucket_list"+("v2" if version==2 else "")+"_"+("maxkeys_one" if one else "many")) for version in (1,2) for one in (False,True))
CASES+=tuple(make_case(name,version,lambda p,version=version,underscore=underscore,trailing=trailing:delimiter_walk(p,version,underscore,trailing),"test_bucket_list"+("v2" if version==2 else "")+"_"+name) for version in (1,2) for name,underscore,trailing in (("delimiter_prefix",False,False),("delimiter_prefix_underscore",True,False),("delimiter_prefix_ends_with_delimiter",False,True)))
CASES+=tuple(make_case(data[0],data[1],lambda p,data=data:marker_vector(p,data),"test_bucket_list"+("v2" if data[1]==2 else "")+"_"+data[0]) for data in MARKER_VECTORS)
CASES+=tuple(make_case("continuationtoken_"+mode,2,lambda p,mode=mode:token_vector(p,mode),reference) for mode,reference in (("empty","test_bucket_listv2_continuationtoken_empty"),("walk","test_bucket_listv2_continuationtoken"),("both","test_bucket_listv2_both_continuationtoken_startafter")))
CASES+=tuple(make_case("fetchowner_"+mode,2,lambda p,mode=mode:owner(p,mode),"test_bucket_listv2_fetchowner_"+mode) for mode in ("notempty","defaultempty","empty"))
CASES+=(make_case("key_count",2,basic_count,"test_basic_key_count"),make_case("bucket_empty",1,empty_bucket,"test_bucket_list_empty",needs_put=False),make_case("bucket_distinct",1,lambda p:empty_bucket(p,True),"test_bucket_list_distinct"),make_case("delimiter_not_skip_special",1,boundary_special,"test_bucket_list_delimiter_not_skip_special"))

def register(registry):
    registry.add_suite(Suite("ceph_exact_listing",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
