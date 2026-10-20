"""Preserve object-level retention/hold source contracts with proven governance cleanup and bounded compliance expiry."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import time
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_bucket_config import api,raw,rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH

UTC=timezone.utc
DATE2030=int(datetime(2030,1,1,tzinfo=UTC).timestamp()*1000)
DATE2030_3=int(datetime(2030,1,3,tzinfo=UTC).timestamp()*1000)
DATE2140=int(datetime(2140,1,1,tzinfo=UTC).timestamp()*1000)
BYPASS={"x-amz-bypass-governance-retention":"true"}

def retention(date=DATE2030,mode="GOVERNANCE"):return {"Mode":mode,"RetainUntilDate":date}

def wait_until(date):
    delay=date/1000-time.time()+0.15
    if delay>0:time.sleep(delay)

class LockScope(VersionScope):
    """Own an isolated lock-enabled/versioned bucket, clear holds and bypass governance during purge; wait only the source's original short compliance deadline before deletion."""
    def __init__(self,probe):
        super().__init__(probe);self.create_operation="bucket_create_locked";self.compliance_expiry=0;self.allow_unlocked=False
    def cleanup_objects(self):
        if self.compliance_expiry:wait_until(self.compliance_expiry)
        failures=[];previous=None
        for _ in range(10000):
            response=self.raw("versions_list",max_keys=1000)
            if not response.get("ok"):return failures+[response]
            versions=response["versions"];markers=response["delete_markers"];rows=versions+markers
            if not rows:
                if response.get("truncated") is not False:raise RuntimeError("lock cleanup empty page did not confirm completion")
                return failures
            current=tuple(sorted((r["key"],r["version_id"]) for r in rows))
            if current==previous:raise RuntimeError("lock version cleanup did not advance: "+str(failures))
            previous=current
            for row in versions:
                result=raw(self,"PutObjectLegalHold",Key=row["key"],VersionId=row["version_id"],LegalHold={"Status":"OFF"})
                if not result.get("ok") and result.get("status_code")!=404 and not (self.allow_unlocked and (result.get("status_code"),result.get("error_code"))==(400,"InvalidRequest")):failures.append(result)
            for row in rows:
                result=self.raw("delete_version",key=row["key"],version_id=row["version_id"],wire_headers=BYPASS)
                if not result.get("ok"):failures.append(result)
        raise RuntimeError("lock cleanup exceeded page safety bound")
    def probe_governance_cleanup(self):
        """Before any long retention, prove actual same-bucket file1/version bypass deletion using a two-second retention probe; failed proof expires and cleans before not_run."""
        self.probe.ctx.body.write_bytes(b"abc")
        result=self.call("put_version",key="file1",body_path=str(self.probe.ctx.body),wire_headers={"Content-MD5":"kAFQmDzST7DWlj99KOF/cg=="});ident=result.get("version_id")
        if not isinstance(ident,str) or not ident:raise CaseNotRun("cleanup probe lacks a genuine SDK version ID","actual version ID from PutObject")
        expiry=int((datetime.now(UTC)+timedelta(seconds=2)).timestamp()*1000)
        configured=raw(self,"PutObjectRetention",Key="file1",VersionId=ident,Retention=retention(expiry))
        proved=self.raw("delete_version",key="file1",version_id=ident,wire_headers=BYPASS) if configured.get("ok") else {"ok":False}
        if not configured.get("ok") or not proved.get("ok"):
            wait_until(expiry)
            # After the real short expiry, omit bypass so denied bypass permission
            # cannot itself prevent cleanup of the now-unretained probe version.
            clean=self.raw("delete_version",key="file1",version_id=ident)
            if not clean.get("ok"):raise RuntimeError("expired governance probe cleanup failed: "+str(clean))
            raise CaseNotRun("actual same-key governance bypass cleanup proof failed","successful real PutObjectRetention and bypass DeleteObjectVersion probe")

@contextmanager
def locked(p,probe=False):
    if p.ctx.workload.get("object_lock_cleanup_authorized") is not True:raise CaseNotRun("object lock writes require explicitly authorized isolated cleanup","workload.object_lock_cleanup_authorized=true")
    with LockScope(p) as bucket:
        if probe:bucket.probe_governance_cleanup()
        yield bucket

def put(bucket,p,key="file1",headers=None):
    p.ctx.body.write_bytes(b"abc")
    return bucket.call("put_version",key=key,body_path=str(p.ctx.body),wire_headers={"Content-MD5":"kAFQmDzST7DWlj99KOF/cg==",**(headers or {})})

