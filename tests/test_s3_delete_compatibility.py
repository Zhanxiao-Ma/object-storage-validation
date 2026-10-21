"""Run real filesystem producers through the new generic deletion scenarios."""
from pathlib import Path
import sys
import tempfile
import unittest

from osv.scenarios import Context, build_registry
from osv.suites import run_cases
from osv.worker import Worker
from osv.s3_delete_compatibility import CASES

ROOT = Path(__file__).resolve().parents[1]


class DeleteCompatibilityTest(unittest.TestCase):
    def test_quiet_verbose_missing_special_and_repeat_use_real_storage(self):
        """Run quiet/verbose requests against actual stored files; expect independent HEAD/LIST absence and exact acknowledgments after first and repeated deletion."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            worker = Worker({"command": [sys.executable, str(ROOT / "drivers/reference.py")],
                "env": {"OSV_REFERENCE_ROOT": str(directory / "objects")}}, 10)
            try:
                ctx = Context(worker, "unused/", directory, b"independent", directory / "body", directory / "output", {})
                rows = run_cases(ctx, CASES[:2])
                self.assertEqual([r["status"] for r in rows], ["pass", "pass"], rows)
                self.assertFalse([p for p in (directory / "objects").rglob("*") if p.is_file()])
            finally:
                worker.close()

    def test_full_profile_contains_helper_contracts_without_vendor_operations(self):
        """Inspect the production registry; expect all six helper-derived standard S3 cases in complete, ordinary quiet/verbose cases in normal and requiring no Ceph-specific wire operations."""
        registry = build_registry({})
        names = lambda profile: {r["case"] for name in registry.profiles[profile].suites for r in registry.suites[name].catalog}
        self.assertTrue({c.name for c in CASES} <= names("complete"))
        self.assertEqual({c.name for c in CASES} & names("normal"), {"s3_delete_many_quiet", "s3_delete_many_verbose"})
        self.assertFalse(any("ceph" in op for c in CASES for op in c.operations))
