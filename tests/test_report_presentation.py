"""Verify human reports use functional descriptions while retaining genuine test outcomes."""
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from osv.cli import load_config, run
from osv.report import markdown
from test_harness import config


class ReportPresentationTest(unittest.TestCase):
    """Read actual worker-to-report output rather than reconstructing result generation in tests."""

    def test_functional_report_keeps_verdicts_without_upstream_branding(self):
        """Purpose: run genuine normal object IO; expect readable Chinese cases, unchanged 23-pass/one-warning evidence and the actual error code without upstream names, IDs or source links in Markdown."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory)
            data["workload"].update(mode="functional",profile="normal")
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            original=copy.deepcopy(report);text=markdown(report)
            self.assertEqual(report,original)
            self.assertEqual(sum(r["status"]=="pass" for r in report["cases"]),23)
            self.assertEqual(sum(r["status"]=="warning" for r in report["cases"]),1)
            self.assertEqual(text,(directory/"results/report.md").read_text())
            self.assertIsNone(re.search(r"juicefs|ceph|objbench",text,re.IGNORECASE))
            self.assertNotIn("github.com",text);self.assertNotIn("上游能力对齐",text)
            self.assertIn("Range 起点越界处理 | warning",text)
            self.assertIn("错误码 InvalidRange",text)
            self.assertIn("普通对象读写与元数据 | pass",text)
            self.assertIn("| local | 24 | 23 | 0 | 1 | 0 |",text)
            self.assertNotIn("source_matches",text)
            warning=next(r for r in report["cases"] if r["status"]=="warning")
            self.assertFalse(warning["evidence"]["source_matches"])
            self.assertEqual(warning["evidence"]["observed"]["error_code"],"InvalidRange")

    def test_performance_report_retains_measurements_without_source_model_names(self):
        """Purpose: execute the real multi-object workload; expect exact measured successes and performance dataset descriptions without upstream model branding or fabricated functional results."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory)
            data["workload"].update(mode="performance",performance_model="objbench",rounds=1,
                objbench={"small_objects":2,"small_object_size":5,"big_object_size":17,"block_size":8,"list_requests_per_lane":1})
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results");text=(directory/"results/report.md").read_text()
            self.assertEqual(report["cases"],[])
            self.assertTrue(report["samples"]);self.assertTrue(all(s["ok"] for s in report["samples"]))
            self.assertIsNone(re.search(r"juicefs|ceph|objbench",text,re.IGNORECASE))
            self.assertIn("使用多对象与拆块数据集",text)
            self.assertIn("本次未执行功能用例",text)
            for row in report["summary"]:
                self.assertIn(str(row["successes"])+"/"+str(row["attempts"]),text)
