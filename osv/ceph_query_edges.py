"""Retain source nonstandard query parameters and their exact official-SDK response consumers."""
from contextlib import ExitStack
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH


KEYS=['ado','bot','cob','dog','emu','fez','gnu','hex','abc/ink','abc/jet','abc/kin','abc/lax','abc/mux',
 'def/nim','def/owl','def/pie','def/qed','def/rye','ghi/sew','ghi/tor','ghi/uke','ghi/via','ghi/wit','xix','yak','zoo']


def head_usage(p,extended=False):
    """Feed real read-stats=true HEAD headers into original numeric or literal count/quota assertions, retaining true source object payloads."""
    with BucketScope(p) as bucket:
        if not extended:bucket.put('foo',b'foo')
        def head():return bucket.call('bucket_head',wire_query={'read-stats':'true'})['http_headers']
        headers=head()
        if extended:
            assert_equal(int(headers['x-rgw-object-count']),0);assert_equal(int(headers['x-rgw-bytes-used']),0)
            for key in ('foo','bar','baz'):bucket.put(key,key.encode())
            headers=head();assert_equal(int(headers['x-rgw-object-count']),3);assert_equal(int(headers['x-rgw-bytes-used']),9)
        else:
            assert_equal(headers['x-rgw-object-count'],'1');assert_equal(headers['x-rgw-bytes-used'],'3');assert_equal(headers['x-rgw-quota-max-buckets'],'1000')


def unordered(p,version):
    """Preserve original count/sort-return-value defect, real marker producer and V2 callback registration mismatch; only source V1 operations carry the extension."""
    with BucketScope(p) as bucket:
        for key in KEYS:bucket.put(key,key.encode())
        query={'wire_query':{'allow-unordered':'true'}} if version==1 else {}
        op='list_v1_page' if version==1 else 'list_v2_observed'
        keys_in=list(KEYS)
        output=bucket.call(op,max_keys=1000,**query)['keys']
        assert_equal(len(keys_in),len(output))
        # The upstream asserts list.sort() return values, both None. Do not
        # silently strengthen or repair this source assertion to sorted equality.
        assert_equal(keys_in.sort(),output.sort())
        assert_equal(len(bucket.call(op,max_keys=1000,prefix='abc/',**query)['keys']),5)
        first=bucket.call(op,max_keys=6,**query)['keys'];assert_equal(len(first),6)
        second=bucket.call(op,max_keys=6,**({'marker':first[-1]} if version==1 else {'start_after':first[-1]}),**query)['keys'];assert_equal(len(second),6)
        assert_equal(len(set(first).intersection(second)),0)
        # The V2 source mistakenly registers a ListObjects event; preserve that
        # only its final ListObjects call receives allow-unordered=true.
        rejected(bucket.raw('list_v1_page',delimiter='/',wire_query={'allow-unordered':'true'}),400,'InvalidArgument')


def invalid_maxkeys(p):
    """Submit original malformed max-keys=blah after normal SDK serialization and require precise service 400/InvalidArgument."""
    with BucketScope(p) as bucket:
        for key in ('bar','baz','foo','quxx'):bucket.put(key,key.encode())
        rejected(bucket.raw('list_v1_page',wire_query={'max-keys':'blah'}),400,'InvalidArgument')


def response_headers(p):
    """Keep private empty foo object and all six source response query overrides, consuming actual official SDK HTTP headers and status."""
    with BucketScope(p,create_operation='bucket_create_acl',create_options={'acl':'private'}) as bucket:
        p.ctx.body.write_bytes(b'');bucket.call('put_acl',key='foo',acl='private',body_path=str(p.ctx.body))
        query={'response-cache-control':'no-cache','response-content-disposition':'bla','response-content-encoding':'aaa',
         'response-content-language':'esperanto','response-content-type':'foo/bar','response-expires':'123'}
        result=bucket.call('get_http',key='foo',output_path=str(p.ctx.output),wire_query=query)
        assert_equal(result['status_code'],200)
        for name,expected in (('content-type','foo/bar'),('content-disposition','bla'),('content-language','esperanto'),('content-encoding','aaa'),('cache-control','no-cache')):
            assert_equal(result['http_headers'][name],expected)


def list_buckets_paginated(p):
    """Keep empty-account then one/two-created-bucket ordering and opaque token handoff; never remove unrelated account buckets to manufacture the source prerequisite."""
    if 'list_buckets_pagination' not in p.ctx.worker.capabilities:raise CaseNotRun('official SDK model has no account pagination response token','SDK list_buckets_pagination')
    def listing(**query):
        return require_ok(p.ctx.worker.call({'op':'api.ListBuckets','input':{},'wire_query':{'max-buckets':'1',**query}}))['data']
    initial=listing()
    if initial.get('Buckets'):raise CaseNotRun('source pagination requires an isolated initially empty account','account containing no buckets')
    assert_equal(len(initial['Buckets']),0);assert_equal('ContinuationToken' in initial,False)
    with ExitStack() as owned:
        first=owned.enter_context(BucketScope(p));one=listing();assert_equal(len(one['Buckets']),1);assert_equal(one['Buckets'][0]['Name'],first.name);assert_equal('ContinuationToken' in one,False)
        second=owned.enter_context(BucketScope(p));two=listing();assert_equal(len(two['Buckets']),1);assert_equal(two['Buckets'][0]['Name'],first.name)
        last=listing(**{'continuation-token':two['ContinuationToken']});assert_equal(len(last['Buckets']),1);assert_equal(last['Buckets'][0]['Name'],second.name);assert_equal('ContinuationToken' in last,False)


S=SCOPE_OPERATIONS+' put wire_query'
CASES=(
 case('ceph_query_head_usage','Ceph HEAD桶用量原始扩展','read-stats=true原始对象数/字节数/配额头字面值',S,head_usage,'test_head_bucket_usage',CEPH),
 case('ceph_query_unordered_v1','Ceph V1无序列举原始扩展','完整26键计数、原sort返回值断言、真实marker与delimiter拒绝',S+' list_v1_page',lambda p:unordered(p,1),'test_bucket_list_unordered',CEPH),
 case('ceph_query_unordered_v2','Ceph V2原始callback错绑','忠实源V2无扩展、末尾V1才有扩展；保留全部原断言',S+' list_v1_page list_v2_observed',lambda p:unordered(p,2),'test_bucket_listv2_unordered',CEPH),
 case('ceph_query_invalid_maxkeys','Ceph非法max-keys query','原blah值必须400/InvalidArgument',S+' list_v1_page',invalid_maxkeys,'test_bucket_list_maxkeys_invalid',CEPH),
 case('ceph_query_head_extended','Ceph扩展HEAD空桶到三对象','原零到三对象/九字节整数断言',S,lambda p:head_usage(p,True),'test_bucket_head_extended',CEPH),
 case('ceph_query_response_headers','Ceph GET原始响应覆盖query','六个原query实际SDK请求及原五响应头精确断言',SCOPE_OPERATIONS+' wire_query bucket_create_acl put_acl get_http',response_headers,'test_object_raw_response_headers',CEPH),
 case('ceph_query_list_buckets_paginated','Ceph账户桶分页','空账户前提、创建顺序、真实opaque token与最终字段缺失',SCOPE_OPERATIONS+' wire_query api.ListBuckets list_buckets_pagination',list_buckets_paginated,'test_list_buckets_paginated',CEPH),
)


def register(registry):
    registry.add_suite(Suite('ceph_query_edges',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
