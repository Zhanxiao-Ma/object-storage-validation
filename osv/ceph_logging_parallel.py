"""Preserve the source's two real fifty-thread phases on one fifty-connection native S3 client."""
from dataclasses import replace
import time
from .worker import Worker
from .bucket_scope import SCOPE_OPERATIONS
from .ceph_logging_controls import identity
from .ceph_logging_records import content,keys,read
from .ceph_logging_workloads import resources,extensions,enable,verify
from .scenarios import Suite,assert_equal
from .suites import Probe,case,run_cases,CEPH


def concurrent_put(p):
    identity(p)
    target=dict(p.ctx.worker.target);target['env']={**target.get('env',{}),'OSV_CONNECTIONS':'50'}
    worker=Worker(target,p.ctx.worker.timeout);payloads=[]
    try:
        probe=Probe(replace(p.ctx,worker=worker));assert_equal(str(worker.info['connections_per_worker']),'50');extension=extensions(probe)
        with resources(probe,False) as (source,unused,target_bucket):
            kind=enable(probe,source,source,target_bucket,'put',extension)
            requests=[]
            for index in range(50):
                name='myobject'+str(index);path=p.ctx.body.with_name(p.ctx.body.name+'.parallel-'+str(index));payloads.append(path);path.write_bytes(content());source.written_keys.add(name)
                requests.append({'op':'put_http','bucket':source.name,'key':name,'body_path':str(path)})
            result=source.call('concurrent_requests',requests=requests);assert_equal(len(result['results']),50)
            # The source's thread exceptions are not re-raised by join; retain
            # individual SDK outcomes without inventing per-thread success asserts.
            source_keys=keys(source)
            if not extension:time.sleep(5.5)
            if extension:requests=[{'op':'api.PostBucketLogging','bucket':source.name,'input':{}} for _ in range(50)]
            else:
                p.ctx.body.write_bytes(b'dummy');source.written_keys.add('dummy');requests=[{'op':'put','bucket':source.name,'key':'dummy','body_path':str(p.ctx.body)} for _ in range(50)]
            result=source.call('concurrent_requests',requests=requests);assert_equal(len(result['results']),50)
            body=''
            for key in keys(target_bucket):
                assert_equal(key.startswith('log/'),True);body+=read(target_bucket,probe,key)
            verify(body,source.name,'REST.PUT.OBJECT',source_keys,50,kind)
        return {'workload_sdk_clients':1,'connections':50,'concurrent_requests_per_phase':50,'cleanup_confirmed':source.cleanup_completed and target_bucket.cleanup_completed}
    finally:
        worker.close()
        for path in payloads:path.unlink(missing_ok=True)

CASES=(case('ceph_logging_parallel_put','Ceph 同client五十路日志写与flush','保留pool50、两个真实50线程阶段、第一阶段独立随机body、真实5.5秒等待/50次dummy，拼接全部log/日志并校验50原始记录；不把源线程异常升级为额外成功断言',SCOPE_OPERATIONS+' concurrent_requests put put_http list_v2_observed get_http api.PutBucketLogging api.PutBucketPolicy api.DeleteBucketPolicy',concurrent_put,'test_bucket_logging_put_concurrency',CEPH),)

def register(registry):
    registry.add_suite(Suite('ceph_logging_parallel',CASES[0].operations,lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
