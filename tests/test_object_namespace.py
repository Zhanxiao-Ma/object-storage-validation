"""Exercise prefix isolation with the real reference producer and consumer."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from osv.bucket_scope import BucketScope
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.worker import Worker
from osv.ceph_list_exact import CASES as LIST_CASES
from osv.s3_delete_compatibility import CASES as DELETE_CASES

ROOT=Path(__file__).resolve().parents[1]


class ObjectNamespaceTest(unittest.TestCase):
    """Require genuine scoped IO without any create/delete bucket permission."""
    def setUp(self):
        """Create a real filesystem worker and independent sentinel; expect no bucket fixture is necessary."""
        self.temporary=tempfile.TemporaryDirectory()
        self.directory=Path(self.temporary.name)
        self.worker=Worker({'command':[sys.executable,str(ROOT/'drivers/reference.py')],
                            'env':{'OSV_REFERENCE_ROOT':str(self.directory/'objects')}},10)
        self.original=self.worker.call
        self.requests=[]
        def observe(request):
            self.requests.append(request)
            if request['op'].startswith('bucket_'):
                raise AssertionError('object test requested bucket management: '+request['op'])
            return self.original(request)
        self.worker.call=observe
        self.worker.capabilities=self.worker.capabilities-{'bucket_head','bucket_create_raw','bucket_delete'}
        self.ctx=Context(self.worker,'osv/independent-run/',self.directory,b'independent',self.directory/'body',
                         self.directory/'read',{'object_scope':'prefix','bucket_management':False})
        self.ctx.body.write_bytes(b'preserve existing data')
        self.original({'op':'put','key':'sentinel','body_path':str(self.ctx.body)})

    def tearDown(self):
        """Close the actual child process and local fixture; expect no process leak on assertion failure."""
        self.worker.close()
        self.temporary.cleanup()

    def probe(self):
        return Probe(replace(self.ctx,object_namespace=True,scope_state={}))

    def assert_sentinel(self):
        result=self.original({'op':'get','key':'sentinel','output_path':str(self.ctx.output)})
        self.assertTrue(result['ok'])
        self.assertEqual(self.ctx.output.read_bytes(),b'preserve existing data')

    def test_real_readback_physical_keys_and_failure_cleanup_preserve_other_data(self):
        """Write actual bytes then fail the case; expect physical prefixed SDK keys, confirmed cleanup and unchanged outside bytes."""
        scope=BucketScope(self.probe())
        with self.assertRaisesRegex(AssertionError,'original case failure'):
            with scope:
                for key in ('plain','special <&+% 中文',' leading ','nested/path'):
                    scope.put(key,key.encode())
                    result=scope.call('get',key=key,output_path=str(self.ctx.output))
                    self.assertEqual(self.requests[-1]['key'],scope.key(key))
                    self.assertTrue(self.requests[-1]['key'].startswith(scope.prefix))
                    self.assertIn(scope.key(key),scope.call('list',prefix='')['keys'])
                    self.assertEqual(self.ctx.output.read_bytes(),key.encode())
                raise AssertionError('original case failure')
        self.assertTrue(scope.cleanup_completed)
        self.assertEqual(self.original({'op':'list','prefix':scope.prefix})['keys'],[])
        self.assert_sentinel()
        self.assertFalse(any(r['op'].startswith('bucket_') for r in self.requests))

    def test_exact_real_pagination_delimiters_and_quiet_delete_need_no_bucket_operations(self):
        """Run production listing and deletion cases with real returned tokens; expect original semantics and zero bucket operations."""
        names={'ceph_exact_list_v1_delimiter_prefix','ceph_exact_list_v2_continuationtoken_walk',
               'ceph_exact_list_v1_marker_unreadable','ceph_exact_list_v2_prefix_empty',
               'ceph_exact_list_v2_delimiter_basic','ceph_exact_list_v2_startafter_not_in_list'}
        cases=[c for c in LIST_CASES if c.name in names]+[c for c in DELETE_CASES if c.name in {'s3_delete_many_quiet','s3_delete_many_verbose'}]
        rows=run_cases(self.ctx,cases)
        self.assertEqual(len(rows),8)
        self.assertTrue(all(row['status']=='pass' for row in rows),rows)
        self.assertTrue(all(row['isolation']['fresh_bucket_created'] is False for row in rows))
        self.assert_sentinel()
        self.assertEqual(self.original({'op':'list','prefix':self.ctx.prefix})['keys'],[])

    def test_second_bucket_and_bucket_mutation_are_explicitly_not_substituted(self):
        """Attempt a second simultaneous scope and an in-scope bucket mutation; expect prerequisites reported before unsafe IO."""
        from osv.suites import CaseNotRun
        probe=self.probe()
        with BucketScope(probe) as first:
            first.put('same-key',b'first actual producer')
            with self.assertRaisesRegex(CaseNotRun,'two distinct buckets'):
                with BucketScope(probe):self.fail('cross-bucket contract was substituted')
            for op in ('bucket_delete','versioning_put','api.PutBucketPolicy'):
                with self.subTest(op=op),self.assertRaises(CaseNotRun):first.raw(op)
        self.assert_sentinel()

    def test_bad_cleanup_listing_and_request_escapes_never_touch_other_data(self):
        """Corrupt a genuine listing with an outside sentinel and request bucket/key overrides; expect refusal before outside deletion."""
        scope=BucketScope(self.probe())
        with scope:
            scope.put('own',b'actual')
            with self.assertRaises(ValueError):scope.raw('delete',bucket='other',key='sentinel')
            with self.assertRaises(ValueError):scope.raw('delete',key=self.ctx.prefix+'other-scope/sentinel')
            with self.assertRaises(ValueError):scope.raw('api.DeleteObject',input={'Bucket':'other','Key':'sentinel'})
            original=self.worker.call
            def corrupt(request):
                result=original(request)
                if request['op']=='list' and result.get('ok'):result['keys'].append('sentinel')
                return result
            self.worker.call=corrupt
            with self.assertRaisesRegex(RuntimeError,'outside object scope'):scope.cleanup_objects()
            self.worker.call=original
        self.assert_sentinel()
        self.assertFalse(any(r['op']=='delete' and r.get('key')=='sentinel' for r in self.requests))

    def test_same_scope_copy_and_known_opaque_keys_use_real_producer_bytes(self):
        """Copy a genuine special-key object through the SDK then read it; expect exact bytes and prefix-confined source/destination."""
        with BucketScope(self.probe()) as scope:
            scope.put('source ?+% 中文',b'actual independent source')
            scope.call('copy',key='destination',source_key='source ?+% 中文')
            scope.call('get',key='destination',output_path=str(self.ctx.output))
            self.assertEqual(self.ctx.output.read_bytes(),b'actual independent source')
            request=next(r for r in self.requests if r['op']=='copy')
            self.assertEqual(request['source_key'],scope.key('source ?+% 中文'))
            self.assertEqual(request['key'],scope.key('destination'))
        self.assert_sentinel()

    def test_bucket_contract_is_not_run_before_io_when_management_is_disabled(self):
        """Run a genuine bucket lifecycle case under object-only authorization; expect explicit not_run and no bucket request."""
        from osv.ceph_buckets import CASES
        rows=run_cases(self.ctx,[c for c in CASES if c.name=='ceph_bucket_create_delete'])
        self.assertEqual(rows[0]['status'],'not_run')
        self.assertIn('bucket management',rows[0]['missing_requirements'][0])
        self.assertEqual(self.requests,[])

    def test_long_key_and_configuration_validation_boundaries(self):
        """Use an exact 1024-byte source key; expect honest unexecuted boundary instead of altered service semantics."""
        from osv.suites import CaseNotRun
        scope=BucketScope(self.probe())
        with self.assertRaises(CaseNotRun):scope.key('x'*1024)
        self.assertTrue(scope.key('x'*1025).endswith('x'*1025))
        self.assertEqual(scope.key(scope.prefix+'kept'),scope.prefix+'kept')

    def test_nested_bucket_mutation_and_typed_key_escapes_are_rejected_before_io(self):
        """Provide nested versioning/typed bucket changes and SDK bucket-deleting callbacks; expect refusal without transmitting them."""
        from osv.suites import CaseNotRun
        with BucketScope(self.probe()) as scope:
            for request in ({'op':'versioning_put','key':'ignored','status':'Enabled'},
                            {'op':'api.PutBucketPolicy','input':{'Policy':'{}'}},
                            {'op':'atomic_stream','mode':'bucket_gone','key':'object'}):
                count=len(self.requests)
                with self.subTest(request=request),self.assertRaises(CaseNotRun):
                    scope.raw('concurrent_requests',requests=[request])
                self.assertEqual(len(self.requests),count)
            with self.assertRaises(ValueError):
                scope.raw('concurrent_requests',requests=[{'op':'api.DeleteObject','input':{'Bucket':'other','Key':'sentinel'}}])
        self.assert_sentinel()

    def test_lost_write_and_multipart_create_responses_still_clean_only_real_owned_resources(self):
        """Lose actual successful producer responses; expect original errors retained and genuine listed upload IDs consumed by cleanup, preserving another upload."""
        for op in ('put','mp_create'):
            with self.subTest(op=op):
                outside=self.original({'op':'mp_create','key':'outside-upload'})['upload_id']
                scope=BucketScope(self.probe())
                observe=self.worker.call
                def lose(request):
                    result=observe(request)
                    if request['op']==op and result.get('ok'):
                        return {'ok':False,'error_kind':'sdk_error','error':'producer response lost'}
                    return result
                self.worker.call=lose
                try:
                    with self.assertRaisesRegex(RuntimeError,'producer response lost'):
                        with scope:
                            if op=='put':scope.put('real',b'actual producer data')
                            else:scope.call('mp_create',key='real')
                    self.assertTrue(scope.cleanup_completed)
                    uploads=self.original({'op':'mp_list','prefix':'outside-upload'})['uploads']
                    self.assertEqual([row['upload_id'] for row in uploads],[outside])
                    self.assertEqual(self.original({'op':'mp_list','prefix':scope.prefix})['uploads'],[])
                finally:
                    self.worker.call=observe
                    self.original({'op':'mp_abort','key':'outside-upload','upload_id':outside})
        self.assert_sentinel()
