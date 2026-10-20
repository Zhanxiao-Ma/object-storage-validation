"""Translate concrete Ceph version histories, null versions, markers and version-return contracts."""
import time
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_multipart import uploading,source_data,MIB,assert_error
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def configure(bucket,status,**options):
    bucket.call("versioning_put",status=status,**options)
    for attempt in range(5):
        actual=bucket.call("versioning_get").get("versioning_status")
        if actual==status:return
        if attempt<4:time.sleep(1)
    assert_equal(actual,status)

def put(bucket,p,key,data):
    p.ctx.body.write_bytes(data)
    return bucket.call("put_version",key=key,body_path=str(p.ctx.body))

def get(bucket,p,key,version=None):
    p.ctx.output.unlink(missing_ok=True)
    result=bucket.call("get_version",key=key,output_path=str(p.ctx.output),**({"version_id":version} if version is not None else {}))
    return result,p.ctx.output.read_bytes()

def create_versions(bucket,p,key,count,ids=None,contents=None):
    ids=[] if ids is None else ids;contents=[] if contents is None else contents
    for index in range(count):
        data=("content-"+str(index)).encode()
        ids.append(put(bucket,p,key,data)["version_id"]);contents.append(data)
    return ids,contents

def check_versions(bucket,p,key,ids,contents):
    result=bucket.call("versions_list")
    assert_equal("Versions" in result["fields"],True)
    # Preserve the source's actual reverse iteration and assertions, without
    # replacing server order with a locally sorted history.
    for index,row in enumerate(reversed(result["versions"])):
        assert_equal(row["version_id"],ids[index]);assert_equal(row["key"],key)
        assert_equal(get(bucket,p,key,row["version_id"])[1],contents[index])

def remove_version(bucket,p,key,ids,contents,index):
    assert_equal(len(ids),len(contents))
    index%=len(ids);ident=ids.pop(index);data=contents.pop(index)
    assert_equal(get(bucket,p,key,ident)[1],data)
    bucket.call("delete_version",key=key,version_id=ident)
    if ids:check_versions(bucket,p,key,ids,contents)

def no_current(bucket,p,key):
    assert_error(bucket.raw("get_version",key=key,output_path=str(p.ctx.output)),404,"NoSuchKey")

def no_versions(bucket,markers=False):
    result=bucket.call("versions_list")
    assert_equal("Versions" in result["fields"],False)
    if markers:assert_equal("DeleteMarkers" in result["fields"],False)

def multipart(bucket,p,key,data,metadata=None,payloads=None):
    options={"content_type":"text/bla","metadata":metadata} if metadata is not None else {}
    with uploading(bucket,p,key=key,**options) as ident:
        chunks=payloads if payloads is not None else [data[start:start+5*MIB] for start in range(0,len(data),5*MIB)]
        parts=[]
        for number,payload in enumerate(chunks,1):
            p.ctx.body.write_bytes(payload)
            result=bucket.call("mp_part",key=key,upload_id=ident,part_number=number,body_path=str(p.ctx.body))
            parts.append({"part_number":number,"etag":result["etag"].strip(chr(34))})
        return bucket.call("mp_complete_observed",key=key,upload_id=ident,parts=parts)

def returned_id(p,multipart_mode):
    for status,key in (("Enabled","bar"),(None,"baz"),("Suspended","foo" if multipart_mode else "baz")):
        with VersionScope(p) as bucket:
            if status:configure(bucket,status)
            result=multipart(bucket,p,key,source_data(30*MIB),{"foo":"baz"}) if multipart_mode else put(bucket,p,key,b"")
            if status=="Enabled":
                ident=result["version_id"]
                listed=bucket.call("versions_list")
                assert_equal("Versions" in listed["fields"],True)
                for row in listed["versions"]:assert_equal(row["version_id"],ident)
            else:assert_equal("version_id" in result,False)

def suspended_copy(bucket,p):
    ids,contents=create_versions(bucket,p,"testobj1",1)
    configure(bucket,"Suspended")
    put(bucket,p,"testobj1",b"null content")
    with VersionScope(p) as destination:
        bucket.call("copy",key="testobj2",source_key="testobj1")
        destination.call("copy",key="testobj1",source_bucket=bucket.name,source_key="testobj1")
        bucket.call("delete_version",key="testobj1")
        assert_equal(get(bucket,p,"testobj2")[1],b"null content")
        assert_equal(get(destination,p,"testobj1")[1],b"null content")
        bucket.call("delete_version",key="testobj2");bucket.call("delete_version",key="testobj2",version_id="null")
        for ident in ids:bucket.call("delete_version",key="testobj1",version_id=ident)
        destination.call("delete_version",key="testobj1")

