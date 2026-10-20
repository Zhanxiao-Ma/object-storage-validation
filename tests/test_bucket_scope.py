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

    def test_original_written_keys_survive_lossy_listing_and_lost_write_response(self):
        """Write opaque keys through real storage then trim only observed list values or lose a successful PUT response; expect original keys deleted and cleanup confirmed."""
        for lost_response in (False,True):
            with self.subTest(lost_response=lost_response),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory);original=worker.call;deleted=[]
                def observe(request):
                    result=original(request)
                    if request["op"]=="list" and result["ok"]:result["keys"]=[key.strip() for key in result["keys"]]
                    if request["op"]=="delete":deleted.append(request["key"])
                    if lost_response and request["op"]=="put" and result["ok"]:return {"ok":False,"error_kind":"sdk_error","error":"write response lost"}
                    return result
                worker.call=observe;scope=BucketScope(p)
                try:
                    with self.assertRaisesRegex((AssertionError,RuntimeError),"write response lost" if lost_response else "original failure"):
                        with scope as bucket:
                            for key in (" ","\t","\n","\r\n"," real "):bucket.put(key,b"true-producer-value")
                            raise AssertionError("original failure")
                    self.assertTrue(scope.cleanup_completed)
                    self.assertEqual(set(deleted),{" "} if lost_response else {" ","\t","\n","\r\n"," real "})
                    self.assertEqual(original({"op":"bucket_head","bucket":scope.name})["status_code"],404)
                    self.assertEqual([file for file in (directory/"objects").rglob("*") if file.is_file()],[])
                finally:worker.close()

    def test_cleanup_uses_final_bucket_absence_after_ambiguous_delete_or_list(self):
        """Lose a real deletion response or fail listing after known-key cleanup; expect final actual bucket deletion/HEAD to establish cleanup without relaxing object-case assertions."""
        for broken in ("delete_response","list_response"):
            with self.subTest(broken=broken),tempfile.TemporaryDirectory() as temporary:
                directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory);original=worker.call
                def observe(request):
                    result=original(request)
                    if (request["op"]=="delete" and broken=="delete_response") or (request["op"]=="list" and broken=="list_response"):
                        return {"ok":False,"error_kind":"sdk_error","status_code":500,"error":"response lost after real operation"}
                    return result
                worker.call=observe;scope=BucketScope(p)
                try:
                    with scope as bucket:bucket.put("opaque-key",b"actual-producer")
                    self.assertTrue(scope.cleanup_completed)
                    self.assertEqual(original({"op":"bucket_head","bucket":scope.name})["status_code"],404)
                finally:worker.close()

    def test_persistent_delete_denial_cannot_be_reported_as_clean(self):
        """Deny both actual cleanup delete attempts while keeping real producer bytes; expect nonempty-bucket failure and cleanup_completed false."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=self.worker(directory);p=self.probe(worker,directory);original=worker.call
            def deny(request):
                if request["op"]=="delete":return {"ok":False,"error_kind":"sdk_error","status_code":403,"error":"independent deletion denial"}
                return original(request)
            worker.call=deny;scope=BucketScope(p)
            try:
                with self.assertRaisesRegex(RuntimeError,"isolated bucket cleanup failed.*independent deletion denial"):
                    with scope as bucket:bucket.put("still-present",b"actual-producer")
                self.assertFalse(scope.cleanup_completed)
                self.assertTrue(original({"op":"get","bucket":scope.name,"key":"still-present","output_path":str(p.ctx.output)})["ok"])
                self.assertEqual(p.ctx.output.read_bytes(),b"actual-producer")
                self.assertTrue(original({"op":"delete","bucket":scope.name,"key":"still-present"})["ok"])
                self.assertTrue(original({"op":"bucket_delete","bucket":scope.name})["ok"])
            finally:worker.close()
