"""Preserve Ceph object-attribute requests and multipart/version/encryption oracles."""
from contextlib import contextmanager
import base64
import hashlib
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api as sdk_api,raw,rejected
from .response_contract import observe
from .ceph_multipart import uploading,source_data,MIB
from .ceph_versioning import configure,put
from .ceph_sse_customer import HEADERS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def observed_call(bucket, operation, **options):
    """Keep actual SDK errors and annotate successful checksum response field assertions."""
    return observe(bucket.call(operation, **options), operation)

def api(bucket, operation, **options):
    """Preserve typed API output while identifying missing required nested attributes."""
    return observe(sdk_api(bucket, operation, **options), operation)

ATTRIBUTES=["ETag","Checksum","ObjectParts","StorageClass","ObjectSize"]

def attributes(bucket,key,**options):
    return api(bucket,"GetObjectAttributes",Key=key,ObjectAttributes=ATTRIBUTES,**options)["data"]

def check_parts_count(parts,expected):
    # Preserve Ceph's explicit alternative field names; do not infer from array size.
    assert_equal(parts["TotalPartsCount"] if "TotalPartsCount" in parts else parts["PartsCount"],expected)

def ordinary(p):
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"foo")
        etag=observed_call(bucket,"put_http",key="obj",body_path=str(p.ctx.body))["etag"].strip('"')
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
        etag=observed_call(bucket,"put_sse_c",key="obj",body_path=str(p.ctx.body),sse_headers=HEADERS)["etag"].strip('"')
        assert_equal(bool(etag),True)
        rejected(raw(bucket,"GetObjectAttributes",Key="obj",ObjectAttributes=ATTRIBUTES),400)
        result=attributes(bucket,"obj",SSECustomerAlgorithm="AES256",SSECustomerKey="pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=",SSECustomerKeyMD5="DWygnHRtgiJ77HCm+1rvHw==")
        assert_equal("DeleteMarker" in result,False);assert_equal("VersionId" in result,False)
        assert_equal(result["ObjectSize"],1000);assert_equal(result["ETag"],etag)
        assert_equal(result["StorageClass"],"STANDARD");assert_equal("ObjectParts" in result,False)

def multipart(p,mode):
    """Request part-detail pagination only after explicit SHA256 upload; verify independent digests, exact page rows and full readback."""
    size=5*MIB if mode=="single" else 30*MIB
    key="multipart";part_size=5*MIB
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,key,"SHA256",check_algorithm=False) as upload_id:
            parts=[];digests=[];payload=source_data(size)
            for number,offset in enumerate(range(0,size,part_size),1):
                block=payload[offset:offset+part_size]
                digest=base64.b64encode(hashlib.sha256(block).digest()).decode();digests.append(digest)
                part=checksum_part(bucket,p,key,upload_id,number,block,"SHA256",digest)
                assert_equal(part["ChecksumSHA256"],digest);parts.append(part)
            complete=observed_call(bucket,"mp_complete_observed",key=key,upload_id=upload_id,parts=parts)
            etag=complete["etag"].strip('"');assert_equal(bool(etag),True)
            composite=base64.b64encode(hashlib.sha256(b"".join(base64.b64decode(d) for d in digests)).digest()).decode()
            assert_equal(complete["checksum"]["ChecksumSHA256"],composite+"-"+str(len(parts)))
            observed_call(bucket,"get",key=key,output_path=str(p.ctx.output));assert_equal(p.ctx.output.read_bytes(),payload)
            pages=({"MaxParts":1,"PartNumberMarker":3},{"MaxParts":10,"PartNumberMarker":4}) if mode=="paginated" else ({},)
            for page in pages:
                result=attributes(bucket,key,**page);actual=result["ObjectParts"]
                assert_equal(result["ObjectSize"],size);check_parts_count(actual,len(parts))
                assert_equal(result["ETag"],etag);assert_equal(result["StorageClass"],"STANDARD")
                assert_equal(result["Checksum"]["ChecksumSHA256"] in (composite,composite+"-"+str(len(parts))),True)
                start=page.get("PartNumberMarker",0);stop=min(len(parts),start+page.get("MaxParts",len(parts)))
                assert_equal(actual.get("IsTruncated",False),stop<len(parts))
                if page:
                    assert_equal(actual["MaxParts"],page["MaxParts"]);assert_equal(actual["PartNumberMarker"],start)
                    if stop<len(parts):assert_equal(actual["NextPartNumberMarker"],stop)
                assert_equal(len(actual["Parts"]),stop-start)
                for number,part in enumerate(actual["Parts"],start+1):
                    assert_equal(part["PartNumber"],number);assert_equal(part["Size"],part_size)
                    assert_equal(part["ChecksumSHA256"],digests[number-1])

