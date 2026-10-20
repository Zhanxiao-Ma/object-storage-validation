"""Translate original Ceph policy list authorization through genuinely distinct SDK identities."""
import json
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

def document(bucket=None):
    resources=["arn:aws:s3:::*","arn:aws:s3:::*/*"] if bucket is None else ["arn:aws:s3:::"+bucket,"arn:aws:s3:::"+bucket+"/*"]
    return json.dumps({"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":"s3:ListBucket","Resource":resources}]})

def listing(bucket,version):
    """Read through the alternate SDK client rather than substituting the bucket owner's credentials."""
    result=bucket.call("list_v1_page" if version==1 else "list_v2_observed",identity="alt",prefix="")
    assert_equal(len(result["objects"]),1)

def policy(p,version,another):
    """Keep source asdf/abcd objects and policy resources; feed the actual SDK policy GET output directly into the second bucket's actual PUT."""
    with BucketScope(p) as first:
        first.put("asdf",b"asdf")
        api(first,"PutBucketPolicy",Policy=document(None if another else first.name))
        if another:
            with BucketScope(p) as second:
                second.put("abcd",b"abcd")
                observed=api(first,"GetBucketPolicy")["data"]["Policy"]
                api(second,"PutBucketPolicy",Policy=observed)
                listing(first,version);listing(second,version)
            return {"isolated_buckets":[first.name,second.name],"policy_producer_consumer":"actual GetBucketPolicy -> PutBucketPolicy"}
        listing(first,version)
    return {"isolated_bucket":first.name,"cleanup_confirmed":first.cleanup_completed}

VECTORS=((1,False,"test_bucket_policy"),(2,False,"test_bucketv2_policy"),(1,True,"test_bucket_policy_another_bucket"),(2,True,"test_bucketv2_policy_another_bucket"))
CASES=tuple(case("ceph_policy_list_v"+str(version)+("_another" if another else ""),"Ceph 另一身份策略列举 V"+str(version)+( " 跨桶策略" if another else ""),"保留原始资源 ARN、asdf/abcd 数据、真实另一个身份及源列举断言；跨桶复用实际策略读回输出",SCOPE_OPERATIONS+" put identity_alt api.PutBucketPolicy "+("list_v1_page" if version==1 else "list_v2_observed")+(" api.GetBucketPolicy" if another else ""),lambda p,version=version,another=another:policy(p,version,another),reference,CEPH) for version,another,reference in VECTORS)

def register(registry):
    registry.add_suite(Suite("ceph_bucket_policy",frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
