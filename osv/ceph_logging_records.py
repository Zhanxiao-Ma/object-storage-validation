"""Preserve pinned Ceph standard-log records, headers, naming and ACL-required source oracles."""
from datetime import datetime,timedelta
import random
import re
import shlex
import time
from .ceph_logging_controls import resources,document,set_policy,identity,logging_status
from .ceph_bucket_config import api
from .bucket_scope import SCOPE_OPERATIONS
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH

FIELDS=("BucketOwner","BucketName","RequestDateTime","RemoteIP","Requester","RequestID","Operation","Key","RequestURI","HTTPStatus","ErrorCode","BytesSent","ObjectSize","TotalTime","TurnAroundTime","Referrer","UserAgent","VersionID","HostID","SigVersion","CipherSuite","AuthType","HostHeader","TLSVersion","AccessPointARN","ACLRequired")

def parse(record):
    """Retain source bracket-to-quote preprocessing, shell tokenization and strict twenty-six-field standard record schema."""
    fields=shlex.split(record.replace("[",'"').replace("]",'"'))
    assert_equal(len(fields),26)
    return dict(zip(FIELDS,fields))

def records(body,bucket,event,keys,count):
    """Retain source substring matching against the full record, rather than silently strengthening it to parsed Key equality."""
    found=[]
    for line in body.splitlines():
        value=parse(line)
        if bucket in line and event in line and any(key in line for key in keys):found.append(value["Key"])
    assert_equal(len(found),count)

def field(body,bucket,event,key,name,wanted):
    for line in body.splitlines():
        if bucket in line and event in line and key in line:
            assert_equal(parse(line).get(name),wanted);return
    raise AssertionError("source log field record was not found")

_RNG=random.Random(7907)

def content():
    rng=_RNG;return bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(10,1024)))

def put(bucket,p,key):
    p.ctx.body.write_bytes(content())
    return bucket.call("put_http",key=key,body_path=str(p.ctx.body))

def flush(bucket):
    """Keep source standard fallback's real 5.5-second wait and dummy PUT; vendor POST flush requires a genuinely advertised API capability."""
    if "ceph_bucket_logging_extensions" in bucket.probe.ctx.worker.capabilities:
        if "api.PostBucketLogging" not in bucket.probe.ctx.worker.capabilities:raise CaseNotRun("Ceph-enabled logging source requires actual POST ?logging SDK support","api.PostBucketLogging")
        result=api(bucket,"PostBucketLogging");assert_equal(result["status_code"],200)
        return result["data"]["FlushedLoggingObject"]
    time.sleep(5.5);bucket.put("dummy",b"dummy");return None

def keys(bucket):return [item["key"] for item in bucket.call("list_v2_observed")["objects"]]

def read(bucket,p,key):
    bucket.call("get_http",key=key,output_path=str(p.ctx.output));return p.ctx.output.read_text()

def mtime(p):
    """Keep source one-second waits, identical configuration header equality, changed prefix and disable/re-enable lexical Last-Modified increases."""
    identity(p)
    with resources(p,2) as (source,target):
        configured=document(p,source,target);set_policy(target,configured)
        configuration=logging_status(source,target)
        def write():assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=configuration)["status_code"],200)
        def modified():
            result=api(source,"GetBucketLogging");assert_equal(result["status_code"],200);return result["http_headers"]["last-modified"]
        write();previous=modified()
        time.sleep(1);write();assert_equal(modified(),previous)
        time.sleep(1);configured["Statement"][0]["Resource"]="arn:aws:s3:::"+target.name+"/another-log/";set_policy(target,configured)
        configuration["LoggingEnabled"]["TargetPrefix"]="another-log/";write();current=modified();assert_equal(previous<current,True);previous=current
        time.sleep(1);assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus={})["status_code"],200)
        write();assert_equal(previous<modified(),True)

def request_id(p):
    """Feed the actual PUT's x-amz-request-id into the original standard-record field oracle after genuine log-object retrieval."""
    identity(p)
    with resources(p,2) as (source,target):
        set_policy(target,document(p,source,target));assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,target))["status_code"],200)
        observed=put(source,p,"my-test-object")["http_headers"].get("x-amz-request-id")
        if observed is None:raise AssertionError("failed to read x-amz-request-id from response headers")
        flush(source);listed=keys(target);assert_equal(len(listed),1)
        for key in listed:
            body=read(target,p,key);records(body,source.name,"REST.PUT.OBJECT",["my-test-object"],1);field(body,source.name,"REST.PUT.OBJECT","my-test-object","RequestID",observed)

