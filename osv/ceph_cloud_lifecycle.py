"""Translate Ceph cloud-transition and restore contracts without substituting client delays for configured service features."""
from contextlib import contextmanager,ExitStack
from dataclasses import replace
import math
import os
import re
from collections import defaultdict
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_lifecycle_expiration import interval,wait,lifecycle,objects
from .ceph_lifecycle_transition import by_class
from .ceph_versioning import configure,create_versions
from .ceph_state_boundaries import header
from .ceph_bucket_config import api,rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,Probe,CEPH
from .worker import Worker


def positive(p,key):
    value=p.ctx.workload.get(key)
    if value is None:raise CaseNotRun("the source requires this already-configured service timer","workload."+key)
    try:valid=type(value) in (int,float) and math.isfinite(value) and value>0
    except OverflowError:valid=False
    if not valid:raise ValueError(key+" must be finite and positive")
    return value

def settings(p,regular=False,restore=False,expiry=False,remote=True,by_bucket=False):
    config=p.ctx.workload.get("cloud_lifecycle",{})
    if not isinstance(config,dict):raise ValueError("cloud_lifecycle must be an object")
    if not config.get("storage_class"):raise CaseNotRun("Ceph cloud source requires a server-configured cloud storage class","workload.cloud_lifecycle.storage_class")
    if regular and not config.get("regular_storage_class"):raise CaseNotRun("source requires its configured intermediate storage class","workload.cloud_lifecycle.regular_storage_class")
    if by_bucket and not config.get("target_by_bucket"):raise CaseNotRun("source requires server-side target_by_bucket enabled","workload.cloud_lifecycle.target_by_bucket")
    if config.get("target_by_bucket_prefix") is not None:
        template=config["target_by_bucket_prefix"]
        if not isinstance(template,str) or template.count("${bucket}")!=1:raise ValueError("cloud target template must contain exactly one ${bucket} ownership token")
        if "${" in template.replace("${bucket}","").replace("${zonegroup}","").replace("${storage_class}",""):raise ValueError("cloud target template contains an unknown token")
    result=dict(config);result["clock"]=interval(p)
    if restore:result["period"]=positive(p,"restore_processor_period_seconds")
    if expiry:result["restore_clock"]=positive(p,"restore_debug_interval_seconds")
    if remote:
        environment={**os.environ,**p.ctx.worker.target.get("env",{})}
        required=("ENDPOINT","REGION","ACCESS_KEY","SECRET_KEY")
        missing=["OSV_CLOUD_"+name for name in required if not environment.get("OSV_CLOUD_"+name)]
        if missing:raise CaseNotRun("the source reads the separate configured cloud endpoint with an independent SDK client",*missing)
    return result

@contextmanager
def cloud_worker(p):
    target=dict(p.ctx.worker.target);environment={**os.environ,**target.get("env",{})}
    target["env"]={**target.get("env",{}),**{"OSV_"+name:environment.get("OSV_CLOUD_"+name,"") for name in ("ENDPOINT","REGION","ACCESS_KEY","SECRET_KEY","SESSION_TOKEN")}}
    worker=Worker(target,p.ctx.worker.timeout)
    try:yield worker
    finally:worker.close()

def remote_call(worker,bucket,op,**options):return require_ok(worker.call({"op":op,"bucket":bucket,**options}))

def storage_class(response):
    try:return header(response,"x-amz-storage-class")
    except KeyError:return "STANDARD"

def verify_object(p,worker,bucket,key,body=None,sc=None):
    p.ctx.output.unlink(missing_ok=True)
    response=remote_call(worker,bucket,"get_http",key=key,output_path=str(p.ctx.output))
    assert_equal(storage_class(response),"STANDARD" if sc is None else sc)
    if body is not None:assert_equal(p.ctx.output.read_bytes(),body.encode() if isinstance(body,str) else body)

def verify_transition(bucket,key,sc,version=None):
    response=bucket.call("head_version" if version is not None else "head_http",key=key,**({"version_id":version} if version is not None else {}))
    assert_equal(storage_class(response),sc)

