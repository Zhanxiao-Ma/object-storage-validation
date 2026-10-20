"""Keep different upstream oracles independent when they exercise the same boundary class."""
from pathlib import Path
import sys
import tempfile
import unittest

from osv.objbench_cases import CASES as JUICE_CASES
from osv.ceph_objects import CASES as CEPH_CASES
from osv.scenarios import Context
from osv.suites import run_cases
from osv.worker import Worker

ROOT=Path(__file__).resolve().parents[1]


class ConflictTest(unittest.TestCase):
    """Execute actual stored IO through production scenarios and assert each source's own verdict."""
    def test_range_rejection_is_not_rewritten_to_match_both_sources(self):
        """Feed real reference Range rejection into both production cases; expect a JuiceFS preference mismatch and a strict Ceph pass."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            worker=Worker({"language":"python","api":"local","command":[sys.executable,str(ROOT/"drivers/reference.py")],
                           "env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            try:
                ctx=Context(worker,"conflicts/",directory,b"independent-data",directory/"body",directory/"output",{})
                selected=[next(c for c in JUICE_CASES if c.name=="juicefs_range_start_overflow"),
                          next(c for c in CEPH_CASES if c.name=="ceph_range_invalid_start")]
                rows=run_cases(ctx,selected)
                self.assertEqual([row["status"] for row in rows],["warning","pass"])
                self.assertFalse(rows[0]["evidence"]["source_matches"])
                self.assertEqual(rows[0]["evidence"]["source_severity"],"warning")
                self.assertEqual(rows[0]["evidence"]["observed"]["error_code"],"InvalidRange")
                self.assertIn("empty",rows[0]["evidence"]["source_expected"])
            finally:worker.close()

    def test_source_warning_and_strict_error_keep_distinct_verdicts(self):
        """Purpose: corrupt an error code after actual Range IO; expect strict Ceph duplicates to fail while a matching JuiceFS clipped read passes with source warning severity retained."""
        from osv.suites import CORE, EXTENDED
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            worker=Worker({"language":"python","api":"local","command":[sys.executable,str(ROOT/"drivers/reference.py")],
                           "env":{"OSV_REFERENCE_ROOT":str(directory/"objects")}},10)
            actual_call=worker.call
            def corrupt(request):
                result=actual_call(request)
                if request["op"]=="get" and not result["ok"]:result["error_code"]="WrongRangeCode"
                return result
            try:
                ctx=Context(worker,"source-levels/",directory,b"independent-data",directory/"body",directory/"output",{})
                worker.call=corrupt
                selected=[next(c for c in EXTENDED if c.name==name) for name in ("range_start_overflow","range_empty_object")]
                rows=run_cases(ctx,selected)
                self.assertEqual([row["status"] for row in rows],["fail","fail"])
                row=run_cases(ctx,(next(c for c in CORE if c.name=="range_end_overflow"),))[0]
                self.assertEqual(row["status"],"pass");self.assertEqual(row["severity"],"warning")
                self.assertTrue(row["evidence"]["source_matches"])
            finally:worker.close()
