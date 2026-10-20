"""Preserve Ceph's lifecycle stages; accelerated service time is an explicit prerequisite, never simulated by the runner."""
from contextlib import ExitStack
from datetime import datetime,timezone
import math
import re
import time
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw
from .ceph_versioning import configure,create_versions
from .ceph_multipart import uploading
from .ceph_state_boundaries import header
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH


def interval(p):
    value=p.ctx.workload.get("lifecycle_debug_interval_seconds")
    if value is None:raise CaseNotRun("Ceph source requires a service already configured with accelerated lifecycle days; a client wait cannot enable that feature","workload.lifecycle_debug_interval_seconds: confirmed accelerated service interval")
    try:valid=type(value) in (int,float) and math.isfinite(value) and value>0
    except OverflowError:valid=False
    if not valid:
        raise ValueError("lifecycle_debug_interval_seconds must be finite and positive")
    return value

def wait(seconds):
    # Preserve the complete source wait, including values longer than a minute.
    # Bounded sleeps keep cancellation responsive without shortening service time.
    deadline=time.monotonic()+seconds
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:return
        time.sleep(min(remaining,30))

def lifecycle(bucket,rules,check_status=False):
    result=api(bucket,"PutBucketLifecycleConfiguration",LifecycleConfiguration={"Rules":rules})
    if check_status:assert_equal(result["status_code"],200)
    return result

def objects(bucket,v2=False):
    result=bucket.call("list_v2_observed" if v2 else "list_v1_page")
    if "Contents" not in result["fields"]:raise KeyError("Contents")
    return result["objects"]

def versions(bucket,field="Versions",optional=False):
    result=bucket.call("versions_list")
    if field not in result["fields"]:
        if optional:return []
        raise KeyError(field)
    return result["versions" if field=="Versions" else "delete_markers"]

def put_http(bucket,p,key,data):
    p.ctx.body.write_bytes(data)
    result=bucket.call("put_http",key=key,body_path=str(p.ctx.body));assert_equal(result["status_code"],200)
    return result

def tags(bucket,key,pairs):
    result=bucket.call("tags_put",key=key,tags=[{"key":key,"value":value} for key,value in pairs])
    assert_equal(result["status_code"],200)

def prefix_counts(p,v2=False):
    clock=interval(p)
    with BucketScope(p) as bucket:
        for key in ("expire1/foo","expire1/bar","keep2/foo","keep2/bar","expire3/foo","expire3/bar"):bucket.put(key,key.encode())
        lifecycle(bucket,[{"ID":"rule1","Expiration":{"Days":1},"Prefix":"expire1/","Status":"Enabled"},{"ID":"rule2","Expiration":{"Days":5},"Prefix":"expire3/","Status":"Enabled"}])
        initial=objects(bucket,v2)
        wait(3*clock);first=objects(bucket,v2)
        wait(clock);second=objects(bucket,v2)
        wait(3*clock);third=objects(bucket,v2)
        assert_equal(len(initial),6);assert_equal(len(first),4);assert_equal(len(second),4);assert_equal(len(third),2)

def enabled_version(p):
    clock=interval(p)
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled");create_versions(bucket,p,"test1/a",1)
        bucket.call("delete_version",key="test1/a")
        lifecycle(bucket,[{"ID":"rule1","Expiration":{"Days":1},"Prefix":"test1/","Status":"Enabled"}])
        wait(3*clock)
        result=bucket.call("versions_list")
        assert_equal("Versions" in result["fields"],True);assert_equal("DeleteMarkers" in result["fields"],True)
        assert_equal(len(result["versions"]),1);assert_equal(len(result["delete_markers"]),1)

