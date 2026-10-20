"""Preserve pinned Ceph copy/version/multipart inputs and oracles in isolated owned buckets."""
from contextlib import contextmanager
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .ceph_acl import actor
from .ceph_multipart import uploading,source_data,MIB
from .ceph_versioning import configure,create_versions,multipart as multipart_upload
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

class IdentityBucketScope(BucketScope):
    """Keep an alternate user's bucket ownership and cleanup consistent by selecting the same real SDK identity for every scoped request."""
    def __init__(self,probe,identity):
        super().__init__(probe);self.identity=identity
    def raw(self,op,**options):
        options.setdefault("identity",self.identity)
        return super().raw(op,**options)

def read(bucket,p,key,version=None,versioned=False,**options):
    p.ctx.output.unlink(missing_ok=True)
    if version is not None:options["version_id"]=version
    result=bucket.call("get_version" if version is not None or versioned else "get_metadata",key=key,output_path=str(p.ctx.output),**options)
    return result,p.ctx.output.read_bytes()

def copy_to(source,destination,key,target,version=None,**options):
    if version is not None:options["source_version_id"]=version
    return destination.call("copy",key=target,source_bucket=source.name,source_key=key,**options)

def basic(p,mode):
    source_key="obj1" if mode=="16m" else "foo123bar"
    target="obj2" if mode=="16m" else "bar321foo"
    with BucketScope(p) as bucket:
        if mode=="missing":
            rejected(bucket.raw("copy",key=target,source_bucket=bucket.name,source_key=source_key),404);return
        payload=bytes(16*MIB) if mode=="16m" else b"foo"
        if mode=="zero":bucket.put(source_key,source_key.encode());payload=b""
        if mode=="content_type":
            p.ctx.body.write_bytes(payload)
            bucket.call("put_metadata",key=source_key,body_path=str(p.ctx.body),content_type="text/bla",metadata={})
        else:bucket.put(source_key,payload)
        if mode=="self":
            rejected(bucket.raw("copy",key=source_key,source_bucket=bucket.name,source_key=source_key),400,"InvalidRequest");return
        copy_to(bucket,bucket,source_key,target)
        result,body=read(bucket,p,target)
        if mode in ("zero","16m"):assert_equal(result["size"],len(payload))
        else:
            assert_equal(body,b"foo")
            if mode=="content_type":assert_equal(result["content_type"],"text/bla")

def self_metadata(p):
    with BucketScope(p) as bucket:
        bucket.put("foo123bar",b"foo")
        bucket.call("copy_replace",key="foo123bar",source_bucket=bucket.name,source_key="foo123bar",metadata={"foo":"bar"})
        result,_=read(bucket,p,"foo123bar")
        assert_equal(result["metadata"],{"foo":"bar"})

def metadata(p,replacing):
    for size in (3,MIB):
        with BucketScope(p) as bucket:
            p.ctx.body.write_bytes(bytes(size))
            original={"key1":"value1","key2":"value2"}
            bucket.call("put_metadata",key="foo123bar",body_path=str(p.ctx.body),content_type="audio/ogg",metadata=original)
            expected={"key3":"value3","key2":"value2"} if replacing else original
            content_type="audio/mpeg" if replacing else "audio/ogg"
            if replacing:bucket.call("copy_replace",key="bar321foo",source_bucket=bucket.name,source_key="foo123bar",metadata=expected,content_type=content_type)
            else:copy_to(bucket,bucket,"foo123bar","bar321foo")
            result,body=read(bucket,p,"bar321foo")
            assert_equal(result["content_type"],content_type);assert_equal(result["metadata"],expected);assert_equal(result["size"],size)

def not_owned_bucket(p):
    with BucketScope(p) as source,IdentityBucketScope(p,"alt") as destination:
        source.put("foo123bar",b"foo")
        rejected(destination.raw("copy",key="bar321foo",source_bucket=source.name,source_key="foo123bar"),403)

def copy_version_chains(p):
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        data=source_data(30*MIB);metadata={"foo":"bar"};content_type="text/bla"
        multipart_upload(bucket,p,"srcmultipart",data,metadata)
        original,_=read(bucket,p,"srcmultipart",versioned=True)
        version=original["version_id"];original_size=original["size"]
        def verify(destination,key):
            result,body=read(destination,p,key,versioned=isinstance(destination,VersionScope))
            assert_equal(body,data);assert_equal(result["size"],original_size)
            assert_equal(result["metadata"],metadata);assert_equal(result["content_type"],content_type)
            return result
        copy_to(bucket,bucket,"srcmultipart","dstmultipart",version)
        second=verify(bucket,"dstmultipart")
        copy_to(bucket,bucket,"dstmultipart","dstmultipart2",second["version_id"]);verify(bucket,"dstmultipart2")
        with VersionScope(p) as second_bucket:
            configure(second_bucket,"Enabled")
            copy_to(bucket,second_bucket,"srcmultipart","dstmultipart3",version);verify(second_bucket,"dstmultipart3")
        with BucketScope(p) as third_bucket:
            copy_to(bucket,third_bucket,"srcmultipart","dstmultipart4",version);verify(third_bucket,"dstmultipart4")
            copy_to(third_bucket,bucket,"dstmultipart4","dstmultipart5");verify(bucket,"dstmultipart5")

