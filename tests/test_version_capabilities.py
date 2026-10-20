"""Keep filesystem harness coverage separate from actual SDK versioning evidence."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_versioning import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class VersionCapabilitiesTest(unittest.TestCase):
    """Do not emulate versioning or turn a local filesystem model into S3 compatibility evidence."""
    def test_local_reference_leaves_all_version_cases_not_run_before_bucket_io(self):
        """Select all 21 real SDK version cases with the actual reference handshake; expect explicit missing operations and zero bucket/object calls."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);worker=Worker({"command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            original=worker.call;calls=[]
            def observe(request):calls.append(request);return original(request)
            worker.call=observe
            try:
                ctx=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                rows=run_cases(ctx,CASES)
                self.assertEqual(len(rows),21);self.assertEqual(calls,[])
                self.assertTrue(all(row["status"]=="not_run" and row["reason"]=="unsupported_operations" and row["gate"] for row in rows))
                self.assertTrue(all("versions_list" in row["missing_operations"] and "delete_version" in row["missing_operations"] for row in rows))
                self.assertEqual(list((directory/"objects").iterdir()),[])
            finally:worker.close()
