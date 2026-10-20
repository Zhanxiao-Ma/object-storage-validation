"""Exercise every original Ceph encryption-copy parameter instance through real SDKs and independent HTTP documents."""
import hashlib
import os
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote,urlsplit,parse_qs
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker,FixtureHandler
from test_lifecycle_expiration_sdk import XML,scope_begin,scope_end
from osv.ceph_encrypted_copy_matrix import CASES,MIB,matrix,SOURCE_CUSTOMER,DEST_CUSTOMER
from osv.scenarios import Context
from osv.suites import Probe,run_cases

SOURCE_KEY="configured-main-kms"
DEST_KEY="configured-secondary-kms"
MODES=("unencrypted","sse-s3","sse-c","sse-kms")
CLASSES=("STANDARD","STANDARD_IA")


class DigestRequests(list):
    """Bound large request observations before the fixture replies, preserving exact byte length/hash without retaining gigabytes or racing subsequent SDK requests."""
    def append(self,row):
        method,path,headers,body=row
        if len(body)>4096:body={"size":len(body),"sha256":hashlib.sha256(body).hexdigest()}
        super().append((method,path,headers,body))

@lru_cache(maxsize=12)
def data(letter,size):return (b"A"*size+b"B"*size) if letter=="AB" else letter.encode()*size

def response_headers(mode):
    if mode=="sse-s3":return {"x-amz-server-side-encryption":"AES256"}
    if mode=="sse-c":return {"x-amz-server-side-encryption-customer-algorithm":"AES256","x-amz-server-side-encryption-customer-key-md5":"arxBvwY2V4SiOne6yppVPQ=="}
    if mode=="sse-kms":return {"x-amz-server-side-encryption":"aws:kms","x-amz-server-side-encryption-aws-kms-key-id":DEST_KEY}
    return {}

def plans(multipart):
    # Specify the upstream Cartesian input domain independently of the production generator.
    return [(source,destination,source_class,destination_class,size) for source in MODES for destination in MODES for source_class in CLASSES for destination_class in CLASSES for size in ((8*MIB,) if multipart else (1,1024,MIB,8*MIB))]

def vector_documents(values,multipart,index):
    source,dest,source_class,dest_class,size=values
    source_headers={"x-amz-server-side-encryption":"AES256"} if source=="sse-s3" else {}
    dest_headers=response_headers(dest)
    docs=scope_begin()+[(200,source_headers,b"")]+scope_begin()
    if not multipart:
        copied=("<CopyObjectResult><ETag>opaque-copy-"+str(index)+"</ETag></CopyObjectResult>").encode()
        docs += [(200,{**XML,**dest_headers},copied),(200,dest_headers,data("A",size))]
    else:
        upload="server-upload-opaque-"+str(index);copied='"server-copy-part-'+str(index)+'"';temporary='"server-temp-part-'+str(index)+'"'
        init=("<InitiateMultipartUploadResult><UploadId>"+upload+"</UploadId></InitiateMultipartUploadResult>").encode()
        part=("<CopyPartResult><ETag>"+copied+"</ETag></CopyPartResult>").encode()
        bad=(400,XML,b"<Error><Code>InvalidRequest</Code><Message>independent SSE-C rejection</Message></Error>")
        docs += [(200,{**XML,**dest_headers},init)]
        if dest=="sse-c":docs += [bad,bad]
        docs += [(200,{**XML,**dest_headers},part)]
        if dest=="sse-c":docs += [bad,bad]
        docs += [(200,{**dest_headers,"ETag":temporary},b"")]
        if dest=="sse-c":docs += [bad,bad]
        docs += [(200,{**XML,**dest_headers},b"<CompleteMultipartUploadResult><ETag>opaque-complete</ETag></CompleteMultipartUploadResult>"),(200,dest_headers,data("AB",size)),(404,XML,b"<Error><Code>NoSuchUpload</Code><Message>already completed</Message></Error>")]
    return docs+scope_end(("testobj2",))+scope_end(("testobj",))

def headers(request):return {key.lower():value for key,value in request[2].items()}

def body_digest(body):return body if isinstance(body,dict) else {"size":len(body),"sha256":hashlib.sha256(body).hexdigest()}