def version_copies(p):
    with VersionScope(p) as source:
        configure(source,"Enabled")
        versions,contents=create_versions(source,p,"testobj",3)
        for index,version in enumerate(versions):
            copy_to(source,source,"testobj","key_"+str(index),version)
            assert_equal(read(source,p,"key_"+str(index))[1],contents[index])
        with BucketScope(p) as destination:
            for index,version in enumerate(versions):
                copy_to(source,destination,"testobj","key_"+str(index),version)
                assert_equal(read(destination,p,"key_"+str(index))[1],contents[index])
            copy_to(source,destination,"testobj","new_key")
            assert_equal(read(destination,p,"new_key")[1],contents[-1])

def multipart_versioned(p):
    size=15*MIB
    with VersionScope(p) as source,BucketScope(p) as destination:
        assert_equal(source.call("versioning_get").get("versioning_status"),None)
        configure(source,"Enabled")
        for seed in (1,2,3):source.put("foo",source_data(size,size,seed))
        versions=[row["version_id"] for row in source.call("versions_list")["versions"]]
        for version in versions:
            with uploading(destination,p,key="mymultipart") as upload:
                parts=[]
                for number,start in enumerate(range(0,size,5*MIB),1):
                    result=destination.call("mp_copy",key="mymultipart",source_bucket=source.name,source_key="foo",version_id=version,upload_id=upload,part_number=number,range_header=f"bytes={start}-{min(start+5*MIB-1,size-1)}")
                    parts.append({"part_number":number,"etag":result["etag"]})
                destination.call("mp_complete_observed",key="mymultipart",upload_id=upload,parts=parts)
                first,_=read(destination,p,"mymultipart");assert_equal(first["size"],size)
                source_head,_=read(source,p,"foo",version)
                target,body=read(destination,p,"mymultipart")
                assert_equal(source_head["size"]>=target["size"],True)
                expected=read(source,p,"foo",version,offset=0,length=target["size"])[1]
                assert_equal(expected,body)

def atomic_upload(p):
    with BucketScope(p) as bucket:
        bucket.put("foo",b"bar")
        with uploading(bucket,p,key="foo") as upload:
            assert_equal(read(bucket,p,"foo")[1],b"bar")
            bucket.call("mp_abort_raw",key="foo",upload_id=upload)
            assert_equal(read(bucket,p,"foo")[1],b"bar")

def percent_encoded_copy(p):
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"foo")
        for key in ("anyfilename%25.txt","anyfile.txt"):
            bucket.call("put_metadata",key=key,body_path=str(p.ctx.body),content_type="text/plain",metadata={})
        with uploading(bucket,p,key="anyfile.txt") as upload:
            response=bucket.raw("mp_copy",key="anyfile.txt",upload_id=upload,part_number=1,source_bucket=bucket.name,source_key="anyfilename%.txt")
            assert_equal(response["ok"],False)
            if not isinstance(response.get("status_code"),int) or not 400<=response["status_code"]<600:
                raise AssertionError("source expects an actual service ClientError: "+str(response))
            assert_equal(read(bucket,p,"anyfile.txt")[1],b"foo")

@contextmanager
def upload_as(bucket,key,identity):
    upload=bucket.call("mp_create_metadata",key=key,identity=identity)["upload_id"]
    try:yield upload
    finally:
        result=bucket.raw("mp_abort_raw",key=key,upload_id=upload,identity=identity)
        if not result["ok"] and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):
            raise RuntimeError("identity-specific multipart cleanup failed: "+str(result))

def multipart_owners(p):
    main=actor(p,"main");alt=actor(p,"alt")
    with BucketScope(p) as bucket:
        api(bucket,"PutBucketAcl",ACL="public-read-write")
        with upload_as(bucket,"multipart1","main") as first,upload_as(bucket,"multipart2","alt") as second:
            for identity in ("main","alt"):
                uploads=bucket.call("mp_list",identity=identity)["uploads"]
                assert_equal(len(uploads),2)
                for row,key,ident,expected in zip(uploads,("multipart1","multipart2"),(first,second),(main,alt)):
                    assert_equal(row["key"],key);assert_equal(row["upload_id"],ident)
                    for owner in ("initiator","owner"):
                        assert_equal(row[owner]["id"],expected["canonical_id"])
                        assert_equal(row[owner]["display_name"],expected["display_name"])

