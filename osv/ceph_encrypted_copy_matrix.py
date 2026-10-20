"""Preserve the complete Ceph source/destination encryption, storage-class and size matrices for copy and multipart copy."""
from contextlib import contextmanager
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .ceph_state_boundaries import header
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

MIB=1024*1024
MODES=("unencrypted","sse-s3","sse-c","sse-kms")
SOURCE_CUSTOMER={"SSECustomerAlgorithm":"AES256","SSECustomerKey":"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=","SSECustomerKeyMD5":"DWygnHRtgiJ77HCm+1rvHw=="}
DEST_CUSTOMER={"SSECustomerAlgorithm":"AES256","SSECustomerKey":"6b+WOZ1T3cqZMxgThRcXAQBrS5mXKdDUphvpxptl9/4=","SSECustomerKeyMD5":"arxBvwY2V4SiOne6yppVPQ=="}
CUSTOMER_HEADERS={"SSECustomerAlgorithm":"x-amz-server-side-encryption-customer-algorithm","SSECustomerKey":"x-amz-server-side-encryption-customer-key","SSECustomerKeyMD5":"x-amz-server-side-encryption-customer-key-md5"}

def customer_headers(values):return {CUSTOMER_HEADERS[key]:value for key,value in values.items()}

def settings(p):
    classes=p.ctx.workload.get("storage_classes",["STANDARD"])
    if not classes:raise CaseNotRun("the original parameter generator requires configured storage classes","workload.storage_classes")
    missing=["workload."+key for key in ("kms_key_id","secondary_kms_key_id") if not p.ctx.workload.get(key)]
    if missing:raise CaseNotRun("all original KMS source and destination parameter instances require their configured independent keys",*missing)
    return classes

def mode_arguments(p,mode,destination=False):
    if mode=="sse-s3":return {"ServerSideEncryption":"AES256"}
    if mode=="sse-c":return dict(DEST_CUSTOMER if destination else SOURCE_CUSTOMER)
    if mode=="sse-kms":return {"ServerSideEncryption":"aws:kms","SSEKMSKeyId":p.ctx.workload["secondary_kms_key_id" if destination else "kms_key_id"]}
    return {}

def source_copy_arguments(mode):return {"CopySource"+key:value for key,value in SOURCE_CUSTOMER.items()} if mode=="sse-c" else {}

def assert_encryption(p,response,mode,destination=False):
    # Source sse-c/KMS modes have no assert callback in the pinned upstream map.
    if not destination and mode!="sse-s3":return
    if mode=="sse-s3":assert_equal(header(response,"x-amz-server-side-encryption"),"AES256")
    elif mode=="sse-c":
        assert_equal(header(response,"x-amz-server-side-encryption-customer-algorithm"),"AES256")
        assert_equal(header(response,"x-amz-server-side-encryption-customer-key-md5"),DEST_CUSTOMER["SSECustomerKeyMD5"])
    elif mode=="sse-kms":
        assert_equal(header(response,"x-amz-server-side-encryption"),"aws:kms")
        assert_equal(header(response,"x-amz-server-side-encryption-aws-kms-key-id"),p.ctx.workload["secondary_kms_key_id"])

def upload_source(bucket,p,data,mode,storage):
    p.ctx.body.write_bytes(data);options={"storage_class":storage}
    if mode=="sse-c":operation="put_sse_c";options["sse_headers"]=customer_headers(SOURCE_CUSTOMER)
    elif mode in ("sse-s3","sse-kms"):
        operation="put_encryption";options["encryption_headers"]={"x-amz-server-side-encryption":"AES256" if mode=="sse-s3" else "aws:kms"}
        if mode=="sse-kms":options["encryption_headers"]["x-amz-server-side-encryption-aws-kms-key-id"]=p.ctx.workload["kms_key_id"]
    else:operation="put_http"
    result=bucket.call(operation,key="testobj",body_path=str(p.ctx.body),**options)
    assert_encryption(p,result,mode)

