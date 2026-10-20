"""Preserve Ceph's ordinary lifecycle transitions and original version/storage-class stage counts."""
from collections import defaultdict
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .version_scope import VersionScope,VERSION_SCOPE_OPERATIONS
from .ceph_lifecycle_expiration import interval,wait,lifecycle,objects
from .ceph_versioning import configure,create_versions
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH


def classes(p,count):
    values=p.ctx.workload.get("storage_classes",["STANDARD"])
    if len(values)<count:raise CaseNotRun("Ceph source requires configured storage classes","workload.storage_classes: at least "+str(count))
    return values

def by_class(bucket):
    result=bucket.call("versions_list")
    if "Versions" not in result["fields"]:raise KeyError("Versions")
    actual=defaultdict(list)
    for item in result["versions"]:actual[item["storage_class"]].append(item)
    return actual

def counts(bucket,sc,expected):
    actual=by_class(bucket)
    for name,size in zip(("STANDARD",sc[1],sc[2]),expected):assert_equal(len(actual[name]),size)

def current(p,multiple=False):
    sc=classes(p,3);clock=interval(p)
    with BucketScope(p) as bucket:
        for key in ("expire1/foo","expire1/bar","keep2/foo","keep2/bar","expire3/foo","expire3/bar"):bucket.put(key,key.encode())
        rules=[{"ID":"rule1","Transitions":[{"Days":1,"StorageClass":sc[1]}],"Prefix":"expire1/","Status":"Enabled"}]
        if multiple:rules[0]["Transitions"].append({"Days":7,"StorageClass":sc[2]})
        else:rules.append({"ID":"rule2","Transitions":[{"Days":6,"StorageClass":sc[2]}],"Prefix":"expire3/","Status":"Enabled"})
        lifecycle(bucket,rules)
        assert_equal(len(objects(bucket)),6)
        wait((5 if multiple else 4)*clock);counts(bucket,sc,(4,2,0))
        wait(clock);counts(bucket,sc,(4,2,0))
        wait((6 if multiple else 5)*clock);counts(bucket,sc,(4,0,2) if multiple else (2,2,2))

def noncurrent(p):
    sc=classes(p,3);clock=interval(p)
    with VersionScope(p) as bucket:
        # A pre-versioning object must remain a genuine null version after enable.
        bucket.put("test1/a",b"fooz")
        configure(bucket,"Enabled")
        lifecycle(bucket,[{"ID":"rule1","Prefix":"test1/","Status":"Enabled","NoncurrentVersionTransitions":[{"NoncurrentDays":1,"StorageClass":sc[1]},{"NoncurrentDays":5,"StorageClass":sc[2]}],"NoncurrentVersionExpiration":{"NoncurrentDays":9}}])
        create_versions(bucket,p,"test1/a",2);create_versions(bucket,p,"test1/b",3)
        assert_equal(len(by_class(bucket)["STANDARD"]),6)
        wait(4*clock);counts(bucket,sc,(2,4,0))
        wait(4*clock);counts(bucket,sc,(2,0,4))
        wait(6*clock);counts(bucket,sc,(2,0,0))

def plain_null(p):
    sc=classes(p,2);target=sc[1];assert_equal(target!="STANDARD",True);clock=interval(p)
    with VersionScope(p) as bucket:
        assert_equal(bucket.call("versioning_get").get("versioning_status"),None)
        bucket.put("testobjfoo",b"fooz")
        configure(bucket,"Enabled")
        lifecycle(bucket,[{"ID":"rule1","Prefix":"testobj","Status":"Enabled","Transitions":[{"Days":1,"StorageClass":target}]}])
        wait(4*clock);actual=by_class(bucket)
        assert_equal(len(actual["STANDARD"]),0);assert_equal(len(actual[target]),1)

BASE=SCOPE_OPERATIONS+" api.PutBucketLifecycleConfiguration lifecycle_legacy_prefix versions_list versions_storage_class put"
VBASE=VERSION_SCOPE_OPERATIONS+" api.PutBucketLifecycleConfiguration lifecycle_legacy_prefix versions_storage_class versioning_get versioning_put put"
VECTORS=(("current",current,"test_lifecycle_transition",BASE+" list_v1_page"),("current_multiple",lambda p:current(p,True),"test_lifecycle_transition_single_rule_multi_trans",BASE+" list_v1_page"),("noncurrent",noncurrent,"test_lifecycle_noncur_transition",VBASE+" put_version"),("plain_null",plain_null,"test_lifecycle_plain_null_version_current_transition",VBASE))
CASES=tuple(case("ceph_lc_transition_"+name,"Ceph 生命周期存储类别迁移 "+name,"原始规则、plain/null版本、真实ListObjectVersions.StorageClass和全部阶段计数；要求真实配置多存储类别和服务加速，不改变原等待倍数",ops,execute,reference,CEPH) for name,execute,reference,ops in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_lifecycle_transition",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