SHA256="arcu6553sHVAiX4MjW0j7I7vD4w6R+Gz9Ok0Q9lTa+0="
PART_SHA256=("275VF5loJr1YYawit0XSHREhkFXYkkPKGuoK0x9VKxI=","mrHwOfjTL5Zwfj74F05HOQGLdUb7E5szdCbxgUSq6NM=","Vw7oB/nKQ5xWb3hNgbyfkvDiivl+U+/Dft48nfJfDow=")
COMPOSITE_SHA256="uWBwpe1dxI4Vw8Gf0X9ynOdw/SS6VBzfWm9giiv1sf4=-3"

def checksum_put(bucket,p,key,algorithm,value):
    return observed_call(bucket,"put_http",key=key,body_path=str(p.ctx.body),checksum={"ChecksumAlgorithm":algorithm,"Checksum"+algorithm:value})

def object_checksum(p,algorithm):
    field="Checksum"+algorithm;expected=SHA256 if algorithm=="SHA256" else "Qeh8oXvGiSo="
    with BucketScope(p) as bucket:
        p.ctx.body.write_bytes(b"A"*1024)
        response=checksum_put(bucket,p,"myobj",algorithm,expected)
        assert_equal(("PutObject response",response.get("checksum",{}).get(field)),("PutObject response",expected))
        assert_equal(field in observed_call(bucket,"head_http",key="myobj")["checksum"],False)
        assert_equal(observed_call(bucket,"head_http",key="myobj",checksum_mode="ENABLED")["checksum"][field],expected)
        # A malformed Base64 token tests syntax, not payload integrity. Submit a
        # correctly sized digest with a different body, then verify failed overwrite.
        wrong=base64.b64encode(bytes(len(base64.b64decode(expected,validate=True)))).decode()
        p.ctx.body.write_bytes(b"B"*1024)
        rejected(bucket.raw("put_http",key="myobj",body_path=str(p.ctx.body),checksum={"ChecksumAlgorithm":algorithm,field:wrong}),400,"BadDigest")
        observed_call(bucket,"get",key="myobj",output_path=str(p.ctx.output))
        assert_equal(p.ctx.output.read_bytes(),b"A"*1024)

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
    result=observed_call(bucket,"mp_create_metadata",key=key,checksum=checksum,**options)
    ident=result["upload_id"]
    primary=None
    try:
        if check_algorithm:assert_equal(result["checksum"]["ChecksumAlgorithm"],algorithm)
        yield ident
    except Exception as error:
        primary=error
        raise
    finally:
        try:
            result=bucket.raw("mp_abort_raw",key=key,upload_id=ident)
            if not result["ok"] and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):
                raise RuntimeError(str(result))
        except Exception as cleanup_error:
            raise RuntimeError((str(primary)+"; " if primary is not None else "")+"checksum multipart cleanup failed: "+str(cleanup_error)) from primary

def checksum_part(bucket,p,key,ident,number,payload,algorithm,value=None,include_checksum=True):
    p.ctx.body.write_bytes(payload);checksum={"ChecksumAlgorithm":algorithm}
    if value is not None:checksum["Checksum"+algorithm]=value
    response=observed_call(bucket,"mp_part",key=key,upload_id=ident,part_number=number,body_path=str(p.ctx.body),checksum=checksum)
    result={"part_number":number,"etag":response["etag"].strip('"')}
    if include_checksum:result["Checksum"+algorithm]=response["checksum"]["Checksum"+algorithm]
    return result

def checksum_complete(bucket,key,ident,parts,algorithm,value,checksum_type=None):
    checksum={"Checksum"+algorithm:value}
    if checksum_type is not None:checksum["ChecksumType"]=checksum_type
    return observe(bucket.raw("mp_complete_observed",key=key,upload_id=ident,parts=parts,checksum=checksum), "mp_complete_observed")


def multipart_bad_part_digest(p):
    """Independently test a validly encoded but incorrect part checksum; missing init echo must not suppress this rejection test."""
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,"bad-part-digest","SHA256",check_algorithm=False) as ident:
            p.ctx.body.write_bytes(b"foo")
            result=bucket.raw("mp_part",key="bad-part-digest",upload_id=ident,part_number=1,
                              body_path=str(p.ctx.body),checksum_when_required=True,checksum={"ChecksumSHA256":base64.b64encode(bytes(32)).decode()})
            rejected(result,400,"BadDigest")