BASIC=(("zero","test_object_copy_zero_size"),("16m","test_object_copy_16m"),("same","test_object_copy_same_bucket"),("content_type","test_object_copy_verify_contenttype"),("self","test_object_copy_to_itself"),("missing","test_object_copy_key_not_found"))
CASES=tuple(case("ceph_copy_contract_"+mode,"Ceph 完整复制契约 "+mode,"保留原始对象键、字节和 Content-Type 输入及服务端状态/错误码原始断言",SCOPE_OPERATIONS+" put copy get_metadata"+(" put_metadata" if mode=="content_type" else ""),lambda p,mode=mode:basic(p,mode),reference,CEPH) for mode,reference in BASIC)
CASES+=tuple(case("ceph_copy_contract_metadata_"+mode,"Ceph 完整复制元数据 "+mode,"保留 3 字节和 1 MiB 两组大小、原始元数据/替换映射、Content-Type 及 ContentLength",SCOPE_OPERATIONS+" put_metadata get_metadata copy"+(" copy_replace" if replacing else ""),lambda p,replacing=replacing:metadata(p,replacing),reference,CEPH) for mode,replacing,reference in (("retain",False,"test_object_copy_retaining_metadata"),("replace",True,"test_object_copy_replacing_metadata")))
CASES+=(case("ceph_copy_contract_other_owner","Ceph 跨身份私有源复制拒绝","实际alt创建目的桶并尝试复制main私有源，保留403及各自身份清理",SCOPE_OPERATIONS+" identity_alt put copy",not_owned_bucket,"test_object_copy_not_owned_bucket",CEPH),
        case("ceph_copy_contract_version_chains","Ceph 30 MiB 版本分块复制链","保留两次同桶复制、跨版本桶/普通桶和反向复制、真实VersionId、字节、元数据及类型",VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put mp_create_metadata mp_part mp_complete_observed mp_abort_raw copy copy_source_version get_metadata get_version",copy_version_chains,"test_object_copy_versioning_multipart_upload",CEPH),
        case("ceph_copy_contract_versions","Ceph 全部指定版本复制","保留content-0/1/2三版本同桶和跨桶复制，以及省略VersionId的当前版本行为",VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put put_version copy copy_source_version get_metadata",version_copies,"test_versioning_copy_obj_version",CEPH),
        case("ceph_copy_contract_multipart_versions","Ceph 分块复制三个历史版本","保留三个15MiB源版本、实际版本清单顺序、5MiB Range、目标大小与真实源版本Range字节比较",VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put put get_version get_metadata copy mp_create_metadata mp_copy mp_copy_version mp_complete_observed mp_abort_raw",multipart_versioned,"test_multipart_copy_versioned",CEPH),
        case("ceph_copy_contract_atomic_upload","Ceph 创建与取消分块期间原对象原子可见","保留创建未完成上传后和取消上传后的原始bar读取断言",SCOPE_OPERATIONS+" put get_metadata mp_create_metadata mp_abort_raw",atomic_upload,"test_atomic_multipart_upload_write",CEPH),
        case("ceph_copy_contract_percent_source","Ceph 百分号源键分块复制","保留%25源键、%源请求、实际服务端ClientError和已有目的foo保持可读",SCOPE_OPERATIONS+" put_metadata get_metadata mp_create_metadata mp_copy mp_abort_raw",percent_encoded_copy,"test_upload_part_copy_percent_encoded_key",CEPH),
        case("ceph_copy_contract_multipart_owners","Ceph 跨身份未完成上传所有者","保留main/alt实际创建与各自取消、固定返回顺序、Owner/Initiator和独立配置的ID/DisplayName",SCOPE_OPERATIONS+" identity_alt api.PutBucketAcl mp_create_metadata mp_list mp_abort_raw",multipart_owners,"test_list_multipart_upload_owner",CEPH))

CASES+=(case("ceph_copy_contract_self_metadata","Ceph 原地复制仅替换元数据","保留源 MetadataDirective=REPLACE 和 foo=bar 元数据，原始请求不提供 ContentType",SCOPE_OPERATIONS+" put copy_replace get_metadata",self_metadata,"test_object_copy_to_itself_with_metadata",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_copy_contracts",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