def tag_filter(p,mode):
    clock=interval(p);versioned=mode=="versioned_two"
    scope=VersionScope(p) if versioned else BucketScope(p)
    with scope as bucket:
        if versioned:configure(bucket,"Enabled")
        bucket.put("days1/tom",b"tom_body");tags(bucket,"days1/tom",[("tom","sawyer")])
        condition={"Prefix":"days1/","Tag":{"Key":"tom","Value":"sawyer"}}
        if mode!="one":
            bucket.put("days1/huck",b"huck_body");tags(bucket,"days1/huck",[("tom","sawyer"),("huck","finn")])
            # Retain the source's simultaneous Prefix/Tag/And fields verbatim.
            condition["And"]={"Prefix":"days1","Tags":[{"Key":"huck","Value":"finn"}]}
        response=lifecycle(bucket,[{"Expiration":{"Days":1},"ID":"rule_tag1","Filter":condition,"Status":"Enabled"}],True)
        wait(3*clock)
        if mode=="one":
            # Pinned source reads the PUT lifecycle response, not a new listing.
            # Preserve this limitation instead of silently fixing its oracle.
            actual=response["data"].get("Contents",[])
            assert_equal(len(actual),0)
            return {"source_limitation":"source reads Contents from PutBucketLifecycleConfiguration response and performs no post-wait object listing"}
        assert_equal(len(objects(bucket)),1)

def count_versions_after(bucket,seconds):
    wait(seconds)
    try:return len(versions(bucket))
    except BaseException:return 0  # Preserve the pinned helper's bare except.

def noncurrent_filter(p,newer=False):
    clock=interval(p)
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        for number in range(10):
            put_http(bucket,p,"myobject_",("myobject_ v"+str(number)).encode())
            if not newer:tags(bucket,"myobject_",[("vidushi","mishra")])
        expiration={"NoncurrentDays":1,"NewerNoncurrentVersions":5} if newer else {"NoncurrentDays":4}
        condition={"Prefix":""} if newer else {"Prefix":"","Tag":{"Key":"vidushi","Value":"mishra"}}
        lifecycle(bucket,[{"NoncurrentVersionExpiration":expiration,"ID":"newer_noncurrent1" if newer else "rule_tag1","Filter":condition,"Status":"Enabled"}],True)
        assert_equal(count_versions_after(bucket,2*clock),6 if newer else 10)
        if not newer:assert_equal(count_versions_after(bucket,5*clock),1)

def size_filter(p,greater):
    clock=interval(p)
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        put_http(bucket,p,"myobject_small",b"b"*1000);put_http(bucket,p,"myobject_big",b"b"*3000)
        condition={"Prefix":"","ObjectSizeGreaterThan" if greater else "ObjectSizeLessThan":2000}
        lifecycle(bucket,[{"Expiration":{"Days":1},"ID":"object_gt1" if greater else "object_lt1","Filter":condition,"Status":"Enabled"}],True)
        wait((10 if greater else 2)*clock)
        actual=objects(bucket);assert_equal(len(actual),1)
        assert_equal(actual[0]["key"],"myobject_small" if greater else "myobject_big")

def expiration_date(p):
    clock=interval(p)
    with BucketScope(p) as bucket:
        for key in ("past/foo","future/bar"):bucket.put(key,key.encode())
        # Boto accepts these source date strings and serializes UTC-midnight timestamps.
        rules=[{"ID":"rule1","Expiration":{"Date":int(datetime(2015,1,1,tzinfo=timezone.utc).timestamp()*1000)},"Prefix":"past/","Status":"Enabled"},{"ID":"rule2","Expiration":{"Date":int(datetime(2030,1,1,tzinfo=timezone.utc).timestamp()*1000)},"Prefix":"future/","Status":"Enabled"}]
        lifecycle(bucket,rules);initial=objects(bucket)
        wait(3*clock);expired=objects(bucket)
        assert_equal(len(initial),2);assert_equal(len(expired),1)

def days_zero(p):
    with BucketScope(p) as bucket:
        for key in ("days0/foo","days0/bar"):bucket.put(key,key.encode())
        result=raw(bucket,"PutBucketLifecycleConfiguration",LifecycleConfiguration={"Rules":[{"Expiration":{"Days":0},"ID":"rule1","Prefix":"days0/","Status":"Enabled"}]})
        assert_equal(result.get("error_code","") if not result["ok"] else "","InvalidArgument")

def expiration_header(response,start,rule_id,days):
    try:value=header(response,"x-amz-expiration")
    except KeyError:return False
    try:from dateutil import parser
    except ImportError:raise CaseNotRun("the original Ceph expiration-header parser requires python-dateutil","python-dateutil") from None
    match=re.search(r'expiry-date="(.+)", rule-id="(.+)"',value)
    expiration=parser.parse(match.group(1))
    return (expiration.replace(tzinfo=None)-start).days==days and match.group(2)==rule_id