def multipart_sha256(p):
    with BucketScope(p) as bucket:
        for key,mode in (("mymultipart","bad_digest"),("mymultipart2","missing_part_checksum"),("mymultipart3","success")):
            with checksum_upload(bucket,key,"SHA256") as ident:
                part=checksum_part(bucket,p,key,ident,1,b"A"*1024,"SHA256",SHA256)
                if mode=="missing_part_checksum":del part["ChecksumSHA256"]
                expected="Ok6Cs5b96ux6+MWQkJO7UBT5sKPBeXBLwvj/hK89smg=-1"
                response=checksum_complete(bucket,key,ident,[part],"SHA256",base64.b64encode(bytes(32)).decode()+"-1" if mode=="bad_digest" else expected)
                if mode!="success":
                    assert_equal(response["ok"],False)
                    allowed={(400,"BadDigest")} if mode=="bad_digest" else {(400,"InvalidRequest"),(400,"BadDigest")}
                    assert_equal((response.get("status_code"),response.get("error_code")) in allowed,True)
                    rejected(bucket.raw("get",key=key,output_path=str(p.ctx.output)),404,"NoSuchKey")
                    continue
                assert_equal(response["ok"],True);assert_equal(response["checksum"]["ChecksumSHA256"],expected)
                assert_equal("ChecksumSHA256" in observed_call(bucket,"head_http",key=key)["checksum"],False)
                assert_equal(observed_call(bucket,"head_http",key=key,checksum_mode="ENABLED")["checksum"]["ChecksumSHA256"],expected)

def reupload_checksum(p):
    with BucketScope(p) as bucket:
        key="mymultipart"
        with checksum_upload(bucket,key,"SHA256") as ident:
            parts=[checksum_part(bucket,p,key,ident,number,bytes([letter])*5*MIB,"SHA256",checksum) for number,(letter,checksum) in enumerate(zip(b"ABC",PART_SHA256),1)]
            first=checksum_complete(bucket,key,ident,parts,"SHA256",COMPOSITE_SHA256)
            assert_equal(first["ok"],True);assert_equal(bool(first["etag"]),True)
            assert_equal(first["checksum"]["ChecksumSHA256"],COMPOSITE_SHA256)
            second=checksum_complete(bucket,key,ident,parts,"SHA256",COMPOSITE_SHA256)
            if second["ok"]:
                assert_equal(first["etag"],second["etag"])
                if "ChecksumSHA256" in second.get("checksum",{}):assert_equal(first["checksum"]["ChecksumSHA256"],second["checksum"]["ChecksumSHA256"])
            else:rejected(second,404,"NoSuchUpload")
            observed=observed_call(bucket,"head_http",key=key,checksum_mode="ENABLED")
            assert_equal(observed["checksum"]["ChecksumSHA256"],COMPOSITE_SHA256)
            result=observed_call(bucket,"get",key=key,output_path=str(p.ctx.output))
            assert_equal(result["etag"],first["etag"])
            assert_equal(p.ctx.output.read_bytes(),b"A"*(5*MIB)+b"B"*(5*MIB)+b"C"*(5*MIB))