def headers(date=None,mode=None,hold=None):
    result={"Content-MD5":"kAFQmDzST7DWlj99KOF/cg=="}
    if mode is not None:result["x-amz-object-lock-mode"]=mode
    if date is not None:result["x-amz-object-lock-retain-until-date"]=datetime.fromtimestamp(date/1000,UTC).isoformat(timespec="milliseconds").replace("+00:00","Z")
    if hold is not None:result["x-amz-object-lock-legal-hold"]=hold
    return result

def delete(bucket,version=None,bypass=False):
    options={"key":"file1"}
    if version is not None:options["version_id"]=version
    if bypass:options["wire_headers"]=BYPASS
    return bucket.raw("delete_version",**options)

def hold(bucket,status,version=None):
    options={"Key":"file1","LegalHold":{"Status":status}}
    if version is not None:options["VersionId"]=version
    return api(bucket,"PutObjectLegalHold",**options)

def invalid_bucket(p,legal,write):
    if write and p.ctx.workload.get("object_lock_cleanup_authorized") is not True:raise CaseNotRun("negative lock writes still require authorized cleanup if unexpectedly accepted","workload.object_lock_cleanup_authorized=true")
    if write:
        # Prove the real account's governance cleanup before a negative endpoint
        # might unexpectedly accept long retention on a nominally unlocked bucket.
        with locked(p,probe=True):pass
        bucket=LockScope(p);bucket.create_operation="bucket_create_raw";bucket.allow_unlocked=True
    else:bucket=BucketScope(p)
    with bucket:
        bucket.put("file1",b"abc")
        options={"Key":"file1"}
        if write:options.update({"LegalHold":{"Status":"ON"}} if legal else {"Retention":retention()})
        rejected(raw(bucket,("Put" if write else "Get")+"Object"+("LegalHold" if legal else "Retention"),**options),400,"InvalidRequest")

def retained(p,mode):
    with locked(p,probe=True) as bucket:
        if mode=="override":api(bucket,"PutObjectLockConfiguration",ObjectLockConfiguration={"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Days":1}}})
        if mode=="version":put(bucket,p)
        response=put(bucket,p);ident=response["version_id"]
        if mode=="invalid_mode":
            for algorithm in ("governance","abc"):rejected(raw(bucket,"PutObjectRetention",Key="file1",Retention=retention(mode=algorithm)),400,"MalformedXML")
            return
        date=DATE2140 if mode=="put" else int((datetime.today()+timedelta(days=365)).timestamp()*1000) if mode=="iso" else DATE2030_3 if mode.startswith("shorten") else DATE2030
        configured=retention(date);parameters={"Key":"file1",**({"VersionId":ident} if mode=="version" else {})}
        result=api(bucket,"PutObjectRetention",**parameters,Retention=configured)
        if mode=="put":assert_equal(result["status_code"],200)
        if mode in ("increase","shorten","shorten_bypass"):
            configured=retention(DATE2030_3 if mode=="increase" else DATE2030)
            result=raw(bucket,"PutObjectRetention",Key="file1",Retention=configured,**({"BypassGovernanceRetention":True} if mode=="shorten_bypass" else {}))
            if mode=="shorten":rejected(result,403,"AccessDenied")
            else:require_ok(result)
        if mode=="iso":
            observed=bucket.call("head_version",key="file1",version_id=ident)["http_headers"]["x-amz-object-lock-retain-until-date"]
            datetime.fromisoformat(observed.replace("Z","+00:00"))
        elif mode!="shorten":assert_equal(api(bucket,"GetObjectRetention",**parameters)["data"]["Retention"],configured)
        require_ok(delete(bucket,ident,True))

def legal(p,mode):
    with locked(p) as bucket:
        response=put(bucket,p);ident=response["version_id"]
        if mode=="invalid_status":return rejected(raw(bucket,"PutObjectLegalHold",Key="file1",LegalHold={"Status":"abc"}),400,"MalformedXML")
        result=hold(bucket,"OFF" if mode=="delete_off" else "ON")
        if mode=="put":
            assert_equal(result["status_code"],200);assert_equal(hold(bucket,"OFF")["status_code"],200)
        elif mode=="get":
            assert_equal(api(bucket,"GetObjectLegalHold",Key="file1")["data"]["LegalHold"],{"Status":"ON"})
            hold(bucket,"OFF");assert_equal(api(bucket,"GetObjectLegalHold",Key="file1")["data"]["LegalHold"],{"Status":"OFF"})
        elif mode=="delete_on":
            rejected(delete(bucket,ident),403,"AccessDenied");hold(bucket,"OFF")
        elif mode=="delete_off":assert_equal(require_ok(delete(bucket,ident))["status_code"],204)

