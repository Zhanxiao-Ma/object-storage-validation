"""Drive the complete Ceph cloud-source scenarios through independent real SDK endpoints and literal service documents."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote,urlsplit,parse_qs
from xml.etree import ElementTree as ET
from sdk_fixtures import fixture_worker
from test_lifecycle_expiration_sdk import XML,LC,ENABLED,scope_begin,scope_end,put_result,object_doc
from osv.ceph_cloud_lifecycle import CASES
from osv.ceph_lifecycle_expiration import wait
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker

B0="osv-"+"1"*32
B1="osv-"+"2"*32
SC="GLACIER"
REGULAR="STANDARD_IA"
TARGET="independent-cloud-target"
TARGET_SC="STANDARD_IA"
DAYS="2030-01-01T00:00:00Z"

def listing(rows,name=B0):
    # Literal raw documents, not a transition, restore or storage simulator.
    return ("<ListVersionsResult><Name>"+name+"</Name><IsTruncated>false</IsTruncated>"+"".join("<Version><Key>"+key+"</Key><VersionId>"+version+"</VersionId><IsLatest>"+str(latest).lower()+"</IsLatest><StorageClass>"+storage+"</StorageClass></Version>" for key,version,storage,latest in rows)+"</ListVersionsResult>").encode()

def rows_result(rows,name=B0):return (200,XML,listing(rows,name))
def head(storage=SC,size=0):return (200,{"Content-Length":str(size),"x-amz-storage-class":storage},b"")
def get(body,storage=None):return (200,{"x-amz-storage-class":storage} if storage else {},body.encode() if isinstance(body,str) else body)
def error(status,code):return (status,XML,("<Error><Code>"+code+"</Code><Message>independent service error</Message></Error>").encode())
def lifecycle_delete():return (204,{},b"")
def restore_result():return (202,XML,b"<RestoreObjectOutput/>")

def remote_cleanup(rows,owned=False):
    return [(200,{},b"")]+([rows_result(rows,TARGET)]+[(204,{},b"")]*len(rows) if rows else [])+[rows_result([],TARGET)]+([(204,{},b""),(404,{},b"")] if owned else [])

def documents(name):
    """Return independently specified source phases and cloud responses, never compute backend lifecycle behavior from SDK requests."""
    shared={"storage_class":SC,"regular_storage_class":REGULAR,"retain_head_object":"true","target_path":TARGET,"target_storage_class":TARGET_SC,"allow_read_through":"false","target_by_bucket":False}
    if name in ("ordinary","multiple","large"):
        keys=("keep/multi","expire1/multi") if name=="large" else ("expire1/foo","expire1/bar","keep2/foo","keep2/bar")
        source=scope_begin()
        for i,key in enumerate(keys):source += [put_result(i)]+([get("A"*(9*1024*1024))] if name=="large" else [])
        source += [LC]
        if name!="large":source += [(200,XML,object_doc(keys))]
        first=[(key,"null",REGULAR if name=="multiple" and key.startswith("expire1/") else SC if key.startswith("expire1/") else "STANDARD",True) for key in keys]
        source += [rows_result(first)]
        cloud=[]
        if name=="multiple":
            second=[(key,version,SC if storage==REGULAR else storage,latest) for key,version,storage,latest in first]
            final=[row for row in second if row[2]=="STANDARD"]
            source += [rows_result(second),rows_result(final)]+scope_end(keys)
            cloud += remote_cleanup([(B0+"/expire1/foo","remote-v0",TARGET_SC,True),(B0+"/expire1/bar","remote-v1",TARGET_SC,True)])
        else:
            copied=keys[1:2] if name=="large" else keys[:2]
            cloud += [get("A"*(9*1024*1024) if name=="large" else key,TARGET_SC) for key in copied]
            if name=="ordinary":source += [head(),error(403,"InvalidObjectState"),head(),error(403,"InvalidObjectState"),(204,{},b""),error(404,"NoSuchKey")]
            source += scope_end(tuple(keys)+(("copy_obj",) if name=="ordinary" else ()))
            cloud += remote_cleanup([(B0+"/"+key,"remote-v"+str(i),TARGET_SC,True) for i,key in enumerate(copied)])
        return source,cloud,shared
    if name=="noncurrent":
        initial=[("test1/a","null","STANDARD",False),("test1/a","service-v0","STANDARD",False),("test1/a","service-v1","STANDARD",True),("test1/b","service-v2","STANDARD",False),("test1/b","service-v3","STANDARD",False),("test1/b","service-v4","STANDARD",True)]
        first=[(key,version,"STANDARD" if latest else REGULAR,latest) for key,version,_,latest in initial]
        final=[(key,version,"STANDARD" if latest else SC,latest) for key,version,_,latest in initial]
        source=scope_begin()+[(200,{},b""),(200,{},b""),ENABLED,LC]+[put_result(i) for i in range(5)]+[rows_result(initial),rows_result(initial),rows_result(first),rows_result(final),rows_result(final)]+scope_end(rows=[(key,version) for key,version,_,_ in final])
        copied=[(B0+"/"+key+"-"+version,"remote-v"+str(i),TARGET_SC,True) for i,(key,version,_,latest) in enumerate(final) if not latest]
        return source,remote_cleanup(copied),shared
    if name in ("target_bucket","target_buckets"):
        shared={**shared,"target_by_bucket":True}
        multiple=name=="target_buckets";keys=("only-in-a.txt","only-in-b.txt") if multiple else ("file1.txt","subdir/file2.txt")
        source=scope_begin()+(scope_begin() if multiple else [])
        if multiple:source += [put_result(0),put_result(1),LC,LC]
        else:source += [put_result(0),put_result(1),LC,(200,XML,object_doc(keys)),rows_result([(key,"null",SC,True) for key in keys])]
        source += [head(),lifecycle_delete(),restore_result(),head(),head(size=len("content-a" if multiple else keys[0]))]
        # ExitStack unwinds source bucket b before a.
        if multiple:source += scope_end((keys[1],))+scope_end((keys[0],))
        else:source += scope_end(keys)
        targets=["rgwx-default-"+SC.lower()+"-"+name for name in (B0,B1)]
        cloud=[(404,{},b"")]*(2 if multiple else 1)
        for index,key in enumerate(keys):cloud += [get(("content-a","content-b")[index] if multiple else key,TARGET_SC),error(404,"NoSuchKey")]
        if multiple:
            cloud += remote_cleanup([(keys[1],"remote-vb",TARGET_SC,True)],True)+remote_cleanup([(keys[0],"remote-va",TARGET_SC,True)],True)
        else:cloud += remote_cleanup([(key,"remote-v"+str(i),TARGET_SC,True) for i,key in enumerate(keys)],True)
        return source,cloud,shared
    if name in ("temporary","permanent","read_through"):
        key={"temporary":"test_restore_temp","permanent":"test_restore_perm","read_through":"test_restore_readthrough"}[name]
        body={"temporary":"temporary restore data","permanent":"permanent restore data","read_through":"restore data with readthrough"}[name]
        source=scope_begin()+[put_result(0),get(body),LC,head(),lifecycle_delete()]
        if name=="read_through":source += [error(403,"InvalidObjectState")]
        else:
            source += [restore_result()]+([head()] if name=="temporary" else [])+[head(size=len(body))]
            if name=="permanent":source += [head("STANDARD",len(body))]
            else:source += [restore_result(),head(size=len(body)),head(size=0)]
        return source+scope_end((key,)),remote_cleanup([(B0+"/"+key,"remote-retained",TARGET_SC,True)]),shared
    if name=="restore_noncurrent":
        initial=[("test1/a","service-v1","STANDARD",True),("test1/a","service-v0","STANDARD",False),("test1/a","null","STANDARD",False)]
        final=[(key,version,"STANDARD" if latest else SC,latest) for key,version,_,latest in initial]
        source=scope_begin()+[(200,{},b""),(200,{},b""),ENABLED,LC,put_result(0),put_result(1),rows_result(initial),rows_result(initial),rows_result(final),head(),restore_result(),head(size=9),rows_result(final),head(size=0)]+scope_end(rows=[(key,version) for key,version,_,_ in final])
        return source,remote_cleanup([(B0+"/test1/a-service-v0","remote-history",TARGET_SC,True),(B0+"/test1/a-null","remote-null",TARGET_SC,True)]),shared
    if name=="list_status":
        keys=("test_restore_status_temp","test_restore_status_perm")
        before=object_doc(keys)
        after=("<ListBucketResult><Contents><Key>"+keys[0]+"</Key><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress><RestoreExpiryDate>"+DAYS+"</RestoreExpiryDate></RestoreStatus></Contents><Contents><Key>"+keys[1]+"</Key><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress></RestoreStatus></Contents></ListBucketResult>").encode()
        source=scope_begin()+[put_result(0),put_result(1),(200,XML,before),(200,XML,before),LC,head(),head(),lifecycle_delete(),restore_result(),restore_result(),(200,XML,after),(200,XML,after),(200,XML,before)]+scope_end(keys)
        return source,remote_cleanup([(B0+"/"+key,"remote-status"+str(index),TARGET_SC,True) for index,key in enumerate(keys)]),shared
    if name=="list_version_status":
        initial=[("test1/a","service-v0","STANDARD",True),("test1/a","null","STANDARD",False)]
        final=[("test1/a","service-v0","STANDARD",True),("test1/a","null",SC,False)]
        raw=listing(final).decode().replace("<VersionId>null</VersionId>","<VersionId>null</VersionId><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress><RestoreExpiryDate>"+DAYS+"</RestoreExpiryDate></RestoreStatus>").encode()
        source=scope_begin()+[(200,{},b""),(200,{},b""),ENABLED,put_result(0),rows_result(initial),LC,rows_result(final),head(),lifecycle_delete(),restore_result(),(200,XML,raw),rows_result(final)]+scope_end(rows=[(key,version) for key,version,_,_ in final])
        return source,remote_cleanup([(B0+"/test1/a-null","remote-null",TARGET_SC,True)]),shared
    if name=="encrypted":
        source=[];keys=("expire1/foo","expire1/bar")
        for mode in ("unencrypted","sse-s3","sse-c","sse-kms"):
            for source_class,dest_class in (("STANDARD",REGULAR),(REGULAR,"STANDARD")):
                source += scope_begin()+[put_result(0),put_result(1),LC,(200,XML,object_doc(keys)),rows_result([(key,"null",dest_class,True) for key in keys])]+[get(key) for key in keys]+scope_end(keys)
        return source,[],{}
    raise ValueError(name)


@unittest.skipUnless(os.environ.get("OSV_RUN_CLOUD_LIFECYCLE_TESTS")=="1","enable actual Java/C++ workers")
class CloudLifecycleSdkTest(unittest.TestCase):
    """Verify complete native SDK source/cloud data paths separately from actual configured cloud lifecycle service certification."""
    def test_default_profile_never_invents_cloud_targets_or_service_timers(self):
        """Leave every service/target prerequisite unconfigured; expect all 13 source cases explicitly not_run before HTTP, without synthetic performance or compatibility passes."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})
                results=run_cases(context,CASES)
                self.assertEqual([row["status"] for row in results],["not_run"]*13)
                self.assertEqual(server.requests,[])

    def test_complete_source_and_separate_cloud_sdk_chain(self):
        """Feed independent source/cloud HTTP documents to real workers; expect all original stage, restore, absent-field and cross-endpoint read-back assertions, typed unsupported cases zero HTTP, and cleanup of only owned cloud versions."""
        expected_waits={"ordinary":[10,2,12],"multiple":[4,7,12],"noncurrent":[4,15,1],"large":[12,12],"target_bucket":[15,5,9],"target_buckets":[20,9],"temporary":[10,9,6,6],"permanent":[10,9],"read_through":[10],"restore_noncurrent":[7,6,6],"list_status":[10,9],"list_version_status":[7,9],"encrypted":[4]*8}
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for case in CASES:
                name=case.name.removeprefix("ceph_cloud_lc_");source,cloud,config=documents(name)
                with self.subTest(language=language,case=name),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,cloud,"s3") as (_,cloud_server):
                    cloud_endpoint="http://127.0.0.1:"+str(cloud_server.server_port)
                    overrides={"OSV_CLOUD_ENDPOINT":cloud_endpoint,"OSV_CLOUD_REGION":"us-east-1","OSV_CLOUD_ACCESS_KEY":"separate-cloud-access","OSV_CLOUD_SECRET_KEY":"separate-cloud-secret"}
                    with fixture_worker(language,source,"s3",overrides=overrides) as (worker,server):
                        path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"cloud_lifecycle":config,"lifecycle_debug_interval_seconds":.001,"restore_debug_interval_seconds":.002,"restore_processor_period_seconds":.003,"storage_classes":["STANDARD",REGULAR],"kms_key_id":"configured-main-kms"})
                        if case.operations-worker.capabilities:
                            result=run_cases(context,[case])[0];self.assertEqual(result["status"],"not_run");self.assertEqual(server.requests,[]);self.assertEqual(cloud_server.requests,[]);continue
                        with patch("osv.bucket_scope.uuid.uuid4",side_effect=[SimpleNamespace(hex=str(i)*32) for i in range(1,10)]),patch("osv.ceph_cloud_lifecycle.wait",wraps=wait) as waited,patch("osv.ceph_cloud_lifecycle.Worker",wraps=Worker) as created_workers:
                            evidence=case.execute(Probe(context))
                        self.assertEqual([round(item.args[0],6) for item in waited.call_args_list],[round(factor*.001,6) for factor in expected_waits[name]])
                        self.assertEqual(len(server.requests),len(source));self.assertEqual(len(cloud_server.requests),len(cloud))
                        for request in cloud_server.requests:
                            self.assertIn("separate-cloud-access",request[2]["Authorization"])
                        if name=="read_through":
                            self.assertEqual(len(created_workers.call_args_list),2)
                            created=created_workers.call_args_list[0]
                            self.assertEqual(created.args[0]["env"]["OSV_REQUEST_TIMEOUT_MS"],"100000")
                            self.assertEqual(created.args[0]["env"]["OSV_CONNECT_TIMEOUT_MS"],"100000")
                            self.assertGreaterEqual(created.args[1],100)
                        if name=="encrypted":
                            puts=[row for row in server.requests if row[0]=="PUT" and row[3] in (b"expire1/foo",b"expire1/bar")]
                            self.assertEqual(len(puts),16)
                            self.assertEqual([next(value for key,value in row[2].items() if key.lower()=="x-amz-storage-class") for row in puts],["STANDARD","STANDARD",REGULAR,REGULAR]*4)
                            rule_documents=[ET.fromstring(row[3]) for row in server.requests if row[0]=="PUT" and "lifecycle" in row[1]]
                            self.assertEqual([doc.findtext(".//{*}Transition/{*}StorageClass") for doc in rule_documents],[REGULAR,"STANDARD"]*4)
                        if name=="noncurrent":
                            self.assertIn("cloud read-back loop is empty",evidence["source_limitation"])
                            self.assertEqual(sum(row[0]=="GET" and "versions" not in row[1] for row in cloud_server.requests),0)
                        if name in ("ordinary","large"):
                            remote_reads=[unquote(row[1]) for row in cloud_server.requests if row[0]=="GET" and "versions" not in row[1]]
                            keys=("expire1/multi",) if name=="large" else ("expire1/foo","expire1/bar")
                            self.assertEqual(remote_reads,["/"+TARGET+"/"+B0+"/"+key for key in keys])
                        if name in ("target_bucket","target_buckets"):
                            remote_reads=[unquote(row[1]) for row in cloud_server.requests if row[0]=="GET" and "versions" not in row[1]]
                            pairs=[(B0,"only-in-a.txt"),(B1,"only-in-b.txt")] if name=="target_buckets" else [(B0,"file1.txt"),(B0,"subdir/file2.txt")]
                            self.assertEqual(remote_reads,["/rgwx-default-glacier-"+bucket+"/"+key for bucket,key in pairs])
                        if name in ("restore_noncurrent","list_version_status"):
                            requests=[row for row in server.requests if row[0]=="POST" and "restore" in row[1]]
                            self.assertEqual(len(requests),1)
                            self.assertIn("versionId="+("service-v0" if name=="restore_noncurrent" else "null"),requests[0][1])
                        for request in server.requests:
                            if request[0]=="POST" and "restore" in request[1] and request[3]:ET.fromstring(request[3])

    def test_restore_api_typed_fields_and_status_presence_on_both_sdks(self):
        """Exercise real RestoreObject and all three typed listing models directly; expect omitted Days for permanent restore, literal null VersionId and actual false/expiry presence, independent of Legacy Prefix support."""
        from osv.bucket_scope import BucketScope
        from osv.ceph_cloud_lifecycle import restore,list_api
        keys=("test_restore_status_temp","test_restore_status_perm")
        before=object_doc(keys)
        after=("<ListBucketResult><Contents><Key>"+keys[0]+"</Key><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress><RestoreExpiryDate>"+DAYS+"</RestoreExpiryDate></RestoreStatus></Contents><Contents><Key>"+keys[1]+"</Key><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress></RestoreStatus></Contents></ListBucketResult>").encode()
        raw=("<ListVersionsResult><Name>"+B0+"</Name><Version><Key>test1/a</Key><VersionId>null</VersionId><IsLatest>false</IsLatest><RestoreStatus><IsRestoreInProgress>false</IsRestoreInProgress><RestoreExpiryDate>"+DAYS+"</RestoreExpiryDate></RestoreStatus></Version></ListVersionsResult>").encode()
        docs=scope_begin()+[restore_result(),restore_result(),restore_result(),(200,XML,before),(200,XML,after),(200,XML,after),(200,XML,raw)]+scope_end()
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,docs,"s3") as (worker,server):
                path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})
                with BucketScope(Probe(context)) as bucket:
                    restore(bucket,keys[0],20);restore(bucket,keys[1]);restore(bucket,"test1/a",2,"null")
                    actual=list_api(bucket,"ListObjectsV2")["Contents"]
                    self.assertEqual(len(actual),2);self.assertTrue(all("RestoreStatus" not in row for row in actual))
                    for name in ("ListObjectsV2","ListObjects"):
                        actual={row["Key"]:row for row in list_api(bucket,name)["Contents"]}
                        self.assertFalse(actual[keys[0]]["RestoreStatus"]["IsRestoreInProgress"])
                        self.assertIn("RestoreExpiryDate",actual[keys[0]]["RestoreStatus"])
                        self.assertFalse(actual[keys[1]]["RestoreStatus"]["IsRestoreInProgress"])
                        self.assertNotIn("RestoreExpiryDate",actual[keys[1]]["RestoreStatus"])
                    version=list_api(bucket,"ListObjectVersions")["Versions"][0]
                    self.assertEqual(version["VersionId"],"null");self.assertFalse(version["IsLatest"])
                    self.assertFalse(version["RestoreStatus"]["IsRestoreInProgress"]);self.assertIn("RestoreExpiryDate",version["RestoreStatus"])
                posts=[row for row in server.requests if row[0]=="POST"]
                self.assertEqual([ET.fromstring(row[3]).findtext("{*}Days") if row[3] else None for row in posts],["20",None,"2"])
                self.assertIn("versionId=null",posts[2][1])
                optional=[row for row in server.requests if row[0]=="GET" and ("list-type" in row[1] or "versions" in row[1] or row[1].split("?",1)[0].count("/")==1)]
                self.assertEqual(sum(next((value for key,value in row[2].items() if key.lower()=="x-amz-optional-object-attributes"),None)=="RestoreStatus" for row in optional),4)
                self.assertEqual(len(server.requests),len(docs))

    def test_cloud_cleanup_consumes_real_version_ids_and_rejects_foreign_keys(self):
        """Pass actual SDK version listings into owned-prefix cleanup; expect exact opaque ID/key deletion, no shared-bucket deletion, foreign-key refusal and refusal to adopt a preexisting derived bucket."""
        from osv.ceph_cloud_lifecycle import own_cloud_prefix
        from osv.suites import CaseNotRun
        prefix=B0+"/";owned=prefix+"opaque +/key";ident="opaque+/%?&=version"
        from xml.sax.saxutils import escape
        raw=("<ListVersionsResult><IsTruncated>false</IsTruncated><Version><Key>"+escape(owned)+"</Key><VersionId>"+escape(ident)+"</VersionId></Version></ListVersionsResult>").encode()
        foreign=listing([("someone-else/key","foreign-version","STANDARD",True)])
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language,kind="owned"),fixture_worker(language,[(200,{},b""),(200,XML,raw),(204,{},b""),rows_result([])],"s3") as (worker,server):
                with own_cloud_prefix(worker,TARGET,prefix):pass
                deletes=[row for row in server.requests if row[0]=="DELETE"]
                self.assertEqual(len(deletes),1);self.assertEqual(parse_qs(urlsplit(deletes[0][1]).query)["versionId"],[ident]);self.assertEqual(unquote(urlsplit(deletes[0][1]).path),"/"+TARGET+"/"+owned)
            with self.subTest(language=language,kind="foreign"),fixture_worker(language,[(200,{},b""),(200,XML,foreign)],"s3") as (worker,server):
                with self.assertRaisesRegex(RuntimeError,"unowned key"):
                    with own_cloud_prefix(worker,TARGET,prefix):pass
                self.assertEqual([row[0] for row in server.requests],["HEAD","GET"])
            with self.subTest(language=language,kind="existing"),fixture_worker(language,[(200,{},b"")],"s3") as (worker,server):
                with self.assertRaises(CaseNotRun):
                    with own_cloud_prefix(worker,"rgwx-default-glacier-"+B0,"",True,source_bucket=B0):pass
                self.assertEqual([row[0] for row in server.requests],["HEAD"])

    def test_missing_cloud_identity_and_invalid_backend_timers_never_reach_http(self):
        """Omit distinct cloud credentials and supply invalid timer values; expect named prerequisites/configuration errors before SDK bucket or object requests."""
        from osv.ceph_cloud_lifecycle import settings,positive
        from osv.suites import CaseNotRun
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",overrides={"OSV_CLOUD_ENDPOINT":"","OSV_CLOUD_REGION":"","OSV_CLOUD_ACCESS_KEY":"","OSV_CLOUD_SECRET_KEY":""}) as (worker,server):
                path=Path(temporary)
                def probe(workload):return Probe(Context(worker,"unused/",path,b"independent",path/"body",path/"output",workload))
                base={"cloud_lifecycle":{"storage_class":SC},"lifecycle_debug_interval_seconds":.001}
                with self.assertRaises(CaseNotRun) as missing:settings(probe(base),remote=True)
                self.assertIn("OSV_CLOUD_ENDPOINT",missing.exception.requirements)
                for option in ({"regular":True},{"by_bucket":True},{"restore":True},{"expiry":True}):
                    with self.assertRaises(CaseNotRun):settings(probe(base),**option)
                for value in (0,-1,True,"1",float("nan"),float("inf"),10**1000):
                    with self.assertRaises(ValueError):positive(probe({"restore_debug_interval_seconds":value}),"restore_debug_interval_seconds")
                self.assertEqual(server.requests,[])

    def test_read_through_enabled_preserves_success_or_400_and_expiry_waits(self):
        """Enable genuine read-through configuration and use actual GET success/400/403 documents; expect the original two accepted paths, exact restore/expiry waits and 403 rejection rather than swallowing errors."""
        from osv.ceph_cloud_lifecycle import restore_current
        key="test_restore_readthrough";body="restore data with readthrough"
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for status in (200,400,403):
                base,_,config=documents("read_through");config={**config,"allow_read_through":"true","read_through_days":2}
                source=base[:7]+[get(body) if status==200 else error(status,"ReadThroughServiceError")]+([head(size=len(body)),head(size=0)] if status!=403 else [])+scope_end((key,))
                with fixture_worker(language,remote_cleanup([]),"s3") as (_,cloud_server):
                    overrides={"OSV_CLOUD_ENDPOINT":"http://127.0.0.1:"+str(cloud_server.server_port),"OSV_CLOUD_REGION":"us-east-1","OSV_CLOUD_ACCESS_KEY":"separate-cloud-access","OSV_CLOUD_SECRET_KEY":"separate-cloud-secret"}
                    with self.subTest(language=language,status=status),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,source,"s3",overrides=overrides) as (worker,server):
                        path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"cloud_lifecycle":config,"lifecycle_debug_interval_seconds":.001,"restore_debug_interval_seconds":.002,"restore_processor_period_seconds":.003})
                        case=next(case for case in CASES if case.name=="ceph_cloud_lc_read_through")
                        if case.operations-worker.capabilities:
                            self.assertEqual(run_cases(context,[case])[0]["status"],"not_run");self.assertEqual(server.requests,[]);continue
                        with patch("osv.ceph_cloud_lifecycle.wait",wraps=wait) as observed:
                            if status==403:
                                with self.assertRaisesRegex(AssertionError,"403 != 400"):restore_current(Probe(context),"read_through")
                            else:restore_current(Probe(context),"read_through")
                        self.assertEqual([round(call.args[0],6) for call in observed.call_args_list],[.01] if status==403 else [.01,.006,.012])
                        self.assertEqual(len(server.requests),len(source))

    def test_target_template_governs_both_real_cloud_endpoints_and_isolation(self):
        """Supply the original target bucket template with two source UUIDs; expect real independent cloud SDK reads and missing-key HEADs to use distinct fully substituted target names and owned-target cleanup."""
        case=next(case for case in CASES if case.name=="ceph_cloud_lc_target_buckets")
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            source,cloud,config=documents("target_buckets");config={**config,"target_by_bucket_prefix":"custom-${zonegroup}-${storage_class}-${bucket}"}
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,cloud,"s3") as (_,cloud_server):
                overrides={"OSV_CLOUD_ENDPOINT":"http://127.0.0.1:"+str(cloud_server.server_port),"OSV_CLOUD_REGION":"us-east-1","OSV_CLOUD_ACCESS_KEY":"separate-cloud-access","OSV_CLOUD_SECRET_KEY":"separate-cloud-secret"}
                with fixture_worker(language,source,"s3",overrides=overrides) as (worker,server):
                    path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"cloud_lifecycle":config,"lifecycle_debug_interval_seconds":.001,"restore_processor_period_seconds":.003})
                    if case.operations-worker.capabilities:
                        self.assertEqual(run_cases(context,[case])[0]["status"],"not_run");self.assertEqual(server.requests,[]);self.assertEqual(cloud_server.requests,[]);continue
                    with patch("osv.bucket_scope.uuid.uuid4",side_effect=[SimpleNamespace(hex="1"*32),SimpleNamespace(hex="2"*32)]):case.execute(Probe(context))
                    actual=[unquote(row[1]) for row in cloud_server.requests if row[0]=="GET" and "versions" not in row[1]]
                    self.assertEqual(actual,["/custom-default-glacier-"+B0+"/only-in-a.txt","/custom-default-glacier-"+B1+"/only-in-b.txt"])
                    self.assertTrue(any(row[0]=="HEAD" and "only-in-b.txt" in row[1] and B0 in row[1] for row in cloud_server.requests))
                    self.assertTrue(any(row[0]=="HEAD" and "only-in-a.txt" in row[1] and B1 in row[1] for row in cloud_server.requests))
                    self.assertEqual(len(server.requests),len(source));self.assertEqual(len(cloud_server.requests),len(cloud))

    def test_every_cloud_write_path_requires_remote_credentials_before_source_http(self):
        """Configure all service timers/classes but omit remote credentials; every cloud-writing source path, including restore/status/multiple and encrypted cloud destinations, must be not_run before any source or cloud HTTP."""
        from osv.suites import CaseNotRun
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3",overrides={"OSV_CLOUD_ENDPOINT":"","OSV_CLOUD_REGION":"","OSV_CLOUD_ACCESS_KEY":"","OSV_CLOUD_SECRET_KEY":""}) as (worker,server):
                path=Path(temporary);workload={"cloud_lifecycle":{"storage_class":SC,"regular_storage_class":REGULAR,"target_by_bucket":True},"lifecycle_debug_interval_seconds":.001,"restore_debug_interval_seconds":.002,"restore_processor_period_seconds":.003,"storage_classes":["STANDARD",SC],"kms_key_id":"configured-main"}
                context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",workload)
                for case in CASES:
                    if case.operations-worker.capabilities:
                        self.assertEqual(run_cases(context,[case])[0]["status"],"not_run");continue
                    with self.subTest(case=case.name),self.assertRaises(CaseNotRun) as caught:case.execute(Probe(context))
                    self.assertIn("OSV_CLOUD_ENDPOINT",caught.exception.requirements)
                self.assertEqual(server.requests,[])

    def test_shared_or_unknown_target_templates_and_unbound_cleanup_are_zero_http(self):
        """Reject constant/repeated/unknown templates and empty shared-prefix cleanup before HTTP; a whole target bucket may be cleaned only when its exact owned source UUID is bound into its name."""
        from osv.ceph_cloud_lifecycle import settings,own_cloud_prefix
        from osv.suites import CaseNotRun
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                path=Path(temporary)
                for template in ("shared-constant","${bucket}-${bucket}","${unknown}-${bucket}","${zonegroup}-${storage_class}"):
                    context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"cloud_lifecycle":{"storage_class":SC,"target_by_bucket_prefix":template},"lifecycle_debug_interval_seconds":.001})
                    with self.subTest(template=template),self.assertRaises(ValueError):settings(Probe(context))
                for target,prefix,delete_bucket,owner in ((TARGET,"",False,None),(TARGET,"",True,B0),(TARGET,B0+"/",True,None),(TARGET,"someone-else/",False,None)):
                    with self.subTest(target=target,prefix=prefix),self.assertRaises(CaseNotRun):
                        with own_cloud_prefix(worker,target,prefix,delete_bucket,source_bucket=owner):pass
                self.assertEqual(server.requests,[])

    def test_failed_source_still_closes_before_real_cloud_version_consumers(self):
        """Raise an independent source assertion after a genuine upload; source bucket deletion must finish before cloud cleanup, and actual version-list output must enter exact opaque key/ID deletes without deleting the shared bucket."""
        from sdk_fixtures import FixtureHandler
        from osv.ceph_cloud_lifecycle import cloud_source
        from xml.sax.saxutils import escape
        ident="opaque+/%?&=version";key=B0+"/opaque +/key"
        raw=("<ListVersionsResult><Name>"+TARGET+"</Name><IsTruncated>false</IsTruncated><Version><Key>"+escape(key)+"</Key><VersionId>"+escape(ident)+"</VersionId></Version></ListVersionsResult>").encode()
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            events=[]
            class OrderedHandler(FixtureHandler):
                """Record independent HTTP arrival ordering across both real SDK endpoints; replay explicit documents without computing cloud state."""
                def respond(self):
                    events.append((self.server.role,self.command,self.path))
                    return super().respond()
                do_GET=do_PUT=do_HEAD=do_POST=do_DELETE=respond
            cloud=[(200,{},b""),(200,XML,raw),(204,{},b""),rows_result([])]
            source=scope_begin()+[put_result(0)]+scope_end(("owned",))
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,cloud,"s3",handler=OrderedHandler) as (_,cloud_server):
                cloud_server.role="cloud"
                overrides={"OSV_CLOUD_ENDPOINT":"http://127.0.0.1:"+str(cloud_server.server_port),"OSV_CLOUD_REGION":"us-east-1","OSV_CLOUD_ACCESS_KEY":"separate-cloud-access","OSV_CLOUD_SECRET_KEY":"separate-cloud-secret"}
                with fixture_worker(language,source,"s3",overrides=overrides,handler=OrderedHandler) as (worker,server):
                    server.role="source";path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{})
                    with patch("osv.bucket_scope.uuid.uuid4",return_value=SimpleNamespace(hex="1"*32)),self.assertRaisesRegex(AssertionError,"independent source oracle"):
                        with cloud_source(Probe(context),{"storage_class":SC,"target_path":TARGET}) as (bucket,remote):
                            bucket.put("owned",b"source bytes");raise AssertionError("independent source oracle")
                    self.assertTrue(any(event[0]=="cloud" for event in events),"remote cleanup must consume actual cloud version outputs")
                    source_done=max(index for index,event in enumerate(events) if event[0]=="source")
                    cloud_begin=min(index for index,event in enumerate(events) if event[0]=="cloud")
                    self.assertLess(source_done,cloud_begin)
                    deletes=[item for item in cloud_server.requests if item[0]=="DELETE"]
                    self.assertEqual(len(deletes),1);self.assertEqual(unquote(urlsplit(deletes[0][1]).path),"/"+TARGET+"/"+key)
                    self.assertEqual(parse_qs(urlsplit(deletes[0][1]).query)["versionId"],[ident])
                    self.assertEqual(len(server.requests),len(source));self.assertEqual(len(cloud_server.requests),len(cloud))

    def test_read_through_zero_days_executes_zero_wait_and_actual_final_head(self):
        """Source days=0 must execute the original zero-duration wait and real final HEAD, accepting original GET200/400 paths; a final actual size1 must still fail the source zero-size oracle with cleanup."""
        from osv.ceph_cloud_lifecycle import restore_current
        key="test_restore_readthrough";body="restore data with readthrough"
        case=next(case for case in CASES if case.name=="ceph_cloud_lc_read_through")
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            for status,final_size in ((200,0),(400,0),(200,1)):
                base,_,config=documents("read_through");config={**config,"allow_read_through":"true","read_through_days":0}
                source=base[:7]+[get(body) if status==200 else error(400,"InvalidRequest"),head(size=len(body)),head(size=final_size)]+scope_end((key,))
                with fixture_worker(language,remote_cleanup([]),"s3") as (_,cloud_server):
                    overrides={"OSV_CLOUD_ENDPOINT":"http://127.0.0.1:"+str(cloud_server.server_port),"OSV_CLOUD_REGION":"us-east-1","OSV_CLOUD_ACCESS_KEY":"separate-cloud-access","OSV_CLOUD_SECRET_KEY":"separate-cloud-secret"}
                    with self.subTest(language=language,status=status,final_size=final_size),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,source,"s3",overrides=overrides) as (worker,server):
                        path=Path(temporary);context=Context(worker,"unused/",path,b"independent",path/"body",path/"output",{"cloud_lifecycle":config,"lifecycle_debug_interval_seconds":.001,"restore_debug_interval_seconds":.002,"restore_processor_period_seconds":.003})
                        if case.operations-worker.capabilities:
                            self.assertEqual(run_cases(context,[case])[0]["status"],"not_run");self.assertEqual(server.requests,[]);self.assertEqual(cloud_server.requests,[]);continue
                        with patch("osv.ceph_cloud_lifecycle.wait",wraps=wait) as observed:
                            if final_size:
                                with self.assertRaisesRegex(AssertionError,"1 != 0"):restore_current(Probe(context),"read_through")
                            else:restore_current(Probe(context),"read_through")
                        self.assertEqual([round(call.args[0],6) for call in observed.call_args_list],[.01,.006,0])
                        actual_heads=[row for row in server.requests if row[0]=="HEAD" and row[1].endswith("/"+key)]
                        self.assertEqual(len(actual_heads),3);self.assertEqual(len(server.requests),len(source));self.assertEqual(len(cloud_server.requests),2)

    def test_read_through_days_missing_and_noninteger_boundaries_are_zero_http(self):
        """Missing days remains an explicit prerequisite; negative, boolean, floating, textual and enormous floating values must fail configuration before HTTP, matching CLI's nonnegative-integer contract."""
        from osv.ceph_cloud_lifecycle import _restore_current
        from osv.suites import CaseNotRun
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                path=Path(temporary);probe=Probe(Context(worker,"unused/",path,b"independent",path/"body",path/"output",{}))
                config={"storage_class":SC,"clock":.001,"period":.003,"restore_clock":.002,"allow_read_through":"true"}
                with self.assertRaises(CaseNotRun) as missing:_restore_current(probe,config,"read_through")
                self.assertEqual(missing.exception.requirements,["workload.cloud_lifecycle.read_through_days"])
                for value in (-1,True,False,0.0,1.0,"0",float("nan"),float("inf")):
                    with self.subTest(days=value),self.assertRaises(ValueError):_restore_current(probe,{**config,"read_through_days":value},"read_through")
                self.assertEqual(server.requests,[])