def multipart_checksum_attributes(p):
    key="multipart_checksum";size=64*MIB
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,key,"SHA256",check_algorithm=False,content_type="text/plain",metadata={"foo":"bar"}) as ident:
            payload=source_data(size);parts=[];checksums=[]
            for number,offset in enumerate(range(0,size,5*MIB),1):
                block=payload[offset:offset+5*MIB]
                expected=base64.b64encode(hashlib.sha256(block).digest()).decode()
                part=checksum_part(bucket,p,key,ident,number,block,"SHA256",expected)
                assert_equal(part["ChecksumSHA256"],expected)
                # Complete receives the actual service checksum; expectations hash input independently.
                parts.append(part)
                checksums.append(expected)
            response=observed_call(bucket,"mp_complete_observed",key=key,upload_id=ident,parts=parts)
            composite=base64.b64encode(hashlib.sha256(b"".join(base64.b64decode(c,validate=True) for c in checksums)).digest()).decode()
            upload_checksum=response["checksum"]["ChecksumSHA256"]
            assert_equal(upload_checksum,composite+"-"+str(len(parts)))
            observed_call(bucket,"get",key=key,output_path=str(p.ctx.output))
            actual=p.ctx.output.read_bytes()
            assert_equal(len(actual),len(payload))
            assert_equal(hashlib.sha256(actual).hexdigest(),hashlib.sha256(payload).hexdigest())
            result=attributes(bucket,key);assert_equal(result["ObjectSize"],size)
            # Attributes can expose the digest without the part-count suffix. Check the
            # independent digest AND actual part count, never just strip an arbitrary suffix.
            observed=result["Checksum"]["ChecksumSHA256"]
            assert_equal(observed in (composite,upload_checksum),True)
            check_parts_count(result["ObjectParts"],len(parts))
            assert_equal(len(result["ObjectParts"]["Parts"]),len(parts))
            assert_equal(result["ObjectParts"].get("IsTruncated",False),False)
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
    """Verify explicit completion type, interface-specific digests and actual full/part bytes."""
    checksum_type,part_checksums,composite=CHECKSUM_VECTORS[algorithm]
    field="Checksum"+algorithm;key="mymultipart3"
    payloads=[bytes([letter])*5*MIB for letter in b"ABC"]
    with BucketScope(p) as bucket:
        with checksum_upload(bucket,key,algorithm,checksum_type) as ident:
            parts=[checksum_part(bucket,p,key,ident,number,payload,algorithm,checksum)
                   for number,(payload,checksum) in enumerate(zip(payloads,part_checksums),1)]
            result=checksum_complete(bucket,key,ident,parts,algorithm,composite,checksum_type)
            assert_equal(result["ok"],True);assert_equal(result["checksum"]["ChecksumType"],checksum_type)
            assert_equal(result["checksum"][field],composite)
            assert_equal(field in observed_call(bucket,"head_http",key=key)["checksum"],False)
            observed=observed_call(bucket,"head_http",key=key,checksum_mode="ENABLED")["checksum"]
            assert_equal(observed["ChecksumType"],checksum_type);assert_equal(observed[field],composite)
            attributes_result=api(bucket,"GetObjectAttributes",Key=key,ObjectAttributes=["Checksum","ObjectParts"])["data"]
            observed=attributes_result["Checksum"]
            assert_equal(observed["ChecksumType"],checksum_type)
            actual_parts=attributes_result["ObjectParts"]
            check_parts_count(actual_parts,len(parts))
            if checksum_type=="COMPOSITE":
                # Attributes may omit the count suffix; never discard an incorrect suffix.
                digest=base64.b64encode(hashlib.new(algorithm.lower(),b"".join(
                    hashlib.new(algorithm.lower(),payload).digest() for payload in payloads)).digest()).decode()
                assert_equal(observed[field] in (digest,digest+"-"+str(len(parts))),True)
                assert_equal(actual_parts.get("IsTruncated",False),False)
                assert_equal(len(actual_parts["Parts"]),len(parts))
                for number,part in enumerate(actual_parts["Parts"],1):
                    assert_equal(part["PartNumber"],number);assert_equal(part["Size"],len(payloads[number-1]))
                    assert_equal(part[field],part_checksums[number-1])
            else:
                assert_equal(observed[field],composite)
            observed_call(bucket,"get",key=key,output_path=str(p.ctx.output))
            assert_equal(p.ctx.output.read_bytes(),b"".join(payloads))
            for number,expected in enumerate(part_checksums,1):
                result=observed_call(bucket,"get_part",key=key,part_number=number,
                                     checksum_mode="ENABLED",output_path=str(p.ctx.output))
                assert_equal(result["size"],len(payloads[number-1]))
                assert_equal(p.ctx.output.read_bytes(),payloads[number-1])
                # FULL_OBJECT describes the entire object, not a partial GET.
                # AWS omits checksum headers for these partial responses. If a
                # service returns part checksums, still verify every returned value.
                checks=result["checksum"]
                if checksum_type=="COMPOSITE" or checks:
                    assert_equal(checks["ChecksumType"],checksum_type)
                    assert_equal(checks[field],expected)

BASE=SCOPE_OPERATIONS+" api.GetObjectAttributes"
CASES=(case("ceph_attributes_object","Ceph 普通对象属性","保留五类属性选择、ETag/大小/存储类型和 VersionId/DeleteMarker/ObjectParts 缺失规则",BASE+" put_http",ordinary,"test_get_object_attributes",CEPH),
       case("ceph_attributes_versioned","Ceph 版本对象属性","保留实际 VersionId、覆盖后指定旧版本以及全部缺字段断言",VERSION_SCOPE_OPERATIONS+" api.GetObjectAttributes versioning_get versioning_put put_version",versioned,"test_get_versioned_object_attributes",CEPH),
       case("ceph_attributes_sse_customer","Ceph SSE-C 对象属性","保留原始密钥和 MD5、缺密钥 400 以及解密属性和缺字段规则",BASE+" put_sse_c",sse_customer,"test_get_sse_c_encrypted_object_attributes",CEPH))