def retention_delete(p,mode):
    with locked(p,probe=True) as bucket:
        if mode=="multipart":response=multipart(bucket,p,governance=True)
        else:
            response=put(bucket,p);api(bucket,"PutObjectRetention",Key="file1",Retention=retention())
        ident=response["version_id"]
        marker=require_ok(delete(bucket))["version_id"] if mode=="marker" else None
        rejected(delete(bucket,ident),403,"AccessDenied")
        if marker is not None:
            require_ok(delete(bucket,marker));rejected(delete(bucket,ident),403,"AccessDenied")
        assert_equal(require_ok(delete(bucket,ident,True))["status_code"],204)

def multipart(bucket,p,governance=False):
    locks=headers(DATE2030,"GOVERNANCE") if governance else headers(hold="ON")
    locks.pop("Content-MD5")
    ident=bucket.call("mp_create",key="file1",wire_headers=locks)["upload_id"]
    try:
        p.ctx.body.write_bytes(b"abc");etag=bucket.call("mp_part",key="file1",upload_id=ident,part_number=1,body_path=str(p.ctx.body))["etag"]
        return bucket.call("mp_complete_observed",key="file1",upload_id=ident,parts=[{"part_number":1,"etag":etag.strip(chr(34))}])
    finally:
        result=bucket.raw("mp_abort_raw",key="file1",upload_id=ident)
        if not result.get("ok") and (result.get("status_code"),result.get("error_code"))!=(404,"NoSuchUpload"):raise RuntimeError("locked multipart cleanup failed: "+str(result))

def multipart_hold(p):
    with locked(p) as bucket:
        response=multipart(bucket,p);rejected(delete(bucket,response["version_id"]),403,"AccessDenied");hold(bucket,"OFF")

def multi_delete(p):
    with locked(p,probe=True) as bucket:
        first=put(bucket,p,"file1")["version_id"];second=put(bucket,p,"file2")["version_id"]
        api(bucket,"PutObjectRetention",Key="file1",Retention=retention())
        result=bucket.call("delete_many_versions",objects=[{"key":"file1","version_id":first},{"key":"file2","version_id":second}])
        assert_equal(len(result["deleted"]),1);assert_equal(len(result["errors"]),1)
        error=result["errors"][0];assert_equal((error["code"],error["key"],error["version_id"]),("AccessDenied","file1",first))
        deleted=result["deleted"][0];assert_equal((deleted["key"],deleted["version_id"]),("file2",second))
        result=bucket.call("delete_many_versions",objects=[{"key":"file1","version_id":first}],wire_headers=BYPASS)
        assert_equal(len(result["errors"]),0);assert_equal(len(result["deleted"]),1)
        deleted=result["deleted"][0];assert_equal((deleted["key"],deleted["version_id"]),("file1",first))

def metadata(p,upload):
    with locked(p,probe=True) as bucket:
        if upload:put(bucket,p,headers=headers(DATE2030,"GOVERNANCE","ON"))
        else:
            put(bucket,p);hold(bucket,"ON");api(bucket,"PutObjectRetention",Key="file1",Retention=retention())
        response=bucket.call("head_version",key="file1");observed=response["http_headers"]
        assert_equal(observed["x-amz-object-lock-mode"],"GOVERNANCE");assert_equal(observed["x-amz-object-lock-legal-hold"],"ON")
        assert_equal(int(datetime.fromisoformat(observed["x-amz-object-lock-retain-until-date"].replace("Z","+00:00")).timestamp()*1000),DATE2030)
        hold(bucket,"OFF");require_ok(delete(bucket,response["version_id"],True))

def change_mode(p,mode):
    with locked(p,probe=mode!="from_compliance") as bucket:
        expiry=int((datetime.now(UTC)+timedelta(seconds=10)).timestamp()*1000);bucket.compliance_expiry=expiry
        put(bucket,p,headers=headers(expiry,"COMPLIANCE" if mode=="from_compliance" else "GOVERNANCE"))
        request={"Key":"file1","Retention":retention(expiry,"GOVERNANCE" if mode=="from_compliance" else "COMPLIANCE")}
        if mode=="with_bypass":request["BypassGovernanceRetention"]=True;api(bucket,"PutObjectRetention",**request)
        else:rejected(raw(bucket,"PutObjectRetention",**request),403,"AccessDenied")