def header_case(p,mode):
    with BucketScope(p) as bucket:
        if mode in ("put","head"):
            now=datetime.utcnow()
            lifecycle(bucket,[{"ID":"rule1","Expiration":{"Days":1},"Prefix":"days1/","Status":"Enabled"}],True)
            response=put_http(bucket,p,"days1/foo",b"bar")
            if mode=="head":
                response=bucket.call("head_http",key="days1/foo");assert_equal(response["status_code"],200)
            assert_equal(expiration_header(response,now,"rule1",1),True);return
        condition={"Tag":{"Key":"key1","Value":"tag1"}} if mode=="tag" else {"And":{"Tags":[{"Key":"key1","Value":"tag1"},{"Key":"key5","Value":"tag6"}]}}
        lifecycle(bucket,[{"Filter":condition,"Status":"Enabled","Expiration":{"Days":1},"ID":"rule1"}])
        bucket.put("obj_key1",b"obj_key1_body")
        # These two source calls do not assert tagging status separately.
        bucket.call("tags_put",key="obj_key1",tags=[{"key":"key1","value":"tag1"},{"key":"key5","value":"tag5"}])
        response=bucket.call("head_http",key="obj_key1");assert_equal(response["status_code"],200)
        assert_equal(expiration_header(response,datetime.now(None),"rule1",1),mode=="tag")
        if mode=="tag":
            lifecycle(bucket,[{"Filter":{"Tag":{"Key":"key2","Value":"tag1"}},"Status":"Enabled","Expiration":{"Days":1},"ID":"rule1"}])
            response=bucket.call("head_http",key="obj_key1");assert_equal(response["status_code"],200)
            assert_equal(expiration_header(response,datetime.now(None),"rule1",1),False)

def history_expiration(p,mode):
    clock=interval(p)
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        if mode=="noncurrent":
            create_versions(bucket,p,"test1/a",3);create_versions(bucket,p,"test2/abc",3)
            initial=versions(bucket)
            lifecycle(bucket,[{"ID":"rule1","NoncurrentVersionExpiration":{"NoncurrentDays":2},"Prefix":"test1/","Status":"Enabled"}])
            wait(5*clock);expired=versions(bucket)
            assert_equal(len(initial),6);assert_equal(len(expired),4);return
        create_versions(bucket,p,"test1/a",1)
        if mode=="marker":create_versions(bucket,p,"test2/abc",1)
        bucket.call("delete_version",key="test1/a")
        if mode=="marker":bucket.call("delete_version",key="test2/abc")
        if mode=="marker":
            response=bucket.call("versions_list")
            assert_equal("Versions" in response["fields"],True);assert_equal("DeleteMarkers" in response["fields"],True)
            initial=response["versions"]+response["delete_markers"]
        expiration={"ExpiredObjectDeleteMarker":True} if mode=="marker" else {"Days":5}
        lifecycle(bucket,[{"ID":"rule1","NoncurrentVersionExpiration":{"NoncurrentDays":1},"Expiration":expiration,"Prefix":"test1/","Status":"Enabled"}])
        if mode=="marker":
            wait(7*clock);response=bucket.call("versions_list")
            assert_equal("Versions" in response["fields"],True);assert_equal("DeleteMarkers" in response["fields"],True)
            assert_equal(len(initial),4);assert_equal(len(response["versions"]+response["delete_markers"]),2)
        else:
            wait(2*clock);response=bucket.call("versions_list")
            assert_equal(len(response["versions"]) if "Versions" in response["fields"] else 0,0)
            assert_equal(len(response["delete_markers"]) if "DeleteMarkers" in response["fields"] else 0,1)
            wait(4*clock);response=bucket.call("versions_list")
            assert_equal(len(response["delete_markers"]) if "DeleteMarkers" in response["fields"] else 0,0)

