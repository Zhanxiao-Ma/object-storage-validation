"""Preserve Ceph object-attribute requests and multipart/version/encryption oracles."""
from contextlib import contextmanager
import base64
import hashlib
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .ceph_multipart import uploading,source_data,MIB
from .ceph_versioning import configure,put
from .ceph_sse_customer import HEADERS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

ATTRIBUTES=["ETag","Checksum","ObjectParts","StorageClass","ObjectSize"]

def attributes(bucket,key,**options):
    return api(bucket,"GetObjectAttributes",Key=key,ObjectAttributes=ATTRIBUTES,**options)["data"]

def check_parts_count(parts,expected):
    # Preserve Ceph's explicit alternative field names; do not infer from array size.
    assert_equal(parts["TotalPartsCount"] if "TotalPartsCount" in parts else parts["PartsCount"],expected)

def ordinary(p):
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"foo")
        etag=bucket.call("put_http",key="obj",body_path=str(p.ctx.body))["etag"].strip('"')
        assert_equal(bool(etag),True)
        result=attributes(bucket,"obj")
        assert_equal("DeleteMarker" in result,False);assert_equal("VersionId" in result,False)
        assert_equal(result["ObjectSize"],3);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal("ObjectParts" in result,False)

def versioned(p):
    with VersionScope(p) as bucket:
        configure(bucket,"Enabled")
        response=put(bucket,p,"obj",b"foo")
        etag=response["etag"].strip('"');version=response["version_id"]
        assert_equal(bool(etag),True);assert_equal(bool(version),True)
        result=attributes(bucket,"obj")
        assert_equal("DeleteMarker" in result,False);assert_equal(result["VersionId"],version)
        assert_equal(result["ObjectSize"],3);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal("ObjectParts" in result,False)
        put(bucket,p,"obj",b"foo")
        result=attributes(bucket,"obj",VersionId=version)
        assert_equal("DeleteMarker" in result,False);assert_equal(result["VersionId"],version)
        assert_equal(result["ObjectSize"],3);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal("ObjectParts" in result,False)

def sse_customer(p):
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"A"*1000)
        etag=bucket.call("put_sse_c",key="obj",body_path=str(p.ctx.body),sse_headers=HEADERS)["etag"].strip('"')
        assert_equal(bool(etag),True)
        rejected(raw(bucket,"GetObjectAttributes",Key="obj",ObjectAttributes=ATTRIBUTES),400)
        result=attributes(bucket,"obj",SSECustomerAlgorithm="AES256",SSECustomerKey="pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=",SSECustomerKeyMD5="DWygnHRtgiJ77HCm+1rvHw==")
        assert_equal("DeleteMarker" in result,False);assert_equal("VersionId" in result,False)
        assert_equal(result["ObjectSize"],1000);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal("ObjectParts" in result,False)

def multipart(p,mode):
    size=5*MIB if mode=="single" else 30*MIB
    key="multipart";part_size=5*MIB
    with BucketScope(p) as bucket:
        with uploading(bucket,p,key=key) as upload_id:
            parts=[]
            payload=source_data(size)
            for number,offset in enumerate(range(0,size,part_size),1):
                p.ctx.body.write_bytes(payload[offset:offset+part_size])
                response=bucket.call("mp_part",key=key,upload_id=upload_id,part_number=number,body_path=str(p.ctx.body))
                parts.append({"part_number":number,"etag":response["etag"].strip('"')})
            etag=bucket.call("mp_complete_observed",key=key,upload_id=upload_id,parts=parts)["etag"].strip('"')
            assert_equal(bool(etag),True)
            pages=({"MaxParts":1,"PartNumberMarker":3},{"MaxParts":10,"PartNumberMarker":4}) if mode=="paginated" else ({},)
            for page in pages:
                result=attributes(bucket,key,**page);actual=result["ObjectParts"]
                assert_equal(result["ObjectSize"],size);check_parts_count(actual,len(parts))
                assert_equal(result["ETag"],etag);assert_equal(result["StorageClass"],"STANDARD")
                if mode=="paginated":
                    assert_equal(actual["MaxParts"],page["MaxParts"]);assert_equal(actual["PartNumberMarker"],page["PartNumberMarker"])
                    assert_equal(actual["IsTruncated"],page["MaxParts"]==1)
                    if page["MaxParts"]==1:assert_equal(actual["NextPartNumberMarker"],4)
                    assert_equal(len(actual["Parts"]),1 if page["MaxParts"]==1 else 2)
                elif mode=="single":assert_equal(len(actual["Parts"]),1)
                else:assert_equal(actual["IsTruncated"],False)
                number=page.get("PartNumberMarker",0)+1
                for part in actual["Parts"]:
                    assert_equal(part["PartNumber"],number);assert_equal(part["Size"],part_size)
                    assert_equal("ChecksumSHA256" in part,False);number+=1

