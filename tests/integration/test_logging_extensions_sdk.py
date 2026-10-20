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
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
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
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for field,value in (('LoggingType','Journal'),('ObjectRollTime',5),('RecordsBatchSize',0)):
                with self.subTest(language=language,field=field),fixture_worker(language,[],'s3') as (worker,server):
                    result=worker.call({'op':'api.PutBucketLogging','bucket':'fixture-1250000000','input':{'BucketLoggingStatus':{'LoggingEnabled':{'TargetBucket':'target','TargetPrefix':'log/',field:value}}}})
                    self.assertFalse(result.get('ok'));self.assertIn(field,result.get('error',''));self.assertEqual(server.requests,[])

    def test_selected_language_runs_with_only_its_real_worker_artifact_present(self):
        """Mirror single-language CI with a temporary SDK tree containing only the selected genuine artifact; all four original module consumers must execute their real default checks without attempting the absent other-language worker."""
        from unittest.mock import patch
        import sdk_fixtures
        from test_logging_policy_transitions_sdk import LoggingPolicyTransitionsSdkTest
        from test_logging_prefixes_sdk import LoggingPrefixesSdkTest
        from test_logging_tenant_parallel_sdk import TenantParallelSdkTest
        original_root=sdk_fixtures.ROOT
        consumers=((LoggingExtensionsSdkTest,'test_three_complete_sources_remain_not_run_before_any_bucket_or_network_io'),
            (LoggingPolicyTransitionsSdkTest,'test_default_four_sources_and_journal_model_requirements_never_fall_back'),
            (LoggingPrefixesSdkTest,'test_defaults_require_account_metadata_before_creating_related_buckets'),
            (TenantParallelSdkTest,'test_both_source_defaults_and_tenant_prerequisites_stop_before_io'))
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                isolated=Path(temporary)
                artifact=Path('drivers/java/target/sdk-worker-0.1.0.jar') if language=='java' else Path('drivers/cpp/build/osv-cpp-s3')
                selected=isolated/artifact;selected.parent.mkdir(parents=True);selected.symlink_to(original_root/artifact)
                absent=isolated/('drivers/cpp/build/osv-cpp-s3' if language=='java' else 'drivers/java/target/sdk-worker-0.1.0.jar')
                self.assertFalse(absent.exists())
                with patch('sdk_fixtures.ROOT',isolated),patch.dict(os.environ,{'OSV_S3_TEST_LANGUAGES':language}):
                    for test_class,method in consumers:
                        with self.subTest(consumer=test_class.__name__):getattr(test_class(method),method)()
                self.assertTrue(selected.exists());self.assertFalse(absent.exists())