def multipart_expiration(p):
    clock=interval(p)
    with BucketScope(p) as bucket,ExitStack() as pending:
        for key in ("test1/a","test2/"):pending.enter_context(uploading(bucket,p,key=key))
        initial=bucket.call("mp_list")["uploads"]
        lifecycle(bucket,[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","AbortIncompleteMultipartUpload":{"DaysAfterInitiation":2}}])
        wait(5*clock);expired=bucket.call("mp_list")["uploads"]
        assert_equal(len(initial),2);assert_equal(len(expired),1)

BASE=SCOPE_OPERATIONS+" api.PutBucketLifecycleConfiguration"
VBASE=VERSION_SCOPE_OPERATIONS+" api.PutBucketLifecycleConfiguration versioning_get versioning_put"
VECTORS=(
 ("prefix",prefix_counts,"test_lifecycle_expiration",BASE+" put list_v1_page"),
 ("prefix_v2",lambda p:prefix_counts(p,True),"test_lifecyclev2_expiration",BASE+" put list_v2_observed"),
 ("enabled_version",enabled_version,"test_lifecycle_expiration_versioning_enabled",VBASE+" put_version delete_version"),
 ("tag_one",lambda p:tag_filter(p,"one"),"test_lifecycle_expiration_tags1",BASE+" put tags_put"),
 ("tag_two",lambda p:tag_filter(p,"two"),"test_lifecycle_expiration_tags2",BASE+" put tags_put list_v1_page"),
 ("versioned_tag_two",lambda p:tag_filter(p,"versioned_two"),"test_lifecycle_expiration_versioned_tags2",VBASE+" put tags_put list_v1_page"),
 ("noncurrent_tag",noncurrent_filter,"test_lifecycle_expiration_noncur_tags1",VBASE+" put_http tags_put"),
 ("newer_noncurrent",lambda p:noncurrent_filter(p,True),"test_lifecycle_expiration_newer_noncurrent",VBASE+" put_http"),
 ("size_gt",lambda p:size_filter(p,True),"test_lifecycle_expiration_size_gt",VBASE+" put_http list_v1_page"),
 ("size_lt",lambda p:size_filter(p,False),"test_lifecycle_expiration_size_lt",VBASE+" put_http list_v1_page"),
 ("date",expiration_date,"test_lifecycle_expiration_date",BASE+" put list_v1_page"),
 ("days_zero",days_zero,"test_lifecycle_expiration_days0",BASE+" put"),
 ("header_put",lambda p:header_case(p,"put"),"test_lifecycle_expiration_header_put",BASE+" put_http"),
 ("header_head",lambda p:header_case(p,"head"),"test_lifecycle_expiration_header_head",BASE+" put_http head_http"),
 ("header_tag",lambda p:header_case(p,"tag"),"test_lifecycle_expiration_header_tags_head",BASE+" put tags_put head_http"),
 ("header_and_tags",lambda p:header_case(p,"and_tags"),"test_lifecycle_expiration_header_and_tags_head",BASE+" put tags_put head_http"),
 ("noncurrent",lambda p:history_expiration(p,"noncurrent"),"test_lifecycle_noncur_expiration",VBASE+" put_version"),
 ("marker",lambda p:history_expiration(p,"marker"),"test_lifecycle_deletemarker_expiration",VBASE+" put_version delete_version"),
 ("marker_days",lambda p:history_expiration(p,"marker_days"),"test_lifecycle_deletemarker_expiration_with_days_tag",VBASE+" put_version delete_version"),
 ("multipart",multipart_expiration,"test_lifecycle_multipart_expiration",BASE+" mp_create_metadata mp_abort_raw mp_list"),
)
LEGACY_PREFIX=frozenset(("prefix","prefix_v2","enabled_version","date","days_zero","header_put","header_head","noncurrent","marker","marker_days","multipart"))
CASES=tuple(case("ceph_lc_expiration_"+name,"Ceph 生命周期过期 "+name,"保留源生命周期规则、对象/版本/标签/标记及阶段全部断言；后台用例要求服务已配置加速且严格保留原等待倍数，headers和Days=0不依赖加速",operations+(" lifecycle_legacy_prefix" if name in LEGACY_PREFIX else ""),execute,reference,CEPH) for name,execute,reference,operations in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_lifecycle_expiration",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