def download(bucket,p,mode,expected):
    p.ctx.output.unlink(missing_ok=True)
    response=bucket.call("get_sse_c" if mode=="sse-c" else "get_http",key="testobj2",output_path=str(p.ctx.output),**({"sse_headers":customer_headers(DEST_CUSTOMER)} if mode=="sse-c" else {}))
    assert_encryption(p,response,mode,True);assert_equal(p.ctx.output.read_bytes(),expected)

def copy_vector(p,source_mode,dest_mode,source_class,dest_class,size):
    data=b"A"*size
    with BucketScope(p) as source:
        upload_source(source,p,data,source_mode,source_class)
        with BucketScope(p) as destination:
            destination.written_keys.add("testobj2")
            options=mode_arguments(p,dest_mode,True);options.update(source_copy_arguments(source_mode));options["StorageClass"]=dest_class
            response=api(destination,"CopyObject",Key="testobj2",CopySource=source.name+"/testobj",**options)
            assert_encryption(p,response,dest_mode,True)
            download(destination,p,dest_mode,data)

class MultipartCopyDestination(BucketScope):
    """Prevent generic CreateMultipartUpload failures from leaking uploads: track actual IDs before assertions and resolve ambiguous results with real owned-bucket listings before deleting the bucket."""
    def __init__(self,probe):
        super().__init__(probe);self.pending_ids=set();self.unresolved_create=False

    def raw(self,operation,**options):
        if operation=="api.CreateMultipartUpload":self.unresolved_create=True
        result=super().raw(operation,**options)
        if operation=="api.CreateMultipartUpload" and result["ok"]:
            ident=result.get("data",{}).get("UploadId")
            if isinstance(ident,str) and ident:self.pending_ids.add(ident);self.unresolved_create=False
        if operation=="mp_abort_raw" and (result["ok"] or (result.get("status_code"),result.get("error_code"))==(404,"NoSuchUpload")):
            self.pending_ids.discard(options.get("upload_id"))
        return result

    def cleanup_objects(self):
        if self.unresolved_create or self.pending_ids:
            previous=None
            for attempt in range(10000):
                result=self.call("mp_list",max_uploads=1000)
                uploads=result["uploads"]
                if not uploads:
                    assert_equal(result.get("truncated"),False)
                    self.pending_ids.clear();self.unresolved_create=False;break
                signature=tuple((row["key"],row["upload_id"]) for row in uploads)
                if signature==previous:raise RuntimeError("owned multipart cleanup did not advance")
                previous=signature
                for row in uploads:
                    response=self.raw("mp_abort_raw",key=row["key"],upload_id=row["upload_id"])
                    if not response["ok"] and (response.get("status_code"),response.get("error_code"))!=(404,"NoSuchUpload"):
                        raise RuntimeError("actual owned multipart abort failed: "+str(response))
            else:raise RuntimeError("owned multipart cleanup exceeded page safety bound")
        return super().cleanup_objects()

@contextmanager
def pending_upload(destination,ident):
    try:yield
    finally:
        if isinstance(ident,str) and ident:
            result=destination.raw("mp_abort_raw",key="testobj2",upload_id=ident)
            if not result["ok"] and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):
                raise RuntimeError("cannot clean real matrix multipart upload: "+str(result))