SHA256="arcu6553sHVAiX4MjW0j7I7vD4w6R+Gz9Ok0Q9lTa+0="
PART_SHA256=("275VF5loJr1YYawit0XSHREhkFXYkkPKGuoK0x9VKxI=","mrHwOfjTL5Zwfj74F05HOQGLdUb7E5szdCbxgUSq6NM=","Vw7oB/nKQ5xWb3hNgbyfkvDiivl+U+/Dft48nfJfDow=")
COMPOSITE_SHA256="uWBwpe1dxI4Vw8Gf0X9ynOdw/SS6VBzfWm9giiv1sf4=-3"

def checksum_put(bucket,p,key,algorithm,value):
    return bucket.call("put_http",key=key,body_path=str(p.ctx.body),checksum={"ChecksumAlgorithm":algorithm,"Checksum"+algorithm:value})

def object_checksum(p,algorithm):
    field="Checksum"+algorithm;expected=SHA256 if algorithm=="SHA256" else "Qeh8oXvGiSo="
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"A"*1024)
        response=checksum_put(bucket,p,"myobj",algorithm,expected)
        assert_equal(response["checksum"][field],expected)
        assert_equal(field in bucket.call("head_http",key="myobj")["checksum"],False)
        assert_equal(bucket.call("head_http",key="myobj",checksum_mode="ENABLED")["checksum"][field],expected)
        rejected(bucket.raw("put_http",key="myobj",body_path=str(p.ctx.body),checksum={"ChecksumAlgorithm":algorithm,field:"bad"}),400,"BadDigest")

def checksum_attributes(p):
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"A"*1024)
        response=checksum_put(bucket,p,"myobj","SHA256",SHA256)
        assert_equal(response["checksum"]["ChecksumSHA256"],SHA256)
        etag=response["etag"].strip('"');assert_equal(bool(etag),True)
        result=attributes(bucket,"myobj")
        assert_equal(result["ObjectSize"],1024);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal(result["Checksum"]["ChecksumSHA256"],SHA256)
        assert_equal("ObjectParts" in result,False)

@contextmanager
def checksum_upload(bucket,key,algorithm,checksum_type=None,check_algorithm=True,**options):
    checksum={"ChecksumAlgorithm":algorithm}
    if checksum_type is not None:checksum["ChecksumType"]=checksum_type
    result=bucket.call("mp_create_metadata",key=key,checksum=checksum,**options)
    ident=result["upload_id"]
    try:
        if check_algorithm:assert_equal(result["checksum"]["ChecksumAlgorithm"],algorithm)
        yield ident
    finally:
        result=bucket.raw("mp_abort_raw",key=key,upload_id=ident)
        if not result["ok"] and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):
            raise RuntimeError("checksum multipart cleanup failed: "+str(result))

def checksum_part(bucket,p,key,ident,number,payload,algorithm,value=None,include_checksum=True):
    p.ctx.body.write_bytes(payload);checksum={"ChecksumAlgorithm":algorithm}
    if value is not None:checksum["Checksum"+algorithm]=value
    response=bucket.call("mp_part",key=key,upload_id=ident,part_number=number,body_path=str(p.ctx.body),checksum=checksum)
    result={"part_number":number,"etag":response["etag"].strip('"')}
    if include_checksum:result["Checksum"+algorithm]=response["checksum"]["Checksum"+algorithm]
    return result

def checksum_complete(bucket,key,ident,parts,algorithm,value):
    return bucket.raw("mp_complete_observed",key=key,upload_id=ident,parts=parts,checksum={"Checksum"+algorithm:value})

def multipart_sha256(p):
    with BucketScope(p) as bucket:
        for key,mode in (("mymultipart","bad_digest"),("mymultipart2","missing_part_checksum"),("mymultipart3","success")):
            with checksum_upload(bucket,key,"SHA256") as ident:
                part=checksum_part(bucket,p,key,ident,1,b"A"*1024,"SHA256",SHA256)
                if mode=="missing_part_checksum":del part["ChecksumSHA256"]
                expected="Ok6Cs5b96ux6+MWQkJO7UBT5sKPBeXBLwvj/hK89smg=-1"
                response=checksum_complete(bucket,key,ident,[part],"SHA256",expected if mode=="success" else "bad")
                if mode!="success":rejected(response,400,"BadDigest");continue
                assert_equal(response["ok"],True);assert_equal(response["checksum"]["ChecksumSHA256"],expected)
                assert_equal("ChecksumSHA256" in bucket.call("head_http",key=key)["checksum"],False)
                assert_equal(bucket.call("head_http",key=key,checksum_mode="ENABLED")["checksum"]["ChecksumSHA256"],expected)