def acl_required(p,object_acl):
    """Keep initial public ACL authorization, actual alternate GET/list, first ACLRequired=Yes record and second policy-authorized ACLRequired=- record with original resources/status."""
    identity(p)
    with resources(p,2) as (source,target):
        set_policy(target,document(p,source,target));put(source,p,"my-test-object")
        api(source,"PutObjectAcl" if object_acl else "PutBucketAcl",**({"Key":"my-test-object","ACL":"public-read"} if object_acl else {"ACL":"public-read"}))
        keys(source);assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=logging_status(source,target))["status_code"],200)
        event="REST.GET.OBJECT" if object_acl else "REST.GET.BUCKET";key="my-test-object" if object_acl else "-"
        def access():
            if object_acl:source.call("get_http",key="my-test-object",identity="alt",output_path=str(p.ctx.output))
            else:source.call("list_v2_observed",identity="alt")
        access();flush(source);listed=keys(target);assert_equal(len(listed),1)
        for item in listed:
            body=read(target,p,item);records(body,source.name,event,[key],1);field(body,source.name,event,key,"ACLRequired","Yes")
        allowed={"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":"s3:GetObject" if object_acl else "s3:ListBucket","Resource":["arn:aws:s3:::"+source.name]+(["arn:aws:s3:::"+source.name+"/*"] if object_acl else [])}]}
        set_policy(source,allowed);access();flush(source);listed=keys(target)
        body=read(target,p,listed[-1]);records(body,source.name,event,[key],1);field(body,source.name,event,key,"ACLRequired","-")

def verify_key(key,partitioned,prefix,now,ident,bucket):
    """Retain source prefixed/unprefixed regex alternatives, partition fields, matching duplicate date components and five-second naive datetime bound."""
    stamp=r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(.+)$"
    partition=r"([^/]+)/([^/]+)/([^/]+)/(\d{4})/(\d{2})/(\d{2})/" if partitioned else ""
    bare=re.match("^"+partition+stamp,key)
    match=bare or re.match("^(.+?)"+partition+stamp,key)
    if match is None:raise AssertionError("source logging key regex did not match")
    groups=list(match.groups());observed_prefix="" if bare else groups.pop(0);assert_equal(observed_prefix,prefix)
    date=None
    if partitioned:
        assert_equal(groups[:3],[ident,"default",bucket]);date=[int(v) for v in groups[3:6]];groups=groups[6:]
    numbers=[int(v) for v in groups[:6]]
    if date is not None:
        for left,right in zip(numbers[:3],date):
            if right:assert_equal(left,right)
    try:observed=datetime(*numbers)
    except ValueError:raise AssertionError("invalid date/time values in logging key") from None
    assert_equal(abs(observed-now)<=timedelta(seconds=5),True)

def object_names(p,partitioned):
    ident=identity(p)
    for prefix in ("log/",""):
        with resources(p,2) as (source,target):
            configured=document(p,source,target);configured["Statement"][0]["Resource"]="arn:aws:s3:::"+target.name+"/"+prefix;set_policy(target,configured)
            formatting={"PartitionedPrefix":{"PartitionDateSource":"DeliveryTime"}} if partitioned else {"SimplePrefix":{}}
            configuration={"LoggingEnabled":{"TargetBucket":target.name,"TargetPrefix":prefix,"TargetObjectKeyFormat":formatting}}
            assert_equal(api(source,"PutBucketLogging",BucketLoggingStatus=configuration)["status_code"],200)
            for index in range(5):put(source,p,"myobject"+str(index))
            flush(source);listed=keys(target);assert_equal(len(listed),1)
            verify_key(listed[0],partitioned,prefix,datetime.now(),ident,source.name)

BASE=SCOPE_OPERATIONS+" api.GetBucketLogging api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy"
CASES=(case("ceph_logging_records_mtime","Ceph 日志配置修改时间","原始1秒等待、不变配置时间相等、改前缀和重启的头字符串比较；不解析为新日期语义",BASE,mtime,"test_bucket_logging_mtime",CEPH),case("ceph_logging_records_request_id","Ceph 日志RequestID","真实PUT响应x-amz-request-id直接进入实际Get日志正文消费者，26字段、单条记录和RequestID一致",BASE+" put_http put list_v2_observed get_http",request_id,"test_bucket_logging_request_id",CEPH))
CASES+=tuple(case("ceph_logging_records_acl_"+("object" if obj else "bucket"),"Ceph 日志 ACLRequired "+("对象" if obj else "桶"),"原始公开ACL到公开策略、真实alt请求、两次标准flush和26字段日志的Yes/-与记录数断言",BASE+" put_http put list_v2_observed get_http identity_alt "+("api.PutObjectAcl" if obj else "api.PutBucketAcl"),lambda p,obj=obj:acl_required(p,obj),reference,CEPH) for obj,reference in ((False,"test_bucket_logging_bucket_acl_required"),(True,"test_bucket_logging_object_acl_required")))
CASES+=tuple(case("ceph_logging_records_key_"+("partitioned" if partitioned else "simple"),"Ceph 日志对象命名 "+("分区" if partitioned else "简单"),"完整log/及空前缀两向量、5原始随机小对象、标准flush、原始regex和固定default区域/5秒时间界限",BASE+" put_http put list_v2_observed logging_target_key_format",lambda p,partitioned=partitioned:object_names(p,partitioned),reference,CEPH) for partitioned,reference in ((False,"test_bucket_logging_simple_key"),(True,"test_bucket_logging_partitioned_key")))

def register(registry):
    registry.add_suite(Suite("ceph_logging_records",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
