"""Translate Ceph bucket/object ACL access matrices and genuinely unsigned listing calls."""
from contextlib import ExitStack
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CEPH

def anonymous_list(p,version,public):
    """Keep empty source buckets and exact anonymous 403/AccessDenied expectations; never filter the actual list response."""
    with BucketScope(p) as bucket:
        if public:api(bucket,"PutBucketAcl",ACL="public-read")
        response=bucket.raw("list_v1_page" if version==1 else "list_v2_observed",identity="anonymous")
        if public:require_ok(response)
        else:rejected(response,403,"AccessDenied")

def matrix(p,bucket_acl,object_acl,version=1):
    """Preserve original operation order, literal bodies and separate alternate clients after rejected writes; bucket and object grants must independently authorize actual SDK operations."""
    with BucketScope(p) as bucket:
        api(bucket,"PutBucketAcl",ACL=bucket_acl)
        bucket.put("foo",b"foocontent");api(bucket,"PutObjectAcl",Key="foo",ACL=object_acl)
        bucket.put("bar",b"barcontent")
        with ExitStack() as sessions:
            def fresh():
                worker=p.ctx.worker.fork();sessions.callback(worker.close);return worker
            def call(worker,op,**options):
                return worker.call({"op":op,"bucket":bucket.name,"identity":"alt",**options})
            def get(worker,key,allowed):
                p.ctx.output.unlink(missing_ok=True)
                response=call(worker,"get_http",key=key,output_path=str(p.ctx.output))
                if allowed:
                    require_ok(response);assert_equal(p.ctx.output.read_bytes(),b"foocontent")
                else:rejected(response,403)
            def put(worker,key,body,allowed):
                bucket.written_keys.add(key);p.ctx.body.write_bytes(body)
                response=call(worker,"put_http",key=key,body_path=str(p.ctx.body))
                if allowed:require_ok(response)
                else:rejected(response,403)
            def listing(worker,allowed):
                response=call(worker,"list_v1_page" if version==1 else "list_v2_observed")
                if allowed:assert_equal(require_ok(response)["keys"],["bar","foo"])
                else:rejected(response,403)
            alt=fresh()
            if bucket_acl=="private" and object_acl=="private":
                get(alt,"foo",False);get(alt,"bar",False);listing(alt,False)
                put(alt,"foo",b"barcontent",False)
                put(fresh(),"bar",b"baroverwrite",False);put(fresh(),"new",b"newcontent",False)
            elif bucket_acl=="public-read-write":
                get(alt,"foo",object_acl!="private")
                put(alt,"foo",b"foooverwrite" if object_acl=="public-read-write" else b"barcontent",True)
                get(alt,"bar",False);put(alt,"bar",b"baroverwrite",True)
                listing(alt,True);put(alt,"new",b"newcontent",True)
            else:
                get(alt,"foo",object_acl!="private")
                put(alt,"foo",b"barcontent" if object_acl=="private" else b"foooverwrite",False)
                alt2=fresh();get(alt2,"bar",False);put(alt2,"bar",b"baroverwrite",False)
                alt3=fresh();listing(alt3,bucket_acl=="public-read");put(alt3,"new",b"newcontent",False)

ANONYMOUS=tuple(case("ceph_access_anonymous_v"+str(version)+("_public" if public else "_private"),"Ceph 匿名桶列举 V"+str(version)+( " 公开" if public else " 私有"),"真实匿名 SDK 客户端，公开空桶成功，私有空桶精确 403/AccessDenied",SCOPE_OPERATIONS+" identity_anonymous "+("list_v1_page" if version==1 else "list_v2_observed")+(" api.PutBucketAcl" if public else ""),lambda p,version=version,public=public:anonymous_list(p,version,public),reference,CEPH) for version,public,reference in ((1,True,"test_bucket_list_objects_anonymous"),(2,True,"test_bucket_listv2_objects_anonymous"),(1,False,"test_bucket_list_objects_anonymous_fail"),(2,False,"test_bucket_listv2_objects_anonymous_fail")))
MATRICES=tuple((bucket_acl,object_acl,1,"test_access_bucket_"+bucket_acl.replace("-","")+"_object_"+object_acl.replace("-","")) for bucket_acl in ("private","public-read","public-read-write") for object_acl in ("private","public-read","public-read-write"))
MATRICES+=tuple(("private",object_acl,2,"test_access_bucket_private_objectv2_"+object_acl.replace("-","")) for object_acl in ("private","public-read","public-read-write"))
CASES=ANONYMOUS+tuple(case("ceph_access_matrix_"+bucket_acl.replace("-","_")+"_"+object_acl.replace("-","_")+"_v"+str(version),"Ceph 桶/对象 ACL 权限矩阵 "+bucket_acl+" / "+object_acl+" V"+str(version),"保留源码全部读/写/列举许可、精确 403、原始 foocontent/barcontent/覆盖内容与错误写入后的独立身份客户端",SCOPE_OPERATIONS+" identity_alt put get_http put_http api.PutBucketAcl api.PutObjectAcl "+("list_v1_page" if version==1 else "list_v2_observed"),lambda p,bucket_acl=bucket_acl,object_acl=object_acl,version=version:matrix(p,bucket_acl,object_acl,version),reference,CEPH) for bucket_acl,object_acl,version,reference in MATRICES)

def register(registry):
    registry.add_suite(Suite("ceph_access",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
