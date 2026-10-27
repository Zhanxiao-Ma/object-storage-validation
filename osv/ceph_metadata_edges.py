"""Preserve Ceph cross-operation metadata, ACL ownership and versioned-copy relationships."""
import time
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_acl import actor,check_grants,grant,append_grant
from .ceph_bucket_config import api
from .ceph_versioning import configure,get,put
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH


def compare_dates(first,second):
    """Preserve the upstream asymmetric rule: truncate only the first timestamp to seconds."""
    assert_equal((first//1000)*1000,second)


def list_relationship(p,versioned=False):
    """Compare actual HEAD/ACL producers with actual listing rows; retain required owner fields and original date precision rule."""
    with (VersionScope(p) if versioned else BucketScope(p)) as bucket:
        if versioned:
            configure(bucket,"Enabled")
            # _create_objects(bucket_name=...) calls get_new_bucket_resource,
            # which really issues CreateBucket again even for this existing bucket.
            bucket.call("bucket_create_raw")
        for key in ("bar","baz","foo"):bucket.put(key,key.encode())
        data={}
        for key in ("bar","baz","foo"):
            head=bucket.call("head_http",key=key)
            owner=api(bucket,"GetObjectAcl",Key=key)["data"]["Owner"]
            data[bucket.key(key)]={"etag":head["etag"],"size":head["size"],"last_modified_ms":head["last_modified_ms"],
                "owner":{"id":owner["ID"],"display_name":owner["DisplayName"]}}
            if versioned:data[bucket.key(key)]["version_id"]=head["http_headers"]["x-amz-version-id"]
        listing=bucket.call("versions_list" if versioned else "list_v1_page")
        assert_equal(("Versions" if versioned else "Contents") in listing["fields"],True)
        rows=listing["versions" if versioned else "objects"]
        for row in rows:
            original=data[row["key"]]
            for field in ("etag","size","owner")+(("version_id",) if versioned else ()):
                if field=="owner":
                    for name in ("id","display_name"):assert_equal(row[field][name],original[field][name])
                else:assert_equal(row[field],original[field])
            compare_dates(row["last_modified_ms"],original["last_modified_ms"])


def unicode_metadata(p):
    """Keep the exact before-call Unicode header override and literal bar body; inspect the actual GET metadata string."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        bucket.call("put_http",key="foo",body_path=str(p.ctx.body),wire_headers={"x-amz-meta-meta1":"Hello Worldé"})
        observed=bucket.call("get_metadata",key="foo",output_path=str(p.ctx.output))
        assert_equal(observed["metadata"]["meta1"],"Hello Worldé")


def file_body(p):
    """Send UTF-8 bytes from an actual file through the production SDK and decode the independently expected bar read-back."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes("bar".encode("utf-8"))
        bucket.call("put",key="foo",body_path=str(p.ctx.body))
        bucket.call("get",key="foo",output_path=str(p.ctx.output))
        assert_equal(p.ctx.output.read_bytes().decode("utf-8"),"bar")


def public_bucket(p):
    return BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":"public-read-write"})


def bucketowner_acl(p,full):
    """Preserve the source fixed Grants[2] owner lookup, empty second PUT and original strict check_grants order."""
    other=actor(p,"alt")
    with public_bucket(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        bucket.call("put",key="foo",body_path=str(p.ctx.body),identity="alt")
        observed=api(bucket,"GetBucketAcl")["data"]["Grants"][2]["Grantee"]
        owner={"canonical_id":observed["ID"],"display_name":observed["DisplayName"]}
        p.ctx.body.write_bytes(b"")
        bucket.call("put_acl",key="foo",body_path=str(p.ctx.body),acl="bucket-owner-full-control" if full else "bucket-owner-read",identity="alt")
        result=bucket.call("api.GetObjectAcl",input={"Key":"foo"},identity="alt")["data"]
        check_grants(result["Grants"],[grant("FULL_CONTROL",other),grant("FULL_CONTROL" if full else "READ",owner)])


def owner_after_grant(p):
    """Keep source independent owner metadata and real alternate-identity ACL rewrite; only assert the original Owner.ID invariant."""
    main=actor(p);other=actor(p,"alt")
    with public_bucket(p) as bucket:
        bucket.put("foo",b"bar")
        policy={"Grants":[{"Grantee":{"ID":other["canonical_id"],"Type":"CanonicalUser"},"Permission":"FULL_CONTROL"}],
            "Owner":{"DisplayName":main["display_name"],"ID":main["canonical_id"]}}
        api(bucket,"PutObjectAcl",Key="foo",AccessControlPolicy=policy)
        policy["Grants"][0]["Permission"]="READ_ACP"
        bucket.call("api.PutObjectAcl",input={"Key":"foo","AccessControlPolicy":policy},identity="alt")
        assert_equal(bucket.call("api.GetObjectAcl",input={"Key":"foo"},identity="alt")["data"]["Owner"]["ID"],main["canonical_id"])


def alternate_id(p):
    """Require only the alternate canonical ID where the source never reads its display name."""
    value=p.ctx.workload.get("identities",{}).get("alt",{}).get("canonical_id")
    if value is None:raise CaseNotRun("Ceph source requires alternate canonical ID","workload.identities.alt.canonical_id")
    return {"canonical_id":value}


def object_grants(bucket,key,main,other):
    """Append the source grant to actual SDK ACL output, retaining actual grants as the next producer's input."""
    grants=api(bucket,"GetObjectAcl",Key=key)["data"]["Grants"]
    grants.append({"Grantee":{"ID":other["canonical_id"],"Type":"CanonicalUser"},"Permission":"FULL_CONTROL"})
    return {"Grants":grants,"Owner":{"DisplayName":main["display_name"],"ID":main["canonical_id"]}}


def attributes_after_grant(p):
    """Preserve x-amz-foo, actual GET content type/ETag and ACL update without deriving any expected field from the request."""
    main=actor(p);other=alternate_id(p)
    with public_bucket(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        bucket.call("put_http",key="foo",body_path=str(p.ctx.body),wire_headers={"x-amz-foo":"bar"})
        before=bucket.call("get_metadata",key="foo",output_path=str(p.ctx.output))
        api(bucket,"PutObjectAcl",Key="foo",AccessControlPolicy=object_grants(bucket,"foo",main,other))
        after=bucket.call("get_metadata",key="foo",output_path=str(p.ctx.output))
        assert_equal(before["content_type"],after["content_type"]);assert_equal(before["etag"],after["etag"])


def header_acl(p):
    """Send all five literal source x-amz-grant-* headers before signing and keep the original full ordered grant oracle."""
    other=actor(p,"alt")
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"bar")
        headers={"x-amz-grant-"+permission:"id="+other["canonical_id"] for permission in ("read","write","read-acp","write-acp","full-control")}
        bucket.call("put_http",key="foo_key",body_path=str(p.ctx.body),wire_headers=headers)
        check_grants(api(bucket,"GetObjectAcl",Key="foo_key")["data"]["Grants"],
            [grant(permission,other) for permission in ("READ","WRITE","READ_ACP","WRITE_ACP","FULL_CONTROL")])


def copy_canned(p):
    """Keep both actual public-read CopyObject writes, alternate GETs, and source metadata-only REPLACE without adding a content type."""
    with BucketScope(p) as bucket:
        bucket.put("foo123bar",b"foo")
        bucket.call("copy",key="bar321foo",source_key="foo123bar",acl="public-read")
        bucket.call("get_http",key="bar321foo",output_path=str(p.ctx.output),identity="alt")
        bucket.call("copy",key="foo123bar",source_key="bar321foo",acl="public-read",
            wire_headers={"x-amz-meta-abc":"def","x-amz-metadata-directive":"REPLACE"})
        bucket.call("get_http",key="foo123bar",output_path=str(p.ctx.output),identity="alt")


def copy_granted(p):
    """Grant the actual object and bucket ACL outputs to another identity, then use that identity for GET, HEAD preflight and CopyObject."""
    main=actor(p);other=alternate_id(p)
    with BucketScope(p) as bucket:
        bucket.put("foo123bar",b"foo")
        api(bucket,"PutObjectAcl",Key="foo123bar",AccessControlPolicy=object_grants(bucket,"foo123bar",main,other))
        value={"Grantee":{"ID":other["canonical_id"],"Type":"CanonicalUser"},"Permission":"FULL_CONTROL"}
        api(bucket,"PutBucketAcl",AccessControlPolicy=append_grant(bucket,main,value))
        bucket.call("get_http",key="foo123bar",output_path=str(p.ctx.output),identity="alt")
        bucket.call("head_http",key="foo123bar",identity="alt")
        bucket.call("copy",key="bar321foo",source_key="foo123bar",identity="alt")


def version_copy(p,encoded=False):
    """Pass actual GET version IDs into real SDK sourceVersionId, across all source/destination bucket states and special key encodings."""
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        if encoded:
            put(bucket,p,"foo?bar",b"")
            head=bucket.call("head_http",key="foo?bar")
            bucket.call("copy",key="bar&foo",source_key="foo?bar",source_version_id=head["http_headers"]["x-amz-version-id"])
            bucket.call("head_http",key="bar&foo")
            return
        put(bucket,p,"foo123bar",bytes(5))
        original,body=get(bucket,p,"foo123bar")
        def copied(target,key,source,key_from,version=None):
            options={"source_version_id":version} if version is not None else {}
            target.call("copy",key=key,source_bucket=source.name,source_key=key_from,**options)
            result,body=get(target,p,key)
            assert_equal(body.decode(),bytes(5).decode());assert_equal(result["size"],5)
            return result
        first=copied(bucket,"bar321foo",bucket,"foo123bar",original["version_id"])
        copied(bucket,"bar321foo2",bucket,"bar321foo",first["version_id"])
        with VersionScope(p) as versioned:
            configure(versioned,"Enabled")
            copied(versioned,"bar321foo3",bucket,"foo123bar",original["version_id"])
        with BucketScope(p) as plain:
            copied(plain,"bar321foo4",bucket,"foo123bar",original["version_id"])
            copied(bucket,"foo123bar2",plain,"bar321foo4")


def acl_mtime(p):
    """Keep the two-second source separation and asymmetric date comparisons over actual HEAD, LIST and version LIST before and after ACL mutation."""
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled");bucket.put("foo",b"foooz")
        original=bucket.call("head_http",key="foo")["last_modified_ms"]
        for operation,field in (("list_v1_page","objects"),("versions_list","versions")):
            compare_dates(bucket.call(operation)[field][0]["last_modified_ms"],original)
        time.sleep(2);api(bucket,"PutObjectAcl",Key="foo",ACL="private")
        compare_dates(original,bucket.call("head_http",key="foo")["last_modified_ms"])
        for operation,field in (("list_v1_page","objects"),("versions_list","versions")):
            compare_dates(bucket.call(operation)[field][0]["last_modified_ms"],original)


def content_encoding(p):
    """Preserve six original encoding strings and require aws-chunked removal or true absence after actual empty PUT/HEAD."""
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"")
        for value,expected in (("gzip","gzip"),("deflate, gzip","deflate, gzip"),("gzip, aws-chunked","gzip"),
            ("aws-chunked, gzip","gzip"),("aws-chunked",None),("aws-chunked, aws-chunked",None)):
            bucket.call("put_http",key="encoding",body_path=str(p.ctx.body),wire_headers={"content-encoding":value})
            headers=bucket.call("head_http",key="encoding")["http_headers"]
            if expected is None:assert_equal("content-encoding" in headers,False)
            else:assert_equal(headers["content-encoding"],expected)


def special_keys(p):
    """Keep the exact source special-key order, full GET contents and subsequent private ACL updates, including whitespace keys."""
    keys=[" ",'"',"$","%","&","'","<",">","_","_ ","_ _","__"]
    with BucketScope(p) as bucket:
        for key in keys:bucket.put(key,key.encode())
        observed=bucket.call("list_v1_page")["keys"]
        assert_equal(observed,[bucket.key(key) for key in keys])
        for key in keys:
            assert_equal(bucket.key(key) in observed,True)
            bucket.call("get",key=key,output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes().decode(),key)
            api(bucket,"PutObjectAcl",Key=key,ACL="private")


def special_prefix(p):
    """Preserve the five literal source keys and exact unfiltered/four-key prefix counts with actual V1 listing."""
    with BucketScope(p) as bucket:
        for key in ("_bla/1","_bla/2","_bla/3","_bla/4","abcd"):bucket.put(key,key.encode())
        assert_equal(len(bucket.call("list_v1_page")["keys"]),5)
        assert_equal(len(bucket.call("list_v1_page",prefix="_bla/")["keys"]),4)


def recreate_retains_objects(p):
    """Recreate only the freshly owned bucket; retain source exact key ordering and verify existing data remains listed."""
    with BucketScope(p) as bucket:
        keys=["mykey1","mykey2"]
        for key in keys:bucket.put(key,key.encode())
        assert_equal(bucket.call("list_v1_page")["keys"],keys)
        bucket.call("bucket_create_raw")
        assert_equal(bucket.call("list_v1_page")["keys"],keys)


V=VERSION_SCOPE_OPERATIONS+" versioning_put versioning_get"
S=SCOPE_OPERATIONS
CASES=(
 case("ceph_metadata_edges_special_keys","Ceph 特殊对象键","原始十二个特殊键顺序、实际 GET 内容及 private ACL 更新",S+" put list_v1_page get api.PutObjectAcl",special_keys,"test_bucket_create_special_key_names",CEPH),
 case("ceph_metadata_edges_special_prefix","Ceph 特殊键前缀","原始五键全量计数为5，_bla/ 前缀计数为4",S+" put list_v1_page",special_prefix,"test_bucket_list_special_prefix",CEPH),
 case("ceph_metadata_edges_recreate","Ceph 重建桶保留数据","只重建本用例拥有的桶，前后两键清单顺序均保留",S+" put list_v1_page",recreate_retains_objects,"test_bucket_recreate_not_overriding",CEPH),
 case("ceph_metadata_edges_list","Ceph 清单对象字段关系","实际 HEAD/ACL 与 LIST 的 owner、ETag、大小、日期逐项一致",S+" put head_http list_v1_page api.GetObjectAcl",list_relationship,"test_bucket_list_return_data",CEPH),
 case("ceph_metadata_edges_list_versioned","Ceph 版本清单字段关系","实际 HEAD/ACL 与版本 LIST 的 owner、版本ID、ETag、大小、日期逐项一致",V+" put head_http api.GetObjectAcl",lambda p:list_relationship(p,True),"test_bucket_list_return_data_versioning",CEPH),
 case("ceph_metadata_edges_unicode","Ceph Unicode 元数据头","原始 Hello Worldé 在实际 GET Metadata.meta1 完整保留",S+" put_http wire_headers get_metadata",unicode_metadata,"test_object_set_get_unicode_metadata",CEPH),
 case("ceph_metadata_edges_file","Ceph 文件字节写入","真实文件中的 UTF-8 bar 经 SDK PUT/GET 后解码仍为 bar",S+" put get",file_body,"test_object_write_file",CEPH),
 case("ceph_metadata_edges_owner_read","Ceph 桶拥有者读 ACL","保留 Grants[2] 原始下标与另一身份的空对象重写、完整授权顺序",S+" bucket_create_acl identity_alt put put_acl api.GetBucketAcl api.GetObjectAcl",lambda p:bucketowner_acl(p,False),"test_object_acl_canned_bucketownerread",CEPH),
 case("ceph_metadata_edges_owner_full","Ceph 桶拥有者全控 ACL","保留 Grants[2] 原始下标与另一身份的空对象重写、完整授权顺序",S+" bucket_create_acl identity_alt put put_acl api.GetBucketAcl api.GetObjectAcl",lambda p:bucketowner_acl(p,True),"test_object_acl_canned_bucketownerfullcontrol",CEPH),
 case("ceph_metadata_edges_owner_stable","Ceph ACL 重写拥有者","另一身份真实重写授权后 Owner.ID 保持主身份",S+" bucket_create_acl put identity_alt api.PutObjectAcl api.GetObjectAcl",owner_after_grant,"test_object_acl_full_control_verify_owner",CEPH),
 case("ceph_metadata_edges_attributes_stable","Ceph ACL 不改变对象属性","x-amz-foo 写入及真实 ACL 更新前后 ContentType/ETag 相同",S+" bucket_create_acl put_http wire_headers get_metadata api.GetObjectAcl api.PutObjectAcl",attributes_after_grant,"test_object_acl_full_control_verify_attributes",CEPH),
 case("ceph_metadata_edges_acl_headers","Ceph 五项授权请求头","保留原始五头声明及完整授权字段、数量与顺序",S+" put_http wire_headers api.GetObjectAcl",header_acl,"test_object_header_acl_grants",CEPH),
 case("ceph_metadata_edges_copy_canned","Ceph 复制公共 ACL","两次真实公共复制及另一个身份 GET；第二次仅替换 abc=def",S+" put copy copy_acl get_http identity_alt wire_headers",copy_canned,"test_object_copy_canned_acl",CEPH),
 case("ceph_metadata_edges_copy_granted","Ceph 另一身份复制授权对象","源对象与桶真实 ACL 作为授权生产者，另一身份 GET/HEAD/复制成功",S+" put copy head_http get_http identity_alt api.GetObjectAcl api.PutObjectAcl api.GetBucketAcl api.PutBucketAcl",copy_granted,"test_object_copy_not_owned_object_bucket",CEPH),
 case("ceph_metadata_edges_copy_versions","Ceph 指定版本跨桶复制","五条复制链消费真实版本ID，原始五字节内容和 ContentLength 一致",V+" put_version get_version copy copy_source_version",version_copy,"test_object_copy_versioned_bucket",CEPH),
 case("ceph_metadata_edges_copy_encoded","Ceph 特殊键指定版本复制","foo?bar 与 bar&foo 的真实 HEAD 及生产者版本ID经过 SDK 编码",V+" put_version head_http copy copy_source_version",lambda p:version_copy(p,True),"test_object_copy_versioned_url_encoding",CEPH),
 case("ceph_metadata_edges_acl_mtime","Ceph ACL 不改变修改时间","保留两秒间隔及 HEAD/LIST/版本LIST 的原始单边截秒规则",V+" put head_http list_v1_page api.PutObjectAcl",acl_mtime,"test_object_put_acl_mtime",CEPH),
 case("ceph_metadata_edges_content_encoding","Ceph aws-chunked 编码声明","六种原始 Content-Encoding 的精确值或字段缺失",S+" put_http head_http wire_headers",content_encoding,"test_object_content_encoding_aws_chunked",CEPH),
)


def register(registry):
    registry.add_suite(Suite("ceph_metadata_edges",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