BASE=VERSION_SCOPE_OPERATIONS+" bucket_create_locked put_version head_version wire_headers api.PutObjectRetention api.GetObjectRetention api.PutObjectLegalHold api.GetObjectLegalHold"
CASES=()
for legal_hold in (False,True):
    for write in (False,True):
        name=("put_" if write else "get_")+("legal_hold" if legal_hold else "retention")
        reference="test_object_lock_"+("put_legal_hold" if write else "get_legal_hold")+"_invalid_bucket" if legal_hold else "test_object_lock_"+("put_obj_retention" if write else "get_obj_retention")+"_invalid_bucket"
        operations=(BASE if write else SCOPE_OPERATIONS)+" put api."+("Put" if write else "Get")+"Object"+("LegalHold" if legal_hold else "Retention")
        CASES+=(case("ceph_object_lock_invalid_"+name,"Ceph 非锁桶 "+name,"原始abc与file1、精确400/InvalidRequest；负例写入需显式清理授权",operations,lambda p,legal_hold=legal_hold,write=write:invalid_bucket(p,legal_hold,write),reference,CEPH),)
for mode,reference in (("put","test_object_lock_put_obj_retention"),("invalid_mode","test_object_lock_put_obj_retention_invalid_mode"),("get","test_object_lock_get_obj_retention"),("iso","test_object_lock_get_obj_retention_iso8601"),("version","test_object_lock_put_obj_retention_versionid"),("override","test_object_lock_put_obj_retention_override_default_retention"),("increase","test_object_lock_put_obj_retention_increase_period"),("shorten","test_object_lock_put_obj_retention_shorten_period"),("shorten_bypass","test_object_lock_put_obj_retention_shorten_period_bypass")):
    CASES+=(case("ceph_object_lock_retention_"+mode,"Ceph 对象保留 "+mode,"保留2030/2140日期、原始模式和全部断言；显式授权并真实2秒bypass探针后执行，finally关闭hold清理全部版本",BASE+(" api.PutObjectLockConfiguration" if mode=="override" else ""),lambda p,mode=mode:retained(p,mode),reference,CEPH),)
for mode,reference in (("put","test_object_lock_put_legal_hold"),("invalid_status","test_object_lock_put_legal_hold_invalid_status"),("get","test_object_lock_get_legal_hold"),("delete_on","test_object_lock_delete_object_with_legal_hold_on"),("delete_off","test_object_lock_delete_object_with_legal_hold_off")):
    CASES+=(case("ceph_object_lock_hold_"+mode,"Ceph 法律保留 "+mode,"保留ON/OFF、非法abc、完整Get相等及删除400/403/204语义；先明确授权，finally按实际版本关闭hold再删",BASE,lambda p,mode=mode:legal(p,mode),reference,CEPH),)
for mode,reference in (("plain","test_object_lock_delete_object_with_retention"),("multipart","test_object_lock_delete_multipart_object_with_retention"),("marker","test_object_lock_delete_object_with_retention_and_marker")):
    CASES+=(case("ceph_object_lock_delete_"+mode,"Ceph 保留对象删除 "+mode,"真实版本ID/分块ETag/删除标记与原始403、bypass204，实际探针与finally清理",BASE+(" mp_create mp_part mp_complete_observed mp_abort_raw" if mode=="multipart" else ""),lambda p,mode=mode:retention_delete(p,mode),reference,CEPH),)
CASES+=(case("ceph_object_lock_multi_delete","Ceph 混合保留批删除","保留两对象实际版本ID、Deleted/Errors全部字段与bypass第二次完整结果",BASE+" delete_many_versions",multi_delete,"test_object_lock_multi_delete_object_with_retention",CEPH),case("ceph_object_lock_multipart_hold","Ceph 分块对象法律保留","原始abc单part、真实完成版本、删除403、关闭ON及finally abort/版本清理",BASE+" mp_create mp_part mp_complete_observed mp_abort_raw",multipart_hold,"test_object_lock_delete_multipart_object_with_legal_hold_on",CEPH))
CASES+=tuple(case("ceph_object_lock_metadata_"+("upload" if upload else "later"),"Ceph 保留对象元数据 "+("上传时" if upload else "设置后"),"保留原始三个HEAD锁字段、2030日期与实际版本ID清理；SDK签名链真实原始锁头/正文MD5",BASE,lambda p,upload=upload:metadata(p,upload),reference,CEPH) for upload,reference in ((False,"test_object_lock_get_obj_metadata"),(True,"test_object_lock_uploading_obj")))
CASES+=tuple(case("ceph_object_lock_mode_"+mode,"Ceph 对象保留模式转换 "+mode,"原始now+10秒和COMPLIANCE/GOVERNANCE、bypass或403；finally等待真实原始expiry后清理，不缩短保留",BASE,lambda p,mode=mode:change_mode(p,mode),reference,CEPH) for mode,reference in (("with_bypass","test_object_lock_changing_mode_from_governance_with_bypass"),("without_bypass","test_object_lock_changing_mode_from_governance_without_bypass"),("from_compliance","test_object_lock_changing_mode_from_compliance")))

def register(registry):
    registry.add_suite(Suite("ceph_object_locks",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