def assert_vector_wire(test,requests,values,multipart,index):
    source,dest,source_class,dest_class,size=values
    puts=[row for row in requests if row[0]=="PUT" and unquote(urlsplit(row[1]).path).endswith("/testobj")]
    test.assertEqual(len(puts),1)
    original=puts[0];original_path=unquote(urlsplit(original[1]).path).lstrip("/")
    test.assertEqual(body_digest(original[3]),{"size":size,"sha256":hashlib.sha256(data("A",size)).hexdigest()})
    test.assertEqual(headers(original).get("x-amz-storage-class"),source_class)
    test.assertEqual(headers(original).get("x-amz-server-side-encryption"),{"sse-s3":"AES256","sse-kms":"aws:kms"}.get(source))
    if source=="sse-c":test.assertEqual(headers(original)["x-amz-server-side-encryption-customer-key"],SOURCE_CUSTOMER["SSECustomerKey"])
    if source=="sse-kms":test.assertEqual(headers(original)["x-amz-server-side-encryption-aws-kms-key-id"],SOURCE_KEY)
    copied=[row for row in requests if row[0]=="PUT" and "x-amz-copy-source" in headers(row)]
    test.assertEqual(len(copied),3 if multipart and dest=="sse-c" else 1)
    for offset,request in enumerate(copied):
        test.assertEqual(unquote(headers(request)["x-amz-copy-source"]),original_path)
        expected_source_customer=SOURCE_CUSTOMER["SSECustomerKey"] if source=="sse-c" or (multipart and dest=="sse-c" and offset==1) else None
        test.assertEqual(headers(request).get("x-amz-copy-source-server-side-encryption-customer-key"),expected_source_customer)
        expected_dest_customer=DEST_CUSTOMER["SSECustomerKey"] if dest=="sse-c" and offset==len(copied)-1 else None
        test.assertEqual(headers(request).get("x-amz-server-side-encryption-customer-key"),expected_dest_customer)
        test.assertEqual(headers(request).get("x-amz-storage-class"),None if multipart else dest_class)
    if multipart:
        create=[row for row in requests if row[0]=="POST" and "uploads" in row[1]]
        test.assertEqual(len(create),1);test.assertEqual(headers(create[0]).get("x-amz-storage-class"),dest_class)
        parts=[row for row in requests if row[0]=="PUT" and "partNumber=2" in row[1]]
        test.assertEqual(len(parts),3 if dest=="sse-c" else 1)
        for request in parts:test.assertEqual(body_digest(request[3]),{"size":size,"sha256":hashlib.sha256(data("B",size)).hexdigest()})
        test.assertEqual([headers(row).get("x-amz-server-side-encryption-customer-key") for row in parts],[None,SOURCE_CUSTOMER["SSECustomerKey"],DEST_CUSTOMER["SSECustomerKey"]] if dest=="sse-c" else [None])
        complete=[row for row in requests if row[0]=="POST" and "uploadId" in row[1]]
        test.assertEqual(len(complete),3 if dest=="sse-c" else 1)
        for request in copied+parts+complete:
            test.assertEqual(parse_qs(urlsplit(request[1]).query)["uploadId"],["server-upload-opaque-"+str(index)])
        for request in complete:
            root=ET.fromstring(request[3]);test.assertEqual([node.text for node in root.findall(".//{*}Part/{*}ETag")],['"server-copy-part-'+str(index)+'"','"server-temp-part-'+str(index)+'"'])
            test.assertEqual([node.text for node in root.findall(".//{*}Part/{*}PartNumber")],["1","2"])
            test.assertNotIn("x-amz-storage-class",headers(request))
        test.assertEqual([headers(row).get("x-amz-server-side-encryption-customer-key") for row in complete],[None,SOURCE_CUSTOMER["SSECustomerKey"],DEST_CUSTOMER["SSECustomerKey"]] if dest=="sse-c" else [None])
    reads=[row for row in requests if row[0]=="GET" and unquote(urlsplit(row[1]).path).endswith("/testobj2")]
    test.assertEqual(len(reads),1);test.assertEqual(headers(reads[0]).get("x-amz-server-side-encryption-customer-key"),DEST_CUSTOMER["SSECustomerKey"] if dest=="sse-c" else None)