def versioning(p,mode):
    if mode in ("atomic_id","multipart_id"):return returned_id(p,mode=="multipart_id")
    with VersionScope(p) as bucket:
        if mode=="configure":
            assert_equal(bucket.call("versioning_get").get("versioning_status"),None)
            for status in ("Suspended","Enabled","Enabled","Suspended"):configure(bucket,status)
            return
        if mode in ("null_remove","null_overwrite","null_suspended"):
            assert_equal(bucket.call("versioning_get").get("versioning_status"),None)
            key="testobjbar" if mode=="null_suspended" else "testobjfoo"
            original=b"foooz" if mode=="null_suspended" else b"fooz"
            put(bucket,p,key,original);configure(bucket,"Enabled")
            if mode=="null_suspended":configure(bucket,"Suspended")
            if mode!="null_remove":
                put(bucket,p,key,b"zzz");result,body=get(bucket,p,key);assert_equal(body,b"zzz")
                if mode=="null_suspended":assert_equal(len(bucket.call("versions_list")["versions"]),1)
                else:
                    bucket.call("delete_version",key=key,version_id=result["version_id"])
                    assert_equal(get(bucket,p,key)[1],original)
            bucket.call("delete_version",key=key,version_id="null")
            no_current(bucket,p,key);no_versions(bucket);return
        if mode.startswith("marker_header_"):
            state=mode.removeprefix("marker_header_")
            key={"plain":"frodo.txt","enabled":"bilbo.txt","suspended":"ringo.txt"}[state]
            if state!="plain":configure(bucket,"Enabled")
            put(bucket,p,key,("body version "+key).encode())
            if state=="suspended":configure(bucket,"Suspended")
            bucket.call("delete_version",key=key)
            result=bucket.raw("head_version",key=key)
            value="none" if result["ok"] else result["http_headers"]["x-amz-delete-marker"]
            if isinstance(value,list):value=",".join(value)
            assert_equal(value,"false" if state=="plain" else "true");return
        configure(bucket,"Enabled",**({"mfa_delete":"Disabled"} if mode in ("walks","head") else {}))
        key="testobj"
        if mode=="suspended_copy":return suspended_copy(bucket,p)
        if mode=="walks":
            for start,increment in ((-1,0),(-1,0),(0,0),(1,0),(4,-1),(3,3)):
                ids,contents=create_versions(bucket,p,key,5);index=start
                for attempt in range(5):remove_version(bucket,p,key,ids,contents,index);index+=increment
                bucket.call("versions_list")
            return
        if mode in ("remove_all","special"):
            for name in ("_testobj","_",":"," ") if mode=="special" else (key,):
                ids,contents=create_versions(bucket,p,name,10)
                for index in range(10):remove_version(bucket,p,name,ids,contents,index)
                assert_equal(len(ids),0);assert_equal(len(ids),len(contents))
            return
        if mode=="head":
            ids,contents=create_versions(bucket,p,key,5)
            bucket.call("delete_version",key=key,version_id=ids.pop());contents.pop()
            assert_equal(get(bucket,p,key)[1],contents[-1])
            deleted=bucket.call("delete_version",key=key);assert_equal(deleted["delete_marker"],True)
            result=bucket.call("versions_list")
            assert_equal(len(result["versions"]),4);assert_equal(len(result["delete_markers"]),1)
            assert_equal(result["delete_markers"][0]["version_id"],deleted["version_id"]);return
        if mode=="stack":
            create_versions(bucket,p,"test1/a",1)
            for attempt in range(3):bucket.call("delete_version",key="test1/a")
            result=bucket.call("versions_list")
            assert_equal(len(result["versions"]),1);assert_equal(len(result["delete_markers"]),3);return
        if mode=="suspend_versions":
            ids,contents=create_versions(bucket,p,key,5);configure(bucket,"Suspended")
            def delete_null():
                bucket.call("delete_version",key=key);assert_equal(len(ids),len(contents))
                for index in range(len(ids)-1,-1,-1):
                    if ids[index]=="null":ids.pop(index);contents.pop(index)
            def overwrite(data):
                put(bucket,p,key,data);assert_equal(len(ids),len(contents))
                for index in range(len(ids)-1,-1,-1):
                    if ids[index]=="null":ids.pop(index);contents.pop(index)
                ids.append("null");contents.append(data)
            delete_null();delete_null();overwrite(b"null content 1");overwrite(b"null content 2");delete_null();overwrite(b"null content 3");delete_null()
            configure(bucket,"Enabled");create_versions(bucket,p,key,3,ids,contents)
            for index in range(8):remove_version(bucket,p,key,ids,contents,index)
            assert_equal(len(ids),0);assert_equal(len(ids),len(contents));return
        if mode=="list_marker":
            ids,contents=create_versions(bucket,p,key,5);second,other=create_versions(bucket,p,"testobj-1",5)
            result=bucket.call("versions_list");assert_equal("Versions" in result["fields"],True)
            rows=list(reversed(result["versions"]))
            for index,(ident,data) in enumerate(zip(second+ids,other+contents)):
                expected="testobj-1" if index<5 else key
                assert_equal(rows[index]["version_id"],ident);assert_equal(rows[index]["key"],expected)
                assert_equal(get(bucket,p,expected,ident)[1],data)
            return
        if mode=="multipart_overwrite":
            contents=[]
            for pattern in (b"ABCDE",b"FGHIJ",b"KLMNO"):
                payload=pattern*MIB;tail=b"123"*MIB;data=payload*3+tail
                result=multipart(bucket,p,key,data,payloads=[payload]*3+[tail])
                assert_equal(bool(result["etag"]),True);assert_equal(get(bucket,p,key)[1],data);contents.append(data)
            listed=bucket.call("versions_list");assert_equal("Versions" in listed["fields"],True)
            ids=[row["version_id"] for row in reversed(listed["versions"])]
            check_versions(bucket,p,key,ids,contents)
            for index in range(3):remove_version(bucket,p,key,ids,contents,index)
            assert_equal(len(ids),0);assert_equal(len(ids),len(contents));return
        if mode=="bulk_marker_create":
            result=bucket.call("delete_many_versions",objects=[{"key":"key"}])
            assert_equal(len(result["deleted"]),1);assert_equal(result["deleted"][0]["delete_marker"],True)
            ident=result["deleted"][0]["delete_marker_version_id"]
            markers=bucket.call("versions_list")["delete_markers"]
            assert_equal(len(markers),1);assert_equal(markers[0]["version_id"],ident);assert_equal(markers[0]["key"],"key");return
        if mode in ("bulk","bulk_marker"):
            ids,contents=create_versions(bucket,p,"key",2);assert_equal(len(ids),2)
            objects=[{"key":"key","version_id":ident} for ident in ids]
            if mode=="bulk_marker":
                result=bucket.call("delete_version",key="key");assert_equal(bool(result["delete_marker"]),True)
                objects.append({"key":"key","version_id":result["version_id"]})
            for attempt in range(2):
                bucket.call("delete_many_versions",objects=objects);no_versions(bucket,mode=="bulk_marker")
            return
        raise ValueError(mode)