def head_size(bucket,key,size,version=None):
    response=bucket.call("head_version" if version is not None else "head_http",key=key,**({"version_id":version} if version is not None else {}))
    assert_equal(response["size"],size)

def restore(bucket,key,days=None,version=None):
    api(bucket,"RestoreObject",Key=key,RestoreRequest={} if days is None else {"Days":days},**({"VersionId":version} if version is not None else {}))

def delete_lifecycle(bucket):api(bucket,"DeleteBucketLifecycle")

def shared_target(config):return config.get("target_path") or "rgwx-default-"+config["storage_class"].lower()+"-cloud-bucket"

def bucket_target(config,name):
    template=config.get("target_by_bucket_prefix")
    if template:return template.replace("${zonegroup}","default").replace("${storage_class}",config["storage_class"].lower()).replace("${bucket}",name)
    return "rgwx-default-"+config["storage_class"].lower()+"-"+name

@contextmanager
def own_cloud_prefix(worker,target,prefix,delete_bucket=False,source_bucket=None):
    # The source UUID bucket prefix owns these copied keys, never other keys in a shared cloud target.
    # For target_by_bucket, establish target absence before the server auto-creates it.
    if delete_bucket:
        if not isinstance(source_bucket,str) or not re.fullmatch(r"osv-[0-9a-f]{32}",source_bucket) or source_bucket not in target:
            raise CaseNotRun("derived cloud target lacks a unique owned source UUID","target derived from the exact source bucket UUID")
        if prefix:raise ValueError("derived cloud bucket cleanup must use its entire proven owned namespace")
        existing=worker.call({"op":"bucket_head","bucket":target})
        if existing["ok"] or (existing.get("status_code"),existing.get("error_kind"))!=(404,"not_found"):
            raise CaseNotRun("cannot establish ownership of the derived cloud target before transition","fresh cloud target "+target)
    elif not isinstance(prefix,str) or not re.fullmatch(r"osv-[0-9a-f]{32}/",prefix):
        raise CaseNotRun("shared cloud cleanup requires a unique owned source UUID prefix","source bucket UUID prefix")
    try:yield
    finally:
        existing=worker.call({"op":"bucket_head","bucket":target})
        if not existing["ok"]:
            if (existing.get("status_code"),existing.get("error_kind"))!=(404,"not_found"):require_ok(existing)
        else:
            previous=None
            for attempt in range(10000):
                result=remote_call(worker,target,"versions_list",prefix=prefix,max_keys=1000)
                rows=result["versions"]+result["delete_markers"]
                if any(not row["key"].startswith(prefix) for row in rows):raise RuntimeError("cloud cleanup returned an unowned key")
                if not rows:
                    assert_equal(result.get("truncated"),False);break
                signature=tuple((row["key"],row["version_id"]) for row in rows)
                if signature==previous:raise RuntimeError("cloud version cleanup did not advance")
                previous=signature
                for row in rows:remote_call(worker,target,"delete_version",key=row["key"],version_id=row["version_id"])
            else:raise RuntimeError("cloud cleanup exceeded page bound")
            if delete_bucket:
                remote_call(worker,target,"bucket_delete")
                gone=worker.call({"op":"bucket_head","bucket":target})
                assert_equal((gone.get("ok"),gone.get("status_code"),gone.get("error_kind")),(False,404,"not_found"))

@contextmanager
def cloud_source(p,config,versioned=False):
    """Own the actual remote namespace before lifecycle writes, close the source scope first, then purge only the source UUID prefix or its proven fresh derived target through a separate real SDK."""
    with cloud_worker(p) as worker,ExitStack() as copied:
        with (VersionScope(p) if versioned else BucketScope(p)) as bucket:
            if config.get("target_by_bucket"):
                copied.enter_context(own_cloud_prefix(worker,bucket_target(config,bucket.name),"",True,source_bucket=bucket.name))
            else:copied.enter_context(own_cloud_prefix(worker,shared_target(config),bucket.name+"/"))
            yield bucket,worker