def reupload_checksum(p):
    with BucketScope(p) as bucket:
        key="mymultipart"
        with checksum_upload(bucket,key,"SHA256") as ident:
            parts=[checksum_part(bucket,p,key,ident,number,bytes([letter])*5*MIB,"SHA256",checksum) for number,(letter,checksum) in enumerate(zip(b"ABC",PART_SHA256),1)]
            first=checksum_complete(bucket,key,ident,parts,"SHA256",COMPOSITE_SHA256)
            assert_equal(first["ok"],True);assert_equal(first["etag"].strip('"'),"b2add96cc9702bbf4efb0ccdfc6b7747-3")
            assert_equal(first["checksum"]["ChecksumSHA256"],COMPOSITE_SHA256)
            second=checksum_complete(bucket,key,ident,parts,"SHA256",COMPOSITE_SHA256)
            assert_equal(second["ok"],True);assert_equal(first["etag"],second["etag"])
            assert_equal(first["checksum"]["ChecksumSHA256"],second["checksum"]["ChecksumSHA256"])

def multipart_checksum_attributes(p):
    key="multipart_checksum";size=64*MIB
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,key,"SHA256",check_algorithm=False,content_type="text/plain",metadata={"foo":"bar"}) as ident:
            payload=source_data(size);parts=[];checksums=[]
            for number,offset in enumerate(range(0,size,5*MIB),1):
                block=payload[offset:offset+5*MIB]
                part=checksum_part(bucket,p,key,ident,number,block,"SHA256",include_checksum=False)
                # Original helper consumes only ETag and independently hashes its input.
                parts.append(part)
                checksums.append(base64.b64encode(hashlib.sha256(block).digest()).decode())
            response=bucket.call("mp_complete_observed",key=key,upload_id=ident,parts=parts)
            upload_checksum=response["checksum"]["ChecksumSHA256"]
            bucket.call("get",key=key,output_path=str(p.ctx.output))
            result=attributes(bucket,key);assert_equal(result["ObjectSize"],size)
            assert_equal(result["Checksum"]["ChecksumSHA256"],upload_checksum)
            check_parts_count(result["ObjectParts"],len(parts))
            for number,part in enumerate(result["ObjectParts"]["Parts"],1):
                assert_equal(part["PartNumber"],number)
                assert_equal(part["Size"],5*MIB if number<len(parts) else size-(len(parts)-1)*5*MIB)
                assert_equal(part["ChecksumSHA256"],checksums[number-1])

CHECKSUM_VECTORS={
    "SHA256":("COMPOSITE",PART_SHA256,COMPOSITE_SHA256),
    "CRC64NVME":("FULL_OBJECT",("L/E4WYn8v98=","xW1l19VobYM=","cK5MnNaWrW4="),"i+6LR0y3eFo="),
    "CRC32":("FULL_OBJECT",("JRTCyQ==","QoZTGg==","YAgjqw=="),"WgDhBQ=="),
    "CRC32C":("FULL_OBJECT",("MDaLrw==","TH4EZg==","Z7mBIQ=="),"xU+Krw=="),
    "SHA1":("COMPOSITE",("iIaTCGbm+vdVjNqIMF2S0T7ibMk=","LS/TJ32bAVKEwRu+sE3X7awh/lk=","6DDwovUaHwrKNXDMzOGbuvj9kxI="),"sizjvY4eud3MrcHdZM3cQ/ol39o=-3"),
}

def checksum_type_helper(p,algorithm):
    checksum_type,part_checksums,composite=CHECKSUM_VECTORS[algorithm]
    field="Checksum"+algorithm;key="mymultipart3"
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,key,algorithm,checksum_type) as ident:
            parts=[checksum_part(bucket,p,key,ident,number,bytes([letter])*5*MIB,algorithm,checksum) for number,(letter,checksum) in enumerate(zip(b"ABC",part_checksums),1)]
            result=checksum_complete(bucket,key,ident,parts,algorithm,composite)
            assert_equal(result["ok"],True);assert_equal(result["checksum"]["ChecksumType"],checksum_type)
            assert_equal(result["checksum"][field],composite)
            assert_equal(field in bucket.call("head_http",key=key)["checksum"],False)
            observed=bucket.call("head_http",key=key,checksum_mode="ENABLED")["checksum"]
            assert_equal(observed["ChecksumType"],checksum_type);assert_equal(observed[field],composite)
            observed=api(bucket,"GetObjectAttributes",Key=key,ObjectAttributes=["Checksum"])["data"]["Checksum"]
            assert_equal(observed["ChecksumType"],checksum_type);assert_equal(observed[field],composite)
            for number,expected in enumerate(part_checksums,1):
                result=bucket.call("get_part",key=key,part_number=number,output_path=str(p.ctx.output))["checksum"]
                assert_equal(result["ChecksumType"],checksum_type);assert_equal(result[field],expected)

