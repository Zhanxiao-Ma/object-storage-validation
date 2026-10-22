"""Optional genuine MinIO SDK adapters exercised against isolated local S3 storage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from osv.cli import load_config,run
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("OSV_RUN_MINIO_TESTS")=="1","set OSV_RUN_MINIO_TESTS=1 after building real MinIO workers")
class MinioSdkTest(unittest.TestCase):
    """Feed actual MinIO producer output to independent SDK consumers and the production benchmark verifier."""
    @classmethod
    def setUpClass(cls):
        import boto3
        # Existing exact-count checks remain count-driven; a separate test exercises sustained windows.
        cls.write_window = patch("osv.benchmark.MIN_WRITE_SECONDS", 0)
        cls.write_window.start();cls.addClassCleanup(cls.write_window.stop)
        from local_moto import LocalMotoServer
        cls.server=LocalMotoServer();cls.server.start();cls.addClassCleanup(cls.server.stop)
        host,port=cls.server.get_host_and_port()
        cls.environment=patch.dict(os.environ,{"OSV_ENDPOINT":f"http://{host}:{port}","OSV_REGION":"us-east-1","OSV_BUCKET":"osv-minio-sdk-test",
            "OSV_ACCESS_KEY":"local-test","OSV_SECRET_KEY":"local-test","OSV_SESSION_TOKEN":"","OSV_PATH_STYLE":"true","OSV_RETRIES":"0","AWS_EC2_METADATA_DISABLED":"true"})
        cls.environment.start();cls.addClassCleanup(cls.environment.stop)
        cls.client=boto3.client("s3",endpoint_url=f"http://{host}:{port}",region_name="us-east-1",aws_access_key_id="local-test",aws_secret_access_key="local-test")
        cls.client.create_bucket(Bucket="osv-minio-sdk-test")
        cls.targets=[{"id":"minio-java","provider":"minio","language":"java","api":"s3","sdk":"minio-java","sdk_version":"9.0.3",
            "command":["java","-Xmx256m","-jar",str(ROOT/"drivers/minio/java/target/minio-worker-0.1.0.jar")]},
            {"id":"minio-cpp","provider":"minio","language":"cpp","api":"s3","sdk":"minio-cpp","sdk_version":"1.0.0",
            "command":[str(ROOT/"drivers/minio/cpp/build/osv-cpp-minio")]}]

    def test_real_minio_uploads_feed_independent_reads_and_exact_ranges(self):
        """Purpose: store empty/binary/special-key inputs with each actual MinIO SDK; expect independent boto3 readback, exact first/last/full Range bytes from both SDKs, true HEAD sizes and no surviving keys after real deletes."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            for target in self.targets:
                worker=Worker(target,30)
                try:
                    for index,payload in enumerate((b"",bytes(range(256))*17)):
                        key=target['id']+"/space + 中文/"+str(index);source=directory/"input";source.write_bytes(payload)
                        self.assertTrue(worker.call({"op":"put","key":key,"body_path":str(source)})['ok'])
                        self.assertEqual(self.client.get_object(Bucket="osv-minio-sdk-test",Key=key)['Body'].read(),payload)
                        self.assertEqual(worker.call({"op":"head","key":key})['size'],len(payload))
                        self.assertIn(key,worker.call({"op":"list","prefix":target['id']})['keys'])
                        for offset,length in ([(0,len(payload)),(0,1),(len(payload)-1,1)] if payload else [(None,None)]):
                            request={"op":"get","key":key,"output_path":str(directory/"output")}
                            if offset is not None:request.update(offset=offset,length=length)
                            self.assertTrue(worker.call(request)['ok'])
                            self.assertEqual((directory/"output").read_bytes(),payload if offset is None else payload[offset:offset+length])
                        self.assertTrue(worker.call({"op":"delete","key":key})['ok'])
                        self.assertFalse(worker.call({"op":"get","key":key,"output_path":str(directory/"output")})['ok'])
                finally:worker.close()
        self.assertEqual(self.client.list_objects_v2(Bucket="osv-minio-sdk-test").get("KeyCount"),0)

    def test_real_minio_performance_runs_all_cases_and_rejects_bad_etags(self):
        """Purpose: run every production performance case with real MinIO Java/C++ SDKs and then complete an upload using an independently wrong ETag; expect exact read/hash verification, actual completion rejection, successful abort and empty storage."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);manifest=directory/"manifest.json"
            manifest.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{"mode":"performance","object_size":1024,"range_size":17,"concurrency":2,"requests":8,"rounds":1,"warmup_requests":0,
                "objbench":{"small_objects":2,"small_object_size":5,"big_object_size":17,"block_size":8},
                "read":{"small_object_sizes":[3,8192],"objects":2,"object_size":65537,"range_sizes":[1,4096,65536],"patterns":["sequential","random","hot"]},
                "multipart":{"object_sizes":[5*1024**2+17],"part_sizes":[5*1024**2],"objects":2}}}))
            report=run(load_config(manifest),directory/"results")
            self.assertEqual(report['execution_errors'],[],str(report['execution_errors']))
            self.assertTrue(all(r['ok'] for r in report['samples']),[r for r in report['samples'] if not r['ok']])
            for target in self.targets:
                rows=[r for r in report['samples'] if r['target']==target['id']]
                self.assertEqual(len(rows),171)
                self.assertEqual(sum(r['op'].startswith('read_') for r in rows),88)
                independent=[r for r in rows if r['op']=='parallel_put']
                self.assertEqual(len(independent),8)
                self.assertEqual(len({r['key'] for r in independent}),2)
                self.assertTrue(all(r['rpc_wall_ns']>=r['elapsed_ns'] for r in independent))
                self.assertEqual(sum(r['op'].startswith('multipart_total_') for r in rows),2)
                worker=Worker(target,30)
                try:
                    key=target['id']+"/reject";upload=worker.call({"op":"mp_create","key":key})['upload_id']
                    source=directory/"part";source.write_bytes(b"independent short last part")
                    part=worker.call({"op":"mp_part","key":key,"upload_id":upload,"part_number":1,"body_path":str(source)})
                    self.assertTrue(part['ok']);self.assertTrue(part['etag'])
                    result=worker.call({"op":"mp_complete","key":key,"upload_id":upload,"parts":[{"part_number":1,"etag":"00000000000000000000000000000000"}]})
                    self.assertFalse(result['ok']);self.assertIn('InvalidPart',result.get('error_code',result['error']))
                    self.assertTrue(worker.call({"op":"mp_abort","key":key,"upload_id":upload})['ok'])
                finally:worker.close()
        self.assertEqual(self.client.list_objects_v2(Bucket="osv-minio-sdk-test").get("KeyCount"),0)
        self.assertEqual(self.client.list_multipart_uploads(Bucket="osv-minio-sdk-test").get("Uploads",[]),[])

    def test_actual_paginated_listing_preserves_every_key(self):
        """Purpose: create 1001 distinct objects with an independent S3 producer; expect both real MinIO SDKs to traverse the 1000-key page boundary with no missing/duplicate keys and leave storage empty after deletes."""
        keys=["pagination/"+str(index).zfill(4) for index in range(1001)]
        try:
            for key in keys:self.client.put_object(Bucket="osv-minio-sdk-test",Key=key,Body=b"independent")
            for target in self.targets:
                worker=Worker(target,30)
                try:
                    response=worker.call({"op":"list","prefix":"pagination/"})
                    self.assertTrue(response["ok"],response)
                    self.assertEqual(response["keys"],keys)
                finally:worker.close()
        finally:
            for offset in range(0,len(keys),1000):
                self.client.delete_objects(Bucket="osv-minio-sdk-test",Delete={"Objects":[{"Key":key} for key in keys[offset:offset+1000]]})
        self.assertEqual(self.client.list_objects_v2(Bucket="osv-minio-sdk-test").get("KeyCount"),0)

    def test_real_minio_sustained_writes_verify_every_extra_key_and_part(self):
        """Purpose: extend genuine MinIO Java/C++ PUT and multipart windows using prepared files; expect fresh extra keys, exact readbacks, bounded measured windows, successful cleanup, and no SDK/protocol simulation."""
        with tempfile.TemporaryDirectory() as temporary, patch("osv.benchmark.MIN_WRITE_SECONDS", .1):
            directory=Path(temporary);manifest=directory/"manifest.json"
            manifest.write_text(json.dumps({"schema_version":1,"targets":self.targets,"workload":{"mode":"performance","object_size":1024,"range_size":17,"concurrency":2,"requests":4,"rounds":1,"warmup_requests":0,
                "objbench":{"small_objects":2,"small_object_size":5,"big_object_size":1024**2,"block_size":1024**2},
                "read":{"small_object_sizes":[3],"objects":1,"object_size":65537,"range_sizes":[4096],"patterns":["hot"]},
                "multipart":{"object_sizes":[5*1024**2+17],"part_sizes":[5*1024**2],"objects":1}}}))
            report=run(load_config(manifest),directory/"results")
            self.assertEqual(report["execution_errors"],[])
            self.assertTrue(all(r["ok"] for r in report["samples"]),[r for r in report["samples"] if not r["ok"]])
            for target in self.targets:
                puts=[r for r in report["samples"] if r["target"]==target["id"] and r["op"]=="put"]
                self.assertGreater(len(puts),1)
                self.assertEqual(len([r for r in report["samples"] if r["target"]==target["id"] and r["op"]=="get"]),1)
                upload=next(b for b in report["batches"] if b["target"]==target["id"] and b["op"]=="put")
                self.assertEqual(upload["verification"]["extra_objects"],len(puts)-1)
                self.assertEqual(upload["verification"]["failures"],0)
                windows=[b for b in report["batches"] if b["target"]==target["id"] and b.get("minimum_seconds",0)>0]
                self.assertEqual(len(windows),4)
                self.assertTrue(all(b["duration_satisfied"] and b["wall_seconds"]>=.1 for b in windows))
            self.assertEqual(self.client.list_objects_v2(Bucket="osv-minio-sdk-test").get("KeyCount"),0)
            self.assertFalse(self.client.list_multipart_uploads(Bucket="osv-minio-sdk-test").get("Uploads",[]))