def cloud_current(p,mode):
    config=settings(p,regular=mode=="multiple");clock=config["clock"];sc=config["storage_class"]
    keys=("keep/multi","expire1/multi") if mode=="large" else ("expire1/foo","expire1/bar","keep2/foo","keep2/bar")
    # Close/clean the source first, so late source work cannot recreate target objects after target cleanup.
    with cloud_source(p,config) as (bucket,worker):
        data="A"*(9*1024*1024) if mode=="large" else None
        for key in keys:
            bucket.put(key,(data or key).encode())
            if mode=="large":verify_object(p,p.ctx.worker,bucket.name,key,data)
        rules=[{"ID":"rule1","Transitions":[{"Days":1,"StorageClass":config["regular_storage_class"] if mode=="multiple" else sc}],"Prefix":"expire1/","Status":"Enabled"}]
        if mode=="multiple":rules += [{"ID":"rule2","Transitions":[{"Days":5,"StorageClass":sc}],"Prefix":"expire1/","Status":"Enabled"},{"ID":"rule3","Expiration":{"Days":9},"Prefix":"expire1/","Status":"Enabled"}]
        lifecycle(bucket,rules)
        if mode!="large":assert_equal(len(objects(bucket)),4)
        wait((4 if mode=="multiple" else 12 if mode=="large" else 10)*clock);actual=by_class(bucket)
        assert_equal(len(actual["STANDARD"]),1 if mode=="large" else 2)
        if mode=="multiple":
            assert_equal(len(actual[config["regular_storage_class"]]),2);assert_equal(len(actual[sc]),0)
            wait(7*clock);actual=by_class(bucket);assert_equal(len(actual["STANDARD"]),2);assert_equal(len(actual[config["regular_storage_class"]]),0)
        retained=config.get("retain_head_object") is not None and config.get("retain_head_object")=="true"
        assert_equal(len(actual[sc]),(1 if mode=="large" else 2) if retained else 0)
        if mode=="multiple":
            wait(12*clock);actual=by_class(bucket)
            assert_equal(len(actual["STANDARD"]),2);assert_equal(len(actual[config["regular_storage_class"]]),0);assert_equal(len(actual[sc]),0);return
        if mode=="ordinary":wait(2*clock)
        wait(12*clock)
        for key in (keys[1:2] if mode=="large" else keys[:2]):verify_object(p,worker,shared_target(config),bucket.name+"/"+key,data or key,config.get("target_storage_class"))
        if mode=="ordinary" and retained:
            response=bucket.call("head_http",key=keys[0]);assert_equal(response["size"],0);assert_equal(storage_class(response),sc)
            if config.get("allow_read_through") is None or config.get("allow_read_through")=="false":
                rejected(bucket.raw("get_http",key=keys[0],output_path=str(p.ctx.output)),403,"InvalidObjectState")
                # Boto's original managed copy first HEADs the source; the retained
                # stub was just asserted to have zero bytes, so its copy is single-part.
                copy_head=bucket.raw("head_http",key=keys[0])
                copied=bucket.raw("copy",key="copy_obj",source_key=keys[0]) if copy_head["ok"] else copy_head
                rejected(copied,403,"InvalidObjectState")
            bucket.call("delete_version",key=keys[0])
            rejected(bucket.raw("get_http",key=keys[0],output_path=str(p.ctx.output)),404,"NoSuchKey")