VECTORS=(
 ("configure","test_versioning_bucket_create_suspend"),("walks","test_versioning_obj_create_read_remove"),("head","test_versioning_obj_create_read_remove_head"),
 ("stack","test_versioning_stack_delete_merkers"),("null_remove","test_versioning_obj_plain_null_version_removal"),("null_overwrite","test_versioning_obj_plain_null_version_overwrite"),
 ("null_suspended","test_versioning_obj_plain_null_version_overwrite_suspended"),("suspend_versions","test_versioning_obj_suspend_versions"),("suspended_copy","test_versioning_obj_suspended_copy"),
 ("remove_all","test_versioning_obj_create_versions_remove_all"),("special","test_versioning_obj_create_versions_remove_special_names"),("list_marker","test_versioning_obj_list_marker"),
 ("bulk","test_versioning_multi_object_delete"),("bulk_marker","test_versioning_multi_object_delete_with_marker"),("bulk_marker_create","test_versioning_multi_object_delete_with_marker_create"),
 ("atomic_id","test_versioning_bucket_atomic_upload_return_version_id"),("multipart_id","test_versioning_bucket_multipart_upload_return_version_id"),("multipart_overwrite","test_versioning_obj_create_overwrite_multipart"),
 ("marker_header_plain","test_delete_marker_nonversioned"),("marker_header_enabled","test_delete_marker_versioned"),("marker_header_suspended","test_delete_marker_suspended"))

def operations(mode):
    if mode=="marker_header_plain":return VERSION_SCOPE_OPERATIONS+" put_version head_version"
    base=VERSION_SCOPE_OPERATIONS+" versioning_get versioning_put"
    if mode=="configure":return base
    if mode.startswith("marker_header_"):return base+" put_version head_version"
    if mode in ("atomic_id","multipart_id"):return base+(" mp_create_metadata mp_part mp_complete_observed mp_abort_raw" if mode=="multipart_id" else " put_version")
    if mode=="multipart_overwrite":return base+" get_version mp_create_metadata mp_part mp_complete_observed mp_abort_raw"
    if mode=="bulk_marker_create":return base+" delete_many_versions"
    if mode in ("bulk","bulk_marker"):return base+" put_version delete_many_versions"
    if mode=="stack":return base+" put_version"
    return base+" put_version get_version"+(" copy" if mode=="suspended_copy" else "")

CASES=tuple(case("ceph_version_"+mode,"Ceph 版本控制 "+mode,"保持原始版本序列、删除顺序、null/删除标记、返回字段和 HTTP 预期；仅在独立桶配置版本控制",operations(mode),lambda p,mode=mode:versioning(p,mode),reference,CEPH) for mode,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_versioning",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