BASE=SCOPE_OPERATIONS+" api.GetObjectAttributes"
CASES=(case("ceph_attributes_object","Ceph 普通对象属性","保留五类属性选择、ETag/大小/存储类型和 VersionId/DeleteMarker/ObjectParts 缺失规则",BASE+" put_http",ordinary,"test_get_object_attributes",CEPH),
       case("ceph_attributes_versioned","Ceph 版本对象属性","保留实际 VersionId、覆盖后指定旧版本以及全部缺字段断言",VERSION_SCOPE_OPERATIONS+" api.GetObjectAttributes versioning_get versioning_put put_version",versioned,"test_get_versioned_object_attributes",CEPH),
       case("ceph_attributes_sse_customer","Ceph SSE-C 对象属性","保留原始密钥和 MD5、缺密钥 400 以及解密属性和缺字段规则",BASE+" put_sse_c",sse_customer,"test_get_sse_c_encrypted_object_attributes",CEPH))
CASES+=tuple(case("ceph_attributes_multipart_"+mode,"Ceph 分块对象属性 "+mode,"保留 5 MiB 分块、总大小、分块数和分页游标、ETag、存储类型及 checksum 缺失规则",BASE+" mp_create_metadata mp_part mp_complete_observed mp_abort_raw",lambda p,mode=mode:multipart(p,mode),reference,CEPH) for mode,reference in (("ordinary","test_get_multipart_object_attributes"),("paginated","test_get_paginated_multipart_object_attributes"),("single","test_get_single_multipart_object_attributes")))

CHECKSUM_BASE=SCOPE_OPERATIONS+" checksum_sha256 put_http head_http"
CASES+=(case("ceph_checksum_object_sha256","Ceph 单对象 SHA256","保留固定 Base64 校验和、默认 HEAD 不返回、启用 HEAD 返回和坏摘要 400/BadDigest",CHECKSUM_BASE,lambda p:object_checksum(p,"SHA256"),"test_object_checksum_sha256",CEPH),
        case("ceph_checksum_object_crc64nvme","Ceph 单对象 CRC64NVME","保持原始 CRC64NVME 摘要与坏摘要预期；SDK 无类型能力明确 not_run",SCOPE_OPERATIONS+" checksum_crc64nvme put_http head_http",lambda p:object_checksum(p,"CRC64NVME"),"test_object_checksum_crc64nvme",CEPH),
        case("ceph_checksum_attributes_sha256","Ceph 校验和对象属性","保留固定校验和、实际 PUT ETag 和五类属性及 ObjectParts 缺失断言",SCOPE_OPERATIONS+" checksum_sha256 put_http api.GetObjectAttributes",checksum_attributes,"test_get_checksum_object_attributes",CEPH),
        case("ceph_checksum_multipart_sha256","Ceph 分块 SHA256 完成错误与成功","保留三个独立上传、错误/缺失分块摘要 BadDigest、复合摘要及 HEAD 可见性",CHECKSUM_BASE+" mp_create_metadata mp_part mp_complete_observed mp_abort_raw",multipart_sha256,"test_multipart_checksum_sha256",CEPH),
        case("ceph_checksum_multipart_reupload","Ceph 分块校验和重复完成","保留三个 5 MiB 分块、固定 ETag 和 SHA256 以及重复完成的完全一致性",SCOPE_OPERATIONS+" checksum_sha256 mp_create_metadata mp_part mp_complete_observed mp_abort_raw",reupload_checksum,"test_multipart_reupload_checksum_and_etag",CEPH),
        case("ceph_checksum_attributes_multipart","Ceph 64 MiB 校验和分块属性","保留 64 MiB 原始分块形状、实际完成摘要、各分块独立摘要和最后分块大小",SCOPE_OPERATIONS+" checksum_sha256 mp_create_metadata mp_part mp_complete_observed mp_abort_raw get api.GetObjectAttributes",multipart_checksum_attributes,"test_get_multipart_checksum_object_attributes",CEPH))

CASES+=tuple(case("ceph_checksum_helper_"+algorithm.lower(),"Ceph 三分块 "+algorithm+" "+checksum_type,"保留固定分块及复合摘要、ChecksumType、默认/启用 HEAD、属性和逐块 GET 全部断言",SCOPE_OPERATIONS+" checksum_type checksum_"+algorithm.lower()+" mp_create_metadata mp_part mp_complete_observed mp_abort_raw head_http api.GetObjectAttributes get_part",lambda p,algorithm=algorithm:checksum_type_helper(p,algorithm),"test_multipart_use_cksum_helper_"+algorithm.lower(),CEPH) for algorithm,(checksum_type,_,__) in CHECKSUM_VECTORS.items())

def register(registry):
    registry.add_suite(Suite("ceph_checksum_attributes",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
