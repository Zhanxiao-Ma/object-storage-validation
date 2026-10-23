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
        """Purpose: run genuine normal object IO; expect readable Chinese cases, 94 passes, one warning and one explicit unsupported lifecycle case, early warning/skip sections, folded complete results and the actual error code without upstream names, IDs or source links in Markdown."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory)
            data["workload"].update(mode="functional",profile="normal")
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results")
            original=copy.deepcopy(report);text=markdown(report)
            self.assertEqual(report,original)
            self.assertEqual(sum(r["status"]=="pass" for r in report["cases"]),94)
            self.assertEqual(sum(r["status"]=="warning" for r in report["cases"]),1)
            self.assertEqual(text,(directory/"results/report.md").read_text())
            self.assertIsNone(re.search(r"juicefs|ceph|objbench",text,re.IGNORECASE))
            self.assertNotIn("github.com",text);self.assertNotIn("上游能力对齐",text)
            self.assertIn("Range 起点越界处理 | warning",text)
            self.assertIn("错误码 InvalidRange",text)
            self.assertIn("普通对象读写与元数据 | pass",text)
            self.assertIn("| local | 96 | 94 | 0 | 1 | 1 |",text)
            self.assertNotIn("source_matches",text)
            self.assertLess(text.index("## 失败与警告"), text.index("<details>"))
            self.assertLess(text.index("## 未执行原因"), text.index("<details>"))
            self.assertIn("<summary>完整功能明细（96 条结果）</summary>", text)
            self.assertIn("</details>", text)
            self.assertIn("驱动未提供操作：api.DeleteBucketLifecycle", text)
            self.assertIn("未完成分块生命周期配置", text)
            self.assertNotIn("加密、生命周期等高级场景", text)
            self.assertNotIn("{'isolated_bucket':", text)
            self.assertNotIn("Verified MiB/s", text)
            self.assertNotIn("## 外部工具", text)
            warning=next(r for r in report["cases"] if r["status"]=="warning")
            self.assertFalse(warning["evidence"]["source_matches"])
            self.assertEqual(warning["evidence"]["observed"]["error_code"],"InvalidRange")

    def test_performance_report_retains_measurements_without_source_model_names(self):
        """Purpose: execute the real atomic file workload; expect exact measured successes and independent operation descriptions without upstream model branding or fabricated functional results."""
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);data=config(directory)
            data["workload"].update(mode="performance",rounds=1,
                objbench={"small_objects":2,"small_object_size":5,"big_object_size":17,"block_size":8,"list_requests_per_lane":1})
            path=directory/"manifest.json";path.write_text(json.dumps(data))
            report=run(load_config(path),directory/"results");text=(directory/"results/report.md").read_text()
            self.assertEqual(report["cases"],[])
            self.assertTrue(report["samples"]);self.assertTrue(all(s["ok"] for s in report["samples"]))
            self.assertIsNone(re.search(r"juicefs|ceph|objbench",text,re.IGNORECASE))
            self.assertIn("独立测量 HEAD",text)
            self.assertIn("本次未执行功能用例",text)
            for row in report["summary"]:
                self.assertIn(str(row["successes"])+"/"+str(row["attempts"]),text)

    def test_startup_failure_remains_visible_before_folded_results(self):
        """Purpose: fail a real worker startup; expect the execution error and failed setup before folded details, a grouped reason for 96 planned but unreported cases, no pass claims and unchanged sparse JSON evidence."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            data = config(directory)
            data["workload"].update(mode="functional", profile="normal")
            data["targets"][0]["command"] = ["/does-not-exist/osv-worker"]
            path = directory/"manifest.json"
            path.write_text(json.dumps(data))
            report = run(load_config(path), directory/"results")
            original = copy.deepcopy(report)
            text = markdown(report)
            self.assertEqual(report, original)
            self.assertEqual(len(report["coverage"]["catalog"]), 96)
            self.assertEqual(len(report["cases"]), 1)
            self.assertEqual(sum(row["status"] == "pass" for row in report["cases"]), 0)
            self.assertLess(text.index("### 执行错误"), text.index("## 驱动配置"))
            self.assertLess(text.index("## 失败与警告"), text.index("<details>"))
            self.assertIn("初始化 | fail", text)
            self.assertIn("| local | 96 | 0 | 0 | 0 | 96 |", text)
            reasons = text.split("## 未执行原因", 1)[1].split("## 兼容性", 1)[0]
            self.assertIn("| local | 96 |", reasons)
            self.assertIn("未产生用例结果", reasons)
            self.assertIn("/does-not-exist/osv-worker", reasons)
