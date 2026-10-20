"""Prove real stock SDKs cannot silently execute Ceph-only logging fields or POST flush."""
import os
from pathlib import Path
import tempfile
import unittest
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.ceph_logging_extensions import CASES
from sdk_fixtures import fixture_worker

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_EXTENSION_TESTS')=='1','enable actual native SDK extension capability checks')
class LoggingExtensionsSdkTest(unittest.TestCase):
    """Use actual worker advertisements and strict typed model rejection, never injected capabilities or replacement XML."""
    def test_three_complete_sources_remain_not_run_before_any_bucket_or_network_io(self):
        """Run all three complete extension sources against actual SDK capabilities and directly invoke their guards; expect truthful not_run/missing-capability and no HTTP requests, never fake source pass."""
        for language in ('java','cpp'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'source-user'}}})
                self.assertNotIn('ceph_bucket_logging_extensions',worker.capabilities);self.assertNotIn('api.PostBucketLogging',worker.capabilities)
                results=run_cases(ctx,CASES);self.assertEqual(len(results),3);self.assertEqual({row['status'] for row in results},{'not_run'})
                for case in CASES:
                    with self.subTest(case=case.name),self.assertRaises(CaseNotRun) as missing:case.execute(Probe(ctx))
                    self.assertIn('ceph_bucket_logging_extensions',missing.exception.requirements)
                    if 'mpu_j' in case.name:self.assertIn('api.PostBucketLogging',missing.exception.requirements)
                self.assertEqual(server.requests,[]);self.assertEqual(list(d.iterdir()),[])

    def test_each_original_ceph_extension_field_is_rejected_by_actual_native_sdk_model(self):
        """Send each original LoggingType/ObjectRollTime/RecordsBatchSize field to actual Java/C++ typed model bridges; expect explicit unsupported member errors before HTTP, proving the prerequisite reflects real SDK limits and no field is dropped."""
        for language in ('java','cpp'):
            for field,value in (('LoggingType','Journal'),('ObjectRollTime',5),('RecordsBatchSize',0)):
                with self.subTest(language=language,field=field),fixture_worker(language,[],'s3') as (worker,server):
                    result=worker.call({'op':'api.PutBucketLogging','bucket':'fixture-1250000000','input':{'BucketLoggingStatus':{'LoggingEnabled':{'TargetBucket':'target','TargetPrefix':'log/',field:value}}}})
                    self.assertFalse(result.get('ok'));self.assertIn(field,result.get('error',''));self.assertEqual(server.requests,[])
