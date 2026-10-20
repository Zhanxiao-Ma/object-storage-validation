"""Verify pinned upstream inventory extraction and honest coverage accounting."""
import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from osv.alignment import audit,inventory
from osv.cli import load_config
from osv.scenarios import build_registry
from scripts.upstream_catalog import extract_ceph,extract_juice
from test_harness import config


class AlignmentTest(unittest.TestCase):
    """Audit actual source identities and never treat a local name/reference as full Ceph equivalence."""
    def test_pinned_inventory_has_every_file_and_unique_case(self):
        """Read the shipped real upstream inventory; expect all eight files, 979 unique test functions and both pinned revisions."""
        data=inventory()
        self.assertEqual(data["juicefs"]["revision"],"29c60bbbeea410c6c0cb7a49540044e0c099dffb")
        self.assertEqual(data["ceph"]["revision"],"5b3987116eca0b4b8fa57d6b10f3db57bab196b6")
        self.assertEqual(len(data["juicefs"]["cases"]),26)
        self.assertEqual(len(data["ceph"]["files"]),8)
        self.assertEqual(len(data["ceph"]["cases"]),979)
        self.assertEqual(len({row["id"] for row in data["ceph"]["cases"]}),979)
        self.assertTrue(all(len(row["sha256"])==64 for row in data["ceph"]["cases"]))
        self.assertEqual(sum(row["active_definition"] for row in data["ceph"]["cases"]),975)
        self.assertTrue(any(row["parameters"] for row in data["ceph"]["cases"]))

    def test_ast_extraction_retains_nested_identity_parameters_and_assertions(self):
        """Feed raw Python source to the real extractor; expect class-scoped names, parameter decorators and exact assertion counts."""
        source="@pytest.mark.parametrize('n',[1,2])\ndef test_a(n):\n assert n>0\nclass TestB:\n def test_a(self):\n  assert True\n  assert 1==1\n"
        rows=extract_ceph(source,"test_raw.py","fixed-revision")
        self.assertEqual([row["id"] for row in rows],["test_raw.py::test_a@L2","test_raw.py::TestB.test_a@L5"])
        self.assertEqual([row["assertions"] for row in rows],[1,2])
        self.assertIn("parametrize",rows[0]["parameters"][0])
        self.assertNotEqual(rows[0]["sha256"],rows[1]["sha256"])
        self.assertIn("parametrize",rows[0]["markers"][0])

    def test_upstream_applicability_markers_survive_extraction(self):
        """Feed AWS/RGW-specific decorators to the production extractor; expect all applicability conditions retained with source identity."""
        source="@pytest.mark.fails_on_aws\n@pytest.mark.bucket_logging\n@pytest.mark.skipif(True, reason='needs service')\ndef test_scoped():\n assert True\n"
        row=extract_ceph(source,"test_scope.py","revision")[0]
        self.assertEqual(row["markers"],["pytest.mark.fails_on_aws","pytest.mark.bucket_logging","pytest.mark.skipif(True, reason='needs service')"])
        self.assertEqual(row["parameters"],[])

    def test_reference_matches_stay_partial_and_unmapped_items_remain_visible(self):
        """Execute production audit for normal/complete selections; expect no invented Ceph alignment or dropped upstream gaps."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            for profile in ("normal","complete"):
                data=config(directory);data["workload"].update(profile=profile,performance_model="objbench")
                path.write_text(json.dumps(data));cfg=load_config(path);result=audit(cfg,build_registry(cfg))
                juice,ceph=result["summary"]
                self.assertEqual((juice["aligned"],juice["not_applicable"],juice["complete"]),(20,6,True))
                self.assertEqual(ceph["aligned"],208 if profile=="complete" else 0)
                self.assertFalse(ceph["complete"])
                self.assertGreater(ceph["not_migrated"],0)
                self.assertEqual(len(result["cases"]),1005)

    def test_reviewed_alignment_requires_exact_upstream_fingerprint(self):
        """Change one source fingerprint fed to the real auditor; expect its reviewed row to fall back to partial rather than retaining aligned."""
        data=inventory()
        target=next(row for row in data["ceph"]["cases"] if row["name"]=="test_object_set_get_metadata_none_to_good")
        target["sha256"]="0"*64
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);path=directory/"manifest.json"
            cfg=config(directory);cfg["workload"]["profile"]="complete";path.write_text(json.dumps(cfg))
            cfg=load_config(path)
            with patch("osv.alignment.inventory",return_value=data):result=audit(cfg,build_registry(cfg))
        row=next(row for row in result["cases"] if row["id"]==target["id"])
        self.assertEqual(row["status"],"partial")
        self.assertIsNone(row["reviewed_implementation"])
        self.assertEqual(row["implementations"],["ceph_metadata_value"])

    def test_shadowed_upstream_names_are_not_double_counted_as_executable(self):
        """Extract repeated function names; expect unique source identities and only the last definition active in each scope."""
        source="def test_same():\n assert False\ndef test_same():\n assert True\nclass TestGroup:\n def test_same(self):\n  assert True\n"
        rows=extract_ceph(source,"test_duplicates.py","revision")
        self.assertEqual(len({row["id"] for row in rows}),3)
        self.assertEqual([row["active_definition"] for row in rows],[False,True,True])
