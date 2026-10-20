"""Verify migrated Ceph tags through actual local storage and production case dispatch."""
from pathlib import Path
import sys
import tempfile
import unittest
from osv.ceph_tags import CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker
ROOT=Path(__file__).resolve().parents[1]

class TagsTest(unittest.TestCase):
    """Exercise all boundary/overwrite/delete tag cases using real persistent producer-consumer state."""
    def test_tag_source_vectors_have_independent_final_assertions(self):
        """Run every translated source vector against real reference files; expect eight passes and preserved seven-MiB object content."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            worker=Worker({"language":"python","api":"local","command":[sys.executable,str(ROOT/"drivers/reference.py")],"env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},20)
            try:
                ctx=Context(worker,"tags/",directory,b"independent",directory/"body",directory/"output",{})
                rows=run_cases(ctx,CASES)
                self.assertEqual([row["status"] for row in rows],["pass"]*8,rows)
                objects=list((directory/"objects/.objects").glob("*.json"))
                self.assertEqual(len(objects),8)
                for file in objects:self.assertEqual(file.with_suffix("").read_bytes(),b"A"*(7*1024*1024))
            finally:worker.close()
