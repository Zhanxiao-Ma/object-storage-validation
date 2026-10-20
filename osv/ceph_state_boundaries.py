"""Keep Ceph version/ACL and authenticated/anonymous state boundaries as real SDK contracts."""
from email.utils import parsedate_to_datetime
import time
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_acl import actor,check_grants,grant,ALL
from .ceph_versioning import configure,create_versions,put
from .ceph_multipart import source_data,MIB,uploading
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH


def header(result,name):
    for key,value in result.get("http_headers",{}).items():
        if key.lower()==name.lower():return ",".join(value) if isinstance(value,list) else value
    raise KeyError("actual SDK response has no HTTP header "+name)

def conditional_read(p,mode):
    if mode in ("not_match","not_modified") and "sdk_error_message" not in p.ctx.worker.capabilities:
        raise CaseNotRun("source requires the actual SDK error message, without parsing or inventing a replacement","worker.sdk_error_message")
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        uploaded=bucket.call("put_http",key="foo",body_path=str(p.ctx.body))
        if mode=="if_match":options={"if_match":'"ABCORZ"'}
        elif mode=="not_match":options={"if_none_match":uploaded["etag"]}
        elif mode=="not_modified":
            observed=bucket.call("get_http",key="foo",output_path=str(p.ctx.output))
            etag=observed["etag"]
            after=(observed["last_modified_ms"]//1000+1)*1000
            time.sleep(1)
            options={"if_modified_since_ms":after}
        elif mode=="unmodified":options={"if_unmodified_since_ms":int(parsedate_to_datetime("Sat, 29 Oct 1994 19:43:31 GMT").timestamp()*1000)}
        else:raise ValueError(mode)
        response=bucket.raw("get_http",key="foo",output_path=str(p.ctx.output),**options)
        if mode in ("if_match","unmodified"):rejected(response,412,"PreconditionFailed")
        else:
            rejected(response,304)
            assert_equal(response["error_message"],"Not Modified")
            assert_equal(header(response,"etag"),etag if mode=="not_modified" else uploaded["etag"])

# auth, bucket ACL, object ACL, resource state, verb, status, error code, source
ACCESS=(
 ("anonymous","public-read","public-read","present","get",200,None,"test_object_raw_get"),
 ("anonymous","public-read","public-read","bucket_gone","get",404,"NoSuchBucket","test_object_raw_get_bucket_gone"),
 ("anonymous","public-read","public-read","bucket_gone","delete",404,"NoSuchBucket","test_object_delete_key_bucket_gone"),
 ("anonymous","public-read","public-read","object_gone","get",404,"NoSuchKey","test_object_raw_get_object_gone"),
 ("anonymous","private","public-read","present","get",200,None,"test_object_raw_get_bucket_acl"),
 ("anonymous","public-read","private","present","get",403,"AccessDenied","test_object_raw_get_object_acl"),
 ("main","public-read","public-read","present","get",200,None,"test_object_raw_authenticated"),
 ("main","private","public-read","present","get",200,None,"test_object_raw_authenticated_bucket_acl"),
 ("main","public-read","private","present","get",200,None,"test_object_raw_authenticated_object_acl"),
 ("main","public-read","public-read","bucket_gone","get",404,"NoSuchBucket","test_object_raw_authenticated_bucket_gone"),
 ("main","public-read","public-read","object_gone","get",404,"NoSuchKey","test_object_raw_authenticated_object_gone"),
)

def resource_access(p,identity,bucket_acl,object_acl,state,verb,status,code):
    with BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":bucket_acl}) as bucket:
        p.ctx.body.write_bytes(b"")
        bucket.call("put_acl",key="foo",body_path=str(p.ctx.body),acl=object_acl)
        if state in ("object_gone","bucket_gone"):bucket.call("delete",key="foo")
        if state=="bucket_gone":bucket.call("bucket_delete")
        options={"identity":identity}
        if verb=="get":options["output_path"]=str(p.ctx.output)
        response=bucket.raw("get_http" if verb=="get" else "delete",key="foo",**options)
        if status==200:
            require_ok(response);assert_equal(response["status_code"],200)
        else:rejected(response,status,code)

def version_acl(p,explicit):
    expected=actor(p,"main")
    default=[grant("FULL_CONTROL",expected)]
    public=[grant("READ",uri=ALL),grant("FULL_CONTROL",expected)]
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        versions,_=create_versions(bucket,p,"xyz",3)
        if explicit:version=versions[1]
        else:
            result=bucket.call("get_version",key="xyz",output_path=str(p.ctx.output));version=result["version_id"]
        response=api(bucket,"GetObjectAcl",Key="xyz",VersionId=version)["data"]
        assert_equal(response["Owner"]["DisplayName"],expected["display_name"])
        assert_equal(response["Owner"]["ID"],expected["canonical_id"])
        check_grants(response["Grants"],default)
        api(bucket,"PutObjectAcl",ACL="public-read",Key="xyz",**({"VersionId":version} if explicit else {}))
        response=api(bucket,"GetObjectAcl",Key="xyz",VersionId=version)["data"]
        check_grants(response["Grants"],public)
        if explicit:
            put(bucket,p,"xyz",b"")
            response=api(bucket,"GetObjectAcl",Key="xyz")["data"]
            check_grants(response["Grants"],default)

def concurrent_version_delete(p):
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        for _ in range(3):
            for number in range(5):
                key="key_"+str(number);bucket.put(key,key.encode())
        versions=bucket.call("versions_list")["versions"]
        assert_equal(len(versions),15)
        identifiers=[{"key":row["key"],"version_id":row["version_id"]} for row in versions]
        # One persistent worker dispatches all five requests against its same
        # default SDK client; processes or sequential JSONL calls are not substitutes.
        results=bucket.call("concurrent_requests",requests=[{"op":"delete_many_versions","bucket":bucket.name,"objects":identifiers} for _ in range(5)])["results"]
        for response in results:
            require_ok(response);assert_equal(len(response["deleted"]),15);assert_equal(response["errors_present"],False)
        assert_equal("Contents" in bucket.call("list_v1_page")["fields"],False)
        response=bucket.call("versions_list")
        assert_equal("Versions" in response["fields"],False);assert_equal("DeleteMarkers" in response["fields"],False)

def recreate_acl(p,initial_public):
    scope=BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":"public-read"}) if initial_public else BucketScope(p)
    with scope as bucket:
        response=bucket.raw("bucket_create_raw" if initial_public else "bucket_create_acl",**({} if initial_public else {"acl":"public-read"}))
        rejected(response,409,"BucketAlreadyExists")

def tagging_head(p):
    with BucketScope(p) as bucket:
        key="testputtags";bucket.put(key,source_data(7*MIB,7*MIB))
        response=bucket.call("tags_put",key=key,tags=[{"key":"0","value":"0"},{"key":"1","value":"1"}])
        assert_equal(response["status_code"],200)
        response=bucket.call("head_http",key=key)
        assert_equal(response["status_code"],200);assert_equal(header(response,"x-amz-tagging-count"),"2")

def multipart_tagging(p):
    with BucketScope(p) as bucket:
        key="mymultipart"
        with uploading(bucket,p,key=key,tagging="Hello=World&foo=bar") as upload:
            p.ctx.body.write_bytes(source_data(1))
            part=bucket.call("mp_part",key=key,upload_id=upload,part_number=1,body_path=str(p.ctx.body))
            response=bucket.call("mp_complete_observed",key=key,upload_id=upload,parts=[{"part_number":1,"etag":part["etag"].strip('"')}])
            assert_equal(response["status_code"],200)
            tags=bucket.call("tags_get",key=key)["tags"]
            assert_equal(len(tags),2)
            assert_equal(tags[0]["key"],"Hello");assert_equal(tags[0]["value"],"World")
            assert_equal(tags[1]["key"],"foo");assert_equal(tags[1]["value"],"bar")
            assert_equal(bucket.call("tags_delete",key=key)["status_code"],204)
            assert_equal(len(bucket.call("tags_get",key=key)["tags"]),0)

BASE=SCOPE_OPERATIONS
CONDITIONS=(("if_match","test_get_object_ifmatch_failed"),("not_match","test_get_object_ifnonematch_good"),("not_modified","test_get_object_ifmodifiedsince_failed"),("unmodified","test_get_object_ifunmodifiedsince_good"))
CASES=tuple(case("ceph_state_condition_"+mode,"Ceph GET 状态边界 "+mode,"保留原始引号ETag、1994时间、实际LastModified+1秒、服务端304/412、真实错误消息及响应ETag",BASE+" put_http get_http"+(" sdk_error_message" if mode in ("not_match","not_modified") else ""),lambda p,mode=mode:conditional_read(p,mode),reference,CEPH) for mode,reference in CONDITIONS)
CASES+=tuple(case("ceph_state_access_"+reference.removeprefix("test_object_"),"Ceph 对象访问状态 "+reference,"保留创建时桶/对象ACL、零字节foo、实际匿名/认证身份、删除顺序及200/403/404和错误码",BASE+" bucket_create_acl put_acl get_http"+(" identity_anonymous" if identity=="anonymous" else ""),lambda p,identity=identity,bucket_acl=bucket_acl,object_acl=object_acl,state=state,verb=verb,status=status,code=code:resource_access(p,identity,bucket_acl,object_acl,state,verb,status,code),reference,CEPH) for identity,bucket_acl,object_acl,state,verb,status,code,reference in ACCESS)
CASES+=tuple(case("ceph_state_version_acl_"+mode,"Ceph 版本ACL边界 "+mode,"保留三版本、实际指定/当前版本ID、独立Owner预期和原始check_grants全部字段/顺序规则，新版本仍为默认ACL",VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put put_version get_version api.GetObjectAcl api.PutObjectAcl",lambda p,explicit=explicit:version_acl(p,explicit),reference,CEPH) for mode,explicit,reference in (("explicit",True,"test_versioned_object_acl"),("current",False,"test_versioned_object_acl_no_version_specified")))
CASES+=(case("ceph_state_concurrent_versions_delete","Ceph 五路并发重复删除15个版本","保留5对象x3版本、同一SDK client并发五次同一删除清单、每次15个Deleted和无Errors、对象/版本/标记均不可见",VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put put concurrent_requests delete_many_versions list_v1_page",concurrent_version_delete,"test_versioning_concurrent_multi_object_delete",CEPH),)
CASES+=tuple(case("ceph_state_recreate_acl_"+mode,"Ceph 重建桶ACL冲突 "+mode,"保留创建时与重复创建的public-read ACL差异和409/BucketAlreadyExists原始预期",BASE+" bucket_create_acl",lambda p,initial_public=initial_public:recreate_acl(p,initial_public),reference,CEPH) for mode,initial_public,reference in (("overwrite",True,"test_bucket_recreate_overwrite_acl"),("new",False,"test_bucket_recreate_new_acl")))
CASES+=(case("ceph_state_tagging_head","Ceph HEAD标签计数","保留7MiB原始随机内容形状、0/1两标签、PUT/HEAD状态和x-amz-tagging-count字符串2",BASE+" put tags_put head_http",tagging_head,"test_get_obj_head_tagging",CEPH),)

CASES+=(case("ceph_state_multipart_tagging","Ceph 分块上传标签生命周期","保留Hello=World&foo=bar原始typed Tagging输入、1字节末块、完成200、标签顺序/内容、删除204和清空",BASE+" mp_create_tagging mp_create_metadata mp_part mp_complete_observed mp_abort_raw tags_get tags_delete",multipart_tagging,"test_set_multipart_tagging",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_state_boundaries",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