@unittest.skipUnless(os.environ.get("OSV_RUN_ENCRYPTED_COPY_MATRIX_TESTS")=="1","enable actual SDK workers")
class EncryptedCopyMatrixSdkTest(unittest.TestCase):
    """Keep the original full parameter domains and independent wire/header/body/ETag expectations without implementing a fake storage engine."""
    def test_defaults_keep_missing_kms_prerequisites_visible(self):
        """Omit independent source/destination KMS metadata; expect both complete matrices not_run with no HTTP, rather than silently dropping their KMS instances."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})
                result=run_cases(context,CASES);self.assertEqual([row["status"] for row in result],["not_run","not_run"]);self.assertEqual(server.requests,[])

    def test_full_four_by_four_two_class_domains_and_all_original_sizes(self):
        """Execute all 256 copy and 64 multipart-copy instances per SDK, including all six SSE-C 400 paths; expect actual opaque producer IDs/ETags in completion and exact real SDK encryption/storage/payload inputs and read-back."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for multipart in (False,True):
                inputs=plans(multipart)
                with self.subTest(language=language,multipart=multipart),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",handler=FixtureHandler) as (worker,server):
                    server.requests=DigestRequests()
                    path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"storage_classes":list(CLASSES),"kms_key_id":SOURCE_KEY,"secondary_kms_key_id":DEST_KEY})
                    case=CASES[1 if multipart else 0]
                    if case.operations-worker.capabilities:
                        self.assertEqual(run_cases(context,[case])[0]["status"],"not_run");self.assertEqual(server.requests,[]);continue
                    # Keep large wire observations bounded while the production matrix
                    # advances unchanged; one observation callback audits each finished
                    # source vector without replacing any SDK consumer or scenario.
                    from osv import ceph_encrypted_copy_matrix as implementation
                    original=implementation.part_vector if multipart else implementation.copy_vector
                    completed=[]
                    def observe(probe,*values):
                        index=len(completed);expected=inputs[index];self.assertEqual(tuple(values),expected)
                        documents=vector_documents(expected,multipart,index);server.fixtures.extend(documents)
                        original(probe,*values)
                        self.assertEqual(len(server.requests),len(documents))
                        assert_vector_wire(self,server.requests,expected,multipart,index)
                        server.requests.clear();completed.append(expected)
                    from unittest.mock import patch
                    with patch.object(implementation,"part_vector" if multipart else "copy_vector",side_effect=observe):evidence=case.execute(Probe(context))
                    self.assertEqual(completed,inputs);self.assertEqual(evidence["parameter_instances"],64 if multipart else 256)
                    self.assertEqual(evidence["source_modes"],list(MODES));self.assertEqual(evidence["destination_modes"],list(MODES))

    def test_real_upload_id_is_aborted_when_initial_encryption_oracle_fails(self):
        """Return a real multipart UploadId with a wrong SSE-C response MD5; expect the original header assertion to fail while that exact upload is aborted before both owned buckets are deleted."""
        from osv.ceph_encrypted_copy_matrix import part_vector
        values=("unencrypted","sse-c","STANDARD","STANDARD",8*MIB)
        documents=vector_documents(values,True,17)[:6]
        status,old_headers,payload=documents[-1]
        documents[-1]=(status,{**old_headers,"x-amz-server-side-encryption-customer-key-md5":"wrong-initial-md5"},payload)
        documents += [(204,{},b"")]+scope_end(("testobj2",))+scope_end(("testobj",))
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3") as (worker,server):
                server.requests=DigestRequests()
                path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"kms_key_id":SOURCE_KEY,"secondary_kms_key_id":DEST_KEY})
                with self.assertRaisesRegex(AssertionError,"wrong-initial-md5"):part_vector(Probe(context),*values)
                aborted=[(index,row) for index,row in enumerate(server.requests) if row[0]=="DELETE" and "uploadId" in row[1]]
                self.assertEqual(len(aborted),1);self.assertEqual(parse_qs(urlsplit(aborted[0][1][1]).query)["uploadId"],["server-upload-opaque-17"])
                bucket_deletes=[index for index,row in enumerate(server.requests) if row[0]=="DELETE" and urlsplit(row[1]).path.count("/")==1]
                self.assertEqual(len(bucket_deletes),2);self.assertLess(aborted[0][0],min(bucket_deletes))
                self.assertEqual(len(server.requests),len(documents))

    def test_ambiguous_create_uses_actual_owned_multipart_listing_before_bucket_delete(self):
        """Make real CreateMultipartUpload return 500, disconnect, or omit UploadId; expect the source failure to remain visible and actual ListMultipartUploads IDs to drive abort before bucket deletion."""
        import socket
        from osv.ceph_encrypted_copy_matrix import part_vector
        class DropCreate(FixtureHandler):
            """Produce a real connection failure for one literal fixture sentinel, without inventing an SDK error response."""
            def respond(self):
                if getattr(self.server,"drop_create",False) and self.command=="POST" and "uploads" in self.path:
                    body=self.rfile.read(int(self.headers.get("Content-Length","0")))
                    self.server.requests.append((self.command,self.path,dict(self.headers),body))
                    self.disconnect()
                return super().respond()
            do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond
            def disconnect(self):
                    self.close_connection=True
                    try:self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    self.connection.close()
                    raise ConnectionResetError("deliberate fixture disconnect before CreateMultipartUpload response")
            def send_response(self,code,message=None):
                if code==599:
                    self.server.drop_create=True;self.disconnect()
                return super().send_response(code,message)
        ident="discovered+/%?&=upload"
        discovered=b"<ListMultipartUploadsResult><IsTruncated>false</IsTruncated><Upload><Key>testobj2</Key><UploadId>discovered+/%?&amp;=upload</UploadId></Upload></ListMultipartUploadsResult>"
        empty=b"<ListMultipartUploadsResult><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>"
        prefix=scope_begin()+[(200,{},b"")]+scope_begin()
        alternatives=((500,(500,XML,b"<Error><Code>InternalError</Code><Message>unknown creation outcome</Message></Error>")),(599,(599,{},b"")),(200,(200,XML,b"<InitiateMultipartUploadResult/>")))
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for kind,bad in alternatives:
                documents=prefix+[bad,(200,{},b""),(200,XML,discovered),(204,{},b""),(200,XML,empty)]+scope_end(("testobj2",))[1:]+scope_end(("testobj",))
                with self.subTest(language=language,kind=kind),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3",handler=DropCreate) as (worker,server):
                    server.requests=DigestRequests()
                    path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"kms_key_id":SOURCE_KEY,"secondary_kms_key_id":DEST_KEY})
                    with self.assertRaises((AssertionError,KeyError,TypeError)):part_vector(Probe(context),"unencrypted","unencrypted","STANDARD","STANDARD",8*MIB)
                    aborts=[(index,row) for index,row in enumerate(server.requests) if row[0]=="DELETE" and "uploadId" in row[1]]
                    self.assertEqual(len(aborts),1);self.assertEqual(parse_qs(urlsplit(aborts[0][1][1]).query)["uploadId"],[ident])
                    actual_lists=[index for index,row in enumerate(server.requests) if row[0]=="GET" and "uploads" in row[1]]
                    self.assertEqual(len(actual_lists),2);self.assertLess(actual_lists[0],aborts[0][0]);self.assertLess(aborts[0][0],actual_lists[1])
                    bucket_deletes=[index for index,row in enumerate(server.requests) if row[0]=="DELETE" and urlsplit(row[1]).path.count("/")==1]
                    self.assertEqual(len(bucket_deletes),2);self.assertLess(actual_lists[1],min(bucket_deletes))
                    retry_creates=sum(row[0]=="POST" and "uploads" in row[1] for row in server.requests)-1
                    self.assertEqual(len(server.requests),len(documents)+retry_creates)

    def test_real_copy_part_and_upload_etags_reach_typed_completion_unchanged(self):
        """Feed independent opaque quoted part ETags and UploadId through actual SDK producer responses; expect those exact values in typed completion XML and a real A+B read-back."""
        from osv.ceph_encrypted_copy_matrix import part_vector
        values=("unencrypted","unencrypted","STANDARD","STANDARD_IA",8*MIB)
        documents=vector_documents(values,True,91)
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3") as (worker,server):
                server.requests=DigestRequests()
                path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"kms_key_id":SOURCE_KEY,"secondary_kms_key_id":DEST_KEY})
                part_vector(Probe(context),*values)
                assert_vector_wire(self,server.requests,values,True,91)
                self.assertEqual(len(server.requests),len(documents))
