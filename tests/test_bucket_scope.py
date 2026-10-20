"""Use actual scoped filesystem operations to verify fresh-bucket ownership, routing and cleanup boundaries."""
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from osv.bucket_scope import BucketScope
from osv.scenarios import Context
from osv.suites import Probe
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class BucketScopeTest(unittest.TestCase):
    """Prevent a new bucket-level test from reusing arbitrary state or hiding cleanup failure behind its primary assertion."""
    def worker(self,directory):
        return Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)

    def probe(self,worker,directory):
        return Probe(Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{}))

    def test_failed_case_cleans_its_bucket_and_preserves_default_bucket_data(self):
        """Write different bytes under the same key in default and scoped buckets, then fail; expect isolated reads and actual bucket deletion."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory)
            try:
                p.ctx.body.write_bytes(b"original-default");worker.call({"op":"put","key":"foo","body_path":str(p.ctx.body)})
                scope=BucketScope(p)
                with self.assertRaisesRegex(AssertionError,"deliberate case failure"):
                    with scope as bucket:
                        bucket.put("foo",b"scoped-value")
                        bucket.call("get",key="foo",output_path=str(p.ctx.output));self.assertEqual(p.ctx.output.read_bytes(),b"scoped-value")
                        worker.call({"op":"get","key":"foo","output_path":str(p.ctx.output)});self.assertEqual(p.ctx.output.read_bytes(),b"original-default")
                        raise AssertionError("deliberate case failure")
                self.assertTrue(scope.cleanup_completed)
                result=worker.call({"op":"bucket_head","bucket":scope.name});self.assertFalse(result["ok"]);self.assertEqual(result["status_code"],404)
                result=worker.call({"op":"get","key":"foo","output_path":str(p.ctx.output)});self.assertTrue(result["ok"]);self.assertEqual(p.ctx.output.read_bytes(),b"original-default")
            finally:worker.close()

    def test_existing_generated_name_is_not_reused_or_cleaned(self):
        """Provide a real already-existing bucket in the generated namespace; expect refusal before case entry and preservation of its object."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory);name="osv-"+uuid.uuid4().hex
            try:
                self.assertTrue(worker.call({"op":"bucket_create_raw","bucket":name})["ok"])
                p.ctx.body.write_bytes(b"sentinel");worker.call({"op":"put","bucket":name,"key":"foo","body_path":str(p.ctx.body)})
                with self.assertRaisesRegex(RuntimeError,"refusing reuse"):
                    with BucketScope(p,name):self.fail("existing bucket was reused")
                result=worker.call({"op":"get","bucket":name,"key":"foo","output_path":str(p.ctx.output)})
                self.assertTrue(result["ok"]);self.assertEqual(p.ctx.output.read_bytes(),b"sentinel")
            finally:worker.close()

    def test_scope_rejects_invalid_names_and_bucket_escape(self):
        """Supply invalid namespaces and an explicit bucket override inside an owned scope; expect rejection and complete cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory)
            try:
                for name in ("",False,5,[],"production","osv-../unsafe","osv-","osv-"+"f"*31,"osv-"+"f"*33,"osv-"+"F"*32):
                    with self.subTest(name=name),self.assertRaises(ValueError):BucketScope(p,name)
                with BucketScope(p) as bucket:
                    with self.assertRaisesRegex(ValueError,"cannot override"):bucket.raw("bucket_delete",bucket="production")
                self.assertEqual(worker.call({"op":"bucket_head","bucket":bucket.name})["status_code"],404)
            finally:worker.close()

    def test_dead_worker_reports_original_failure_and_unfinished_cleanup(self):
        """Kill a real worker after storing data, then fail the case; expect the original assertion plus the specific bucket cleanup diagnostic."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory);scope=BucketScope(p)
            try:
                with self.assertRaisesRegex(RuntimeError,"original failure; isolated bucket cleanup failed for "+scope.name):
                    with scope as bucket:
                        bucket.put("foo",b"actual-data")
                        worker.process.kill();worker.process.wait(timeout=5)
                        raise AssertionError("original failure")
            finally:worker.close()