def part_vector(p,source_mode,dest_mode,source_class,dest_class,size):
    data=b"A"*size
    with BucketScope(p) as source:
        upload_source(source,p,data,source_mode,source_class)
        with MultipartCopyDestination(p) as destination:
            destination.written_keys.add("testobj2")
            upload_options=mode_arguments(p,dest_mode,True);upload_options["StorageClass"]=dest_class
            response=api(destination,"CreateMultipartUpload",Key="testobj2",**upload_options)
            # Register a returned real upload ID for cleanup before any source
            # encryption-header assertion can fail. Preserve the source oracle order.
            observed_id=response["data"].get("UploadId")
            with pending_upload(destination,observed_id):
                assert_encryption(p,response,dest_mode,True)
                ident=response["data"]["UploadId"];assert_equal(bool(len(ident)),True)
                base={"Key":"testobj2","UploadId":ident,"PartNumber":1,"CopySource":source.name+"/testobj"}
                copy_options=source_copy_arguments(source_mode)
                if dest_mode=="sse-c":
                    rejected(destination.raw("api.UploadPartCopy",input={**base,**copy_options}),400)
                    wrong=dict(copy_options);wrong.update(source_copy_arguments("sse-c"))
                    # The source's second negative call adds CopySourceSSECustomer*
                    # again; it does not add destination SSECustomer* fields.
                    rejected(destination.raw("api.UploadPartCopy",input={**base,**wrong}),400)
                    copy_options.update({key:value for key,value in upload_options.items() if key!="StorageClass"})
                response=api(destination,"UploadPartCopy",**base,**copy_options)
                assert_encryption(p,response,dest_mode,True)
                parts=[{"ETag":response["data"]["CopyPartResult"]["ETag"],"PartNumber":1}]
                p.ctx.body.write_bytes(b"B"*size)
                body={"key":"testobj2","upload_id":ident,"part_number":2,"body_path":str(p.ctx.body)}
                if dest_mode=="sse-c":
                    rejected(destination.raw("mp_part",**body),400)
                    rejected(destination.raw("mp_part_sse_c",**body,sse_headers=customer_headers(SOURCE_CUSTOMER)),400)
                response=destination.call("mp_part_sse_c" if dest_mode=="sse-c" else "mp_part",**body,**({"sse_headers":customer_headers(DEST_CUSTOMER)} if dest_mode=="sse-c" else {}))
                assert_encryption(p,response,dest_mode,True)
                parts.append({"ETag":response["etag"],"PartNumber":2})
                complete={"Key":"testobj2","UploadId":ident,"MultipartUpload":{"Parts":parts}}
                if dest_mode=="sse-c":
                    rejected(destination.raw("api.CompleteMultipartUpload",input=complete),400)
                    rejected(destination.raw("api.CompleteMultipartUpload",input={**complete,**SOURCE_CUSTOMER}),400)
                response=api(destination,"CompleteMultipartUpload",**complete,**(DEST_CUSTOMER if dest_mode=="sse-c" else {}))
                assert_encryption(p,response,dest_mode,True)
                download(destination,p,dest_mode,data+b"B"*size)

def vectors(classes,multipart):
    for source in MODES:
        for destination in MODES:
            for source_class in classes:
                for dest_class in classes:
                    for size in ((8*MIB,) if multipart else (1,1024,MIB,8*MIB)):
                        yield source,destination,source_class,dest_class,size

def matrix(p,multipart=False):
    classes=settings(p);count=0
    for values in vectors(classes,multipart):
        (part_vector if multipart else copy_vector)(p,*values);count+=1
    return {"parameter_instances":count,"source_modes":list(MODES),"destination_modes":list(MODES),"storage_classes":classes,"object_sizes":[8*MIB] if multipart else [1,1024,MIB,8*MIB]}

BASE=SCOPE_OPERATIONS+" put_storage_class put_http put_encryption put_sse_c get_http get_sse_c"
CASES=(case("ceph_encrypted_copy_matrix","Ceph完整对象加密复制矩阵","四源模式×四目标模式×全部源/目标存储类别×1/1KiB/1MiB/8MiB；原响应加密头和全部正文断言",BASE+" api.CopyObject",matrix,"test_copy_enc",CEPH),
       case("ceph_encrypted_part_copy_matrix","Ceph完整分块加密复制矩阵","四源模式×四目标模式×全部源/目标类别×8MiB；原六次SSE-C负面400、真实uploadID/ETag完成输入、A+B正文和所有响应头",BASE+" api.CreateMultipartUpload api.UploadPartCopy api.CompleteMultipartUpload mp_part mp_part_sse_c mp_abort_raw mp_list",lambda p:matrix(p,True),"test_copy_part_enc",CEPH))

def register(registry):
    registry.add_suite(Suite("ceph_encrypted_copy_matrix",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