CASES+=tuple(case("ceph_attributes_multipart_"+mode,"Ceph 分块对象属性 "+mode,"显式SHA256上传后验证分块详情、精确分页、独立摘要及完整内容",BASE+" checksum_sha256 get mp_create_metadata mp_part mp_complete_observed mp_abort_raw",lambda p,mode=mode:multipart(p,mode),reference,CEPH) for mode,reference in (("ordinary","test_get_multipart_object_attributes"),("paginated","test_get_paginated_multipart_object_attributes"),("single","test_get_single_multipart_object_attributes")))

CHECKSUM_BASE=SCOPE_OPERATIONS+" checksum_sha256 put_http head_http get"
CASES+=(case("ceph_checksum_object_sha256","Ceph 单对象 SHA256","保留固定 Base64 校验和、默认 HEAD 不返回、启用 HEAD 返回和坏摘要 400/BadDigest",CHECKSUM_BASE,lambda p:object_checksum(p,"SHA256"),"test_object_checksum_sha256",CEPH),
        case("ceph_checksum_object_crc64nvme","Ceph 单对象 CRC64NVME","保持原始 CRC64NVME 摘要与坏摘要预期；SDK 无类型能力明确 not_run",SCOPE_OPERATIONS+" checksum_crc64nvme put_http head_http get",lambda p:object_checksum(p,"CRC64NVME"),"test_object_checksum_crc64nvme",CEPH),
        case("ceph_checksum_attributes_sha256","Ceph 校验和对象属性","保留固定校验和、实际 PUT ETag 和五类属性及 ObjectParts 缺失断言",SCOPE_OPERATIONS+" checksum_sha256 put_http api.GetObjectAttributes",checksum_attributes,"test_get_checksum_object_attributes",CEPH),
        case("ceph_checksum_multipart_sha256","Ceph 分块 SHA256 完成错误与成功","分别验证合法格式坏摘要、缺失分块摘要的拒绝与对象不存在，以及正确复合摘要与HEAD",CHECKSUM_BASE+" checksum_sha256 get mp_create_metadata mp_part mp_complete_observed mp_abort_raw",multipart_sha256,"test_multipart_checksum_sha256",CEPH),
        case("ceph_checksum_multipart_reupload","Ceph 分块校验和重复完成","三块上传后重复完成，允许一致成功或NoSuchUpload，HEAD摘要和完整GET必须一致",SCOPE_OPERATIONS+" checksum_sha256 get head_http mp_create_metadata mp_part mp_complete_observed mp_abort_raw",reupload_checksum,"test_multipart_reupload_checksum_and_etag",CEPH),
        case("ceph_checksum_attributes_multipart","Ceph 64 MiB 校验和分块属性","保留 64 MiB 原始分块形状、实际完成摘要、各分块独立摘要和最后分块大小",SCOPE_OPERATIONS+" checksum_sha256 mp_create_metadata mp_part mp_complete_observed mp_abort_raw get api.GetObjectAttributes",multipart_checksum_attributes,"test_get_multipart_checksum_object_attributes",CEPH))

CASES+=tuple(case("ceph_checksum_helper_"+algorithm.lower(),"Ceph 三分块 "+algorithm+" "+checksum_type,"显式完成校验和类型、属性摘要与分片明细、完整及逐块内容校验",SCOPE_OPERATIONS+" checksum_type checksum_"+algorithm.lower()+" mp_create_metadata mp_part mp_complete_observed mp_abort_raw head_http api.GetObjectAttributes get get_part",lambda p,algorithm=algorithm:checksum_type_helper(p,algorithm),"test_multipart_use_cksum_helper_"+algorithm.lower(),CEPH) for algorithm,(checksum_type,_,__) in CHECKSUM_VECTORS.items())

CASES+=(case("ceph_checksum_multipart_bad_part_digest","分片错误 SHA256 拒绝","独立发送格式合法但与分片内容不符的摘要，必须返回 400/BadDigest；不以创建上传响应头缺失跳过此验证",SCOPE_OPERATIONS+" checksum_sha256 checksum_when_required mp_create_metadata mp_part mp_abort_raw",multipart_bad_part_digest,"test_multipart_checksum_sha256",CEPH),)

def register(registry):
    registry.add_suite(Suite("ceph_checksum_attributes",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