def cloud_noncurrent(p):
    config=settings(p,regular=True,remote=True);clock=config["clock"];sc=config["storage_class"];regular=config["regular_storage_class"]
    with cloud_source(p,config,True) as (bucket,worker):
        bucket.put("test1/a",b"fooz");configure(bucket,"Enabled")
        lifecycle(bucket,[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","NoncurrentVersionTransitions":[{"NoncurrentDays":1,"StorageClass":regular},{"NoncurrentDays":5,"StorageClass":sc}]}])
        create_versions(bucket,p,"test1/a",2);create_versions(bucket,p,"test1/b",3)
        assert_equal(len(by_class(bucket)["STANDARD"]),6)
        # Retain this otherwise unused original listing before the waiting stages.
        bucket.call("versions_list")
        wait(4*clock);actual=by_class(bucket)
        assert_equal(len(actual["STANDARD"]),2);assert_equal(len(actual[regular]),4);assert_equal(len(actual[sc]),0)
        wait(15*clock);actual=by_class(bucket)
        assert_equal(len(actual["STANDARD"]),2);assert_equal(len(actual[regular]),0)
        assert_equal(len(actual[sc]),0 if config.get("retain_head_object") is None or config.get("retain_head_object")=="false" else 4)
        wait(clock);response=bucket.call("versions_list");rows=defaultdict(list)
        if "Versions" not in response["fields"]:raise KeyError("Versions")
        # Preserve source helper's bug: it groups by actual response Name, then callers look up object keys.
        for version in response["versions"]:rows[response["bucket_name"]].append(version)
        for source_key in ("test1/a","test1/b"):
            for version in rows[source_key]:verify_object(p,worker,shared_target(config),bucket.name+"/test1/a-"+version["version_id"],None,config.get("target_storage_class"))
        return {"source_limitation":"source list_bucket_versions groups by response Name but looks up source object keys; cloud read-back loop is empty for ordinary bucket names"}

def cloud_by_bucket(p,multiple=False):
    config=settings(p,restore=True,remote=True,by_bucket=True);clock=config["clock"];sc=config["storage_class"]
    with cloud_worker(p) as worker,ExitStack() as targets,ExitStack() as sources:
        buckets=[sources.enter_context(BucketScope(p)) for _ in range(2 if multiple else 1)]
        keys=("only-in-a.txt","only-in-b.txt") if multiple else ("file1.txt","subdir/file2.txt")
        target_names=[bucket_target(config,bucket.name) for bucket in buckets]
        for target,bucket in zip(target_names,buckets):targets.enter_context(own_cloud_prefix(worker,target,"",True,source_bucket=bucket.name))
        rules=[{"ID":"rule1","Transitions":[{"Days":1,"StorageClass":sc}],"Prefix":"","Status":"Enabled"}]
        if multiple:
            for bucket,key,body in zip(buckets,keys,("content-a","content-b")):bucket.put(key,body.encode())
            for bucket in buckets:lifecycle(bucket,rules)
            wait(20*clock)
        else:
            for key in keys:buckets[0].put(key,key.encode())
            lifecycle(buckets[0],rules);assert_equal(len(objects(buckets[0])),2)
            wait(15*clock);actual=by_class(buckets[0])
            if config.get("retain_head_object") and config["retain_head_object"].lower()=="true":assert_equal(len(actual.get(sc,[])),2)
            wait(5*clock)
        for index,key in enumerate(keys):
            target=target_names[index] if multiple else target_names[0]
            verify_object(p,worker,target,key,("content-a","content-b")[index] if multiple else key,config.get("target_storage_class"))
            missing=keys[1-index] if multiple else buckets[0].name+"/"+key
            response=worker.call({"op":"head_http","bucket":target,"key":missing})
            assert_equal(response["ok"],False)
            if response.get("error_code") not in ("404","NoSuchKey"):raise AssertionError("unexpected cloud HEAD error: "+str(response))
        first=buckets[0];key=keys[0];verify_transition(first,key,sc);delete_lifecycle(first);restore(first,key,2)
        wait(3*config["period"]);verify_transition(first,key,sc);head_size(first,key,len("content-a" if multiple else key))

def restore_current(p,mode):
    config=settings(p,restore=True,expiry=mode in ("temporary","read_through"));sc=config["storage_class"];clock=config["clock"]
    if mode=="read_through":
        if "client_timeouts" not in p.ctx.worker.capabilities:raise CaseNotRun("source requires a separate client with 100s connect/read timeouts","worker.client_timeouts")
        target=dict(p.ctx.worker.target);target["env"]={**target.get("env",{}),"OSV_REQUEST_TIMEOUT_MS":"100000","OSV_CONNECT_TIMEOUT_MS":"100000"}
        worker=Worker(target,max(p.ctx.worker.timeout,105))
        try:
            return _restore_current(Probe(replace(p.ctx,worker=worker)),config,mode)
        finally:worker.close()
    return _restore_current(p,config,mode)

def _restore_current(p,config,mode):
    sc=config["storage_class"];clock=config["clock"]
    key={"temporary":"test_restore_temp","permanent":"test_restore_perm","read_through":"test_restore_readthrough"}[mode]
    data={"temporary":"temporary restore data","permanent":"permanent restore data","read_through":"restore data with readthrough"}[mode]
    if mode=="read_through" and config.get("allow_read_through") is not None and config.get("allow_read_through")=="true":
        days=config.get("read_through_days")
        if type(days) not in (int,float) or days<=0:raise CaseNotRun("source read-through expiration needs actual configured read_through_days","workload.cloud_lifecycle.read_through_days")
    with cloud_source(p,config) as (bucket,cloud):
        bucket.put(key,data.encode());verify_object(p,p.ctx.worker,bucket.name,key,data)
        lifecycle(bucket,[{"ID":"rule1","Transitions":[{"Days":1,"StorageClass":sc}],"Prefix":"","Status":"Enabled"}])
        wait(10*clock);verify_transition(bucket,key,sc);delete_lifecycle(bucket)
        if mode=="read_through":
            response=bucket.raw("get_http",key=key,output_path=str(p.ctx.output))
            if config.get("allow_read_through") is not None and config.get("allow_read_through")=="true":
                if not response["ok"]:assert_equal(response["status_code"],400)
                wait(2*config["period"]);head_size(bucket,key,len(data))
                wait(2*config["read_through_days"]*(config["restore_clock"]+clock));head_size(bucket,key,0)
            else:rejected(response,403,"InvalidObjectState")
            return
        restore(bucket,key,20 if mode=="temporary" else None);wait(3*config["period"])
        if mode=="temporary":verify_transition(bucket,key,sc)
        head_size(bucket,key,len(data))
        if mode=="permanent":verify_transition(bucket,key,"STANDARD");return
        restore(bucket,key,2);wait(2*config["period"]);head_size(bucket,key,len(data))
        wait(2*(config["restore_clock"]+clock));head_size(bucket,key,0)

def restore_noncurrent(p):
    config=settings(p,restore=True,expiry=True);sc=config["storage_class"];clock=config["clock"]
    with cloud_source(p,config,True) as (bucket,cloud):
        key="test1/a";bucket.put(key,b"fooz");configure(bucket,"Enabled")
        lifecycle(bucket,[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","NoncurrentVersionTransitions":[{"NoncurrentDays":2,"StorageClass":sc}]}])
        _,contents=create_versions(bucket,p,key,2);contents.append(b"fooz")
        assert_equal(len(by_class(bucket)["STANDARD"]),3)
        response=bucket.call("versions_list")
        if "Versions" not in response["fields"]:raise KeyError("Versions")
        identifiers=[row["version_id"] for row in response["versions"]]
        wait(7*clock);actual=by_class(bucket)
        assert_equal(len(actual["STANDARD"]),1);assert_equal(len(actual[sc]),2)
        # Source range(1,2) deliberately restores only the second listed version.
        for number in range(1,2):
            verify_transition(bucket,key,sc,identifiers[number]);restore(bucket,key,2,identifiers[number]);wait(2*config["period"])
            head_size(bucket,key,len(contents[number]),identifiers[number])
            response=bucket.call("versions_list")
            if "Versions" not in response["fields"]:raise KeyError("Versions")
            assert_equal(response["versions"][1]["is_latest"],False)
        wait(2*(config["restore_clock"]+clock))
        for number in range(1,2):head_size(bucket,key,0,identifiers[number])

def list_api(bucket,name,optional=True):
    return api(bucket,name,**({"OptionalObjectAttributes":["RestoreStatus"]} if optional else {}))["data"]

def list_restore_status(p,versioned=False):
    config=settings(p,restore=True);sc=config["storage_class"];clock=config["clock"]
    with cloud_source(p,config,versioned) as (bucket,cloud):
        if versioned:
            key="test1/a";bucket.put(key,b"restore status versioned data");configure(bucket,"Enabled");create_versions(bucket,p,key,1)
            for row in list_api(bucket,"ListObjectVersions")["Versions"]:assert_equal("RestoreStatus" in row,False)
            lifecycle(bucket,[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","NoncurrentVersionTransitions":[{"NoncurrentDays":2,"StorageClass":sc}]}])
            wait(7*clock);response=list_api(bucket,"ListObjectVersions",False);noncurrent=None
            for row in response["Versions"]:
                if not row["IsLatest"]:noncurrent=row["VersionId"];break
            assert_equal(noncurrent is not None,True)
            verify_transition(bucket,key,sc,noncurrent);delete_lifecycle(bucket);restore(bucket,key,20,noncurrent);wait(3*config["period"])
            for row in list_api(bucket,"ListObjectVersions")["Versions"]:
                if row["VersionId"]==noncurrent:
                    assert_equal("RestoreStatus" in row,True);assert_equal(row["RestoreStatus"]["IsRestoreInProgress"],False);assert_equal("RestoreExpiryDate" in row["RestoreStatus"],True)
                else:assert_equal("RestoreStatus" in row,False)
            for row in list_api(bucket,"ListObjectVersions",False)["Versions"]:assert_equal("RestoreStatus" in row,False)
            return
        temporary="test_restore_status_temp";permanent="test_restore_status_perm";data=b"restore status listing data"
        bucket.put(temporary,data);bucket.put(permanent,data)
        for name in ("ListObjectsV2","ListObjects"):
            rows=list_api(bucket,name)["Contents"];assert_equal(len(rows),2)
            for row in rows:assert_equal("RestoreStatus" in row,False)
        lifecycle(bucket,[{"ID":"rule1","Transitions":[{"Days":1,"StorageClass":sc}],"Prefix":"","Status":"Enabled"}])
        wait(10*clock);verify_transition(bucket,temporary,sc);verify_transition(bucket,permanent,sc);delete_lifecycle(bucket)
        restore(bucket,temporary,20);restore(bucket,permanent);wait(3*config["period"])
        for name in ("ListObjectsV2","ListObjects"):
            rows={row["Key"]:row for row in list_api(bucket,name)["Contents"]};assert_equal(len(rows),2)
            for key,expiring in ((temporary,True),(permanent,False)):
                assert_equal("RestoreStatus" in rows[key],True);assert_equal(rows[key]["RestoreStatus"]["IsRestoreInProgress"],False)
                assert_equal("RestoreExpiryDate" in rows[key]["RestoreStatus"],expiring)
        rows=list_api(bucket,"ListObjectsV2",False)["Contents"];assert_equal(len(rows),2)
        for row in rows:assert_equal("RestoreStatus" in row,False)

SSE_CUSTOMER={"x-amz-server-side-encryption-customer-algorithm":"AES256","x-amz-server-side-encryption-customer-key":"pO3upElrwuEXSoFwCfnZPdSsmt/xWeFa0N9KgDijwVs=","x-amz-server-side-encryption-customer-key-md5":"DWygnHRtgiJ77HCm+1rvHw=="}

def encrypted_transition(p):
    classes=p.ctx.workload.get("storage_classes",["STANDARD"])
    if len(classes)<2:raise CaseNotRun("source expands all distinct configured storage-class pairs","workload.storage_classes: at least 2")
    clock=interval(p)
    cloud_config=p.ctx.workload.get("cloud_lifecycle",{})
    cloud_config=settings(p) if cloud_config.get("storage_class") in classes else None
    if not p.ctx.workload.get("kms_key_id"):raise CaseNotRun("source SSE-KMS parameter instances require a real configured main key","workload.kms_key_id")
    for mode in ("unencrypted","sse-s3","sse-c","sse-kms"):
        for source in classes:
            for destination in classes:
                if source==destination:continue
                with ExitStack() as owned:
                    bucket=owned.enter_context(cloud_source(p,cloud_config))[0] if cloud_config else owned.enter_context(BucketScope(p))
                    for key in ("expire1/foo","expire1/bar"):
                        p.ctx.body.write_bytes(key.encode());options={"storage_class":source}
                        if mode=="sse-c":options["sse_headers"]=SSE_CUSTOMER;op="put_sse_c"
                        elif mode in ("sse-s3","sse-kms"):
                            options["encryption_headers"]={"x-amz-server-side-encryption":"AES256" if mode=="sse-s3" else "aws:kms"}
                            if mode=="sse-kms":options["encryption_headers"]["x-amz-server-side-encryption-aws-kms-key-id"]=p.ctx.workload["kms_key_id"]
                            op="put_encryption"
                        else:op="put"
                        bucket.call(op,key=key,body_path=str(p.ctx.body),**options)
                    lifecycle(bucket,[{"ID":"rule1","Prefix":"","Transitions":[{"Days":1,"StorageClass":destination}],"Status":"Enabled"}])
                    keys=[row["key"] for row in objects(bucket)];assert_equal(len(keys),2)
                    wait(4*clock);actual=by_class(bucket)
                    assert_equal(len(actual[source]),0);assert_equal(len(actual[destination]),2)
                    for key in keys:
                        p.ctx.output.unlink(missing_ok=True)
                        bucket.call("get_sse_c" if mode=="sse-c" else "get_http",key=key,output_path=str(p.ctx.output),**({"sse_headers":SSE_CUSTOMER} if mode=="sse-c" else {}))
                        assert_equal(p.ctx.output.read_bytes(),key.encode())

BASE=SCOPE_OPERATIONS+" put get_http head_http head_version delete_version versions_list versions_storage_class api.PutBucketLifecycleConfiguration lifecycle_legacy_prefix"
VBASE=VERSION_SCOPE_OPERATIONS+" put get_http head_http head_version versions_storage_class api.PutBucketLifecycleConfiguration lifecycle_legacy_prefix versioning_get versioning_put put_version"
RESTORE=" api.RestoreObject api.DeleteBucketLifecycle"
VECTORS=(
 ("ordinary",lambda p:cloud_current(p,"ordinary"),"test_lifecycle_cloud_transition",BASE+" list_v1_page copy"),
 ("multiple",lambda p:cloud_current(p,"multiple"),"test_lifecycle_cloud_multiple_transition",BASE+" list_v1_page"),
 ("noncurrent",cloud_noncurrent,"test_lifecycle_noncur_cloud_transition",VBASE),
 ("large",lambda p:cloud_current(p,"large"),"test_lifecycle_cloud_transition_large_obj",BASE),
 ("target_bucket",cloud_by_bucket,"test_lifecycle_cloud_transition_target_by_bucket",BASE+RESTORE+" list_v1_page"),
 ("target_buckets",lambda p:cloud_by_bucket(p,True),"test_lifecycle_cloud_transition_target_by_bucket_multiple_buckets",BASE+RESTORE),
 ("temporary",lambda p:restore_current(p,"temporary"),"test_restore_object_temporary",BASE+RESTORE),
 ("permanent",lambda p:restore_current(p,"permanent"),"test_restore_object_permanent",BASE+RESTORE),
 ("read_through",lambda p:restore_current(p,"read_through"),"test_read_through",BASE+RESTORE+" client_timeouts"),
 ("restore_noncurrent",restore_noncurrent,"test_restore_noncur_obj",VBASE+RESTORE),
 ("list_status",list_restore_status,"test_list_objects_restore_status",BASE+RESTORE+" api.ListObjects api.ListObjectsV2"),
 ("list_version_status",lambda p:list_restore_status(p,True),"test_list_object_versions_restore_status",VBASE+RESTORE+" api.ListObjectVersions"),
 ("encrypted",encrypted_transition,"test_lifecycle_transition_encrypted",BASE+" list_v1_page put_storage_class put_sse_c get_sse_c put_encryption"),
)
CASES=tuple(case("ceph_cloud_lc_"+name,"Ceph 云目标生命周期与恢复 "+name,"保留原跨端SDK读回、目标布局/隔离、恢复状态缺字段、原版本选择与服务阶段全部断言；需真实配置云类别/服务计时/云端独立身份，不缩减源等待",ops,execute,reference,CEPH) for name,execute,reference,ops in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_cloud_lifecycle",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
