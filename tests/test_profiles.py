"""Verify real case execution, reusable profiles and explicit coverage through production reports."""
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from osv.cli import load_config, run
from osv.scenarios import Profile, Suite, build_registry
from test_harness import config


class ProfileTest(unittest.TestCase):
    """Drive production scenarios with actual filesystem IO; never replace SDK results with invented passes."""

    def manifest(self, directory, overrides=None):
        data = config(directory)
        data["workload"].update(requests=1, rounds=1, concurrency=1, warmup_requests=0)
        data["workload"].update(overrides or {})
        path = directory/"manifest.json"
        path.write_text(json.dumps(data))
        return path

    def test_both_profiles_run_real_producer_consumer_and_report(self):
        """Run real local IO and complete CLI; expect 24/825 rows, unsupported advanced APIs not_run, exact source URI diagnostic, blocked metrics/exit 1 and clean actual files."""
        for profile, count, observations,not_run in (("normal", 24, 2,0), ("complete", 825, 4,571)):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                manifest=self.manifest(directory,{"profile":profile})
                if profile=="complete":
                    result=subprocess.run([sys.executable,"-m","osv","run",str(manifest),"--output",str(directory/"results")],capture_output=True,text=True,timeout=90)
                    self.assertEqual(result.returncode,1,result.stdout+result.stderr)
                    report=json.loads((directory/"results/report.json").read_text())
                    self.assertEqual(report["samples"],[])
                else:report=run(load_config(manifest),directory/"results")
                self.assertEqual(report["coverage"]["profile"], profile)
                self.assertEqual(len(report["coverage"]["catalog"]), count)
                rows = [c for c in report["cases"] if c.get("suite")]
                self.assertEqual(len(rows), count)
                self.assertEqual(sum(c["status"] == "observation" for c in rows), observations)
                failures=[c for c in rows if c["status"]=="fail"]
                self.assertEqual([c["case"] for c in failures],["ceph_tail_unreadable"] if profile=="complete" else [],failures)
                unsupported=[c for c in rows if c["status"]=="not_run"]
                self.assertEqual(len(unsupported),not_run)
                self.assertTrue(all(c["case"].startswith(("ceph_version_","ceph_config_","ceph_sse_c_","ceph_public_block_","ceph_ownership_","ceph_lock_config_","ceph_sse_multipart_","ceph_encryption_","ceph_policy_","ceph_acl_","ceph_inventory_","ceph_header_","ceph_put_condition_","ceph_copy_condition_","ceph_access_","ceph_interop_","ceph_metadata_edges_","ceph_attributes_","ceph_checksum_","ceph_conditional_versions_","ceph_public_","ceph_copy_contract_","ceph_state_","ceph_basic_","ceph_remaining_policy_","ceph_logging_","ceph_raw_","ceph_query_","ceph_lc_","ceph_object_lock_","ceph_names_","ceph_cloud_lc_","ceph_tail_","ceph_encrypted_form_","ceph_encrypted_copy_","ceph_encrypted_part_copy_","ceph_presigned_","ceph_atomic_","ceph_iam_")) and ((c["reason"]=="unsupported_operations" and c["missing_operations"]) or (c["case"].startswith("ceph_raw_") and c["reason"]=="missing_prerequisites" and c["missing_requirements"]==["workload.ceph_http"])) for c in unsupported))
                self.assertTrue(all(s["ok"] for s in report["samples"]))
                self.assertEqual([p for p in (directory/"objects").rglob("*") if p.is_file()], [])
                self.assertTrue(report["coverage"]["exclusions"])
                self.assertEqual(json.loads((directory/"results/report.json").read_text())["coverage"], report["coverage"])
                markdown = (directory/"results/report.md").read_text()
                self.assertIn(f"| local | {count} | {count-observations-not_run-len(failures)} | {len(failures)} | {observations} | {not_run} |", markdown)
                self.assertIn("不在本档位覆盖范围", markdown)
                if profile == "complete":
                    self.assertIn("multipart_resend", markdown)
                    self.assertIn("test_multipart_upload_resend_part", markdown)

    def test_default_custom_override_and_invalid_selections(self):
        """Resolve default normal, legacy custom suites and CLI override; reject ambiguous or unbounded profiles before IO."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertEqual(load_config(self.manifest(directory))["workload"]["profile"], "normal")
            custom = self.manifest(directory, {"compatibility_suites": ["baseline"]})
            self.assertNotIn("profile", load_config(custom)["workload"])
            self.assertEqual(load_config(custom, "complete")["workload"]["compatibility_suites"], ["core_objects", "objbench_objects", "extended_objects", "ceph_object_boundaries", "ceph_conditions", "ceph_metadata", "ceph_copy", "ceph_http_range", "ceph_tags", "ceph_headers", "ceph_buckets", "ceph_exact_listing", "ceph_delete_many", "ceph_exact_multipart", "ceph_multipart_copy", "ceph_get_part","ceph_versioning","ceph_bucket_config","ceph_sse_customer","ceph_bucket_controls","ceph_sse_multipart","ceph_encryption_objects","ceph_bucket_policy","ceph_acl","ceph_bucket_inventory","ceph_request_headers","ceph_conditional_writes","ceph_access","ceph_object_interop","ceph_policy_conditions","ceph_metadata_edges","ceph_checksum_attributes","ceph_conditional_versions","ceph_public_controls","ceph_copy_contracts","ceph_state_boundaries","ceph_basic_boundaries","ceph_policy_remaining","ceph_logging_controls","ceph_raw_http","ceph_query_edges","ceph_lifecycle_expiration","ceph_lifecycle_transition","ceph_lifecycle_marker_expiration","ceph_object_locks","ceph_logging_records","ceph_bucket_names","ceph_cloud_lifecycle","ceph_logging_workloads","ceph_logging_extensions","ceph_metadata_tail","ceph_encrypted_forms","ceph_encryption_tail","ceph_logging_policy_transitions","ceph_logging_prefixes","ceph_encrypted_copy_matrix","ceph_logging_tenants","ceph_logging_parallel","ceph_presigned_urls","ceph_atomic_streams","ceph_iam_accounts","ceph_iam_inline"])
            for overrides in ({"profile":"unknown"}, {"profile":[]},
                              {"profile":"normal", "compatibility_suites":["baseline"]},
                              {"list_objects":1000}, {"list_objects":10001}, {"list_objects":True},
                              {"kms_key_id":[]},{"kms_key_id":False},{"storage_classes":"STANDARD"},{"storage_classes":[None]},{"storage_classes":[""]}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    load_config(self.manifest(directory, overrides))

    def test_missing_worker_reports_planned_cases_as_not_run(self):
        """Fail actual process startup; expect all 825 planned cases reported as not run rather than compatibility passes."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self.manifest(directory, {"profile":"complete"})
            data = json.loads(path.read_text())
            data["targets"][0]["command"] = ["/does-not-exist/osv-worker"]
            path.write_text(json.dumps(data))
            report = run(load_config(path), directory/"results")
            self.assertEqual(report["samples"], [])
            self.assertEqual(report["cases"][0]["status"], "fail")
            self.assertIn("| local | 825 | 0 | 0 | 0 | 825 |", (directory/"results/report.md").read_text())

    def test_plugin_profile_reuses_suite_without_language_changes(self):
        """Register another profile reusing a real suite; expect execution and reporting without a scheduler language branch."""
        registry = build_registry({})
        registry.add_profile(Profile("smoke", ("core_objects",), "reused object checks"))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.add_profile(Profile("smoke", ("core_objects",), "duplicate"))
        with self.assertRaisesRegex(ValueError, "unknown"):
            registry.add_profile(Profile("broken", ("does_not_exist",), "bad suite"))
        with self.assertRaisesRegex(ValueError, "unique"):
            registry.add_profile(Profile("repeated", ("core_objects", "core_objects"), "duplicates"))
        with tempfile.TemporaryDirectory() as temp, patch("osv.cli.build_registry", return_value=registry):
            directory = Path(temp)
            report = run(load_config(self.manifest(directory, {"profile":"smoke"})), directory/"results")
            self.assertEqual(report["coverage"]["profile"], "smoke")
            self.assertFalse([c for c in report["cases"] if c["status"] == "fail"])

    def test_catalog_cannot_silently_drop_duplicate_or_downgrade_cases(self):
        """Feed malformed suite output to the real runner; missing, duplicate or downgraded gates must fail before measurement."""
        for results in ([{"case":"other", "status":"pass"}],
                        [{"case":"required", "status":"pass"}]*2,
                        [{"case":"required", "status":"observation"}],
                        [{"case":"required", "status":"not_run", "gate":False,"reason":"unsupported_operations"}],
                        [{"case":"required", "status":"not_run"}]):
            registry = build_registry({})
            registry.add_suite(Suite("broken", frozenset({"head"}), lambda ctx, results=results: results,
                                     ({**registry.suites["core_objects"].catalog[0], "case":"required"},)))
            with self.subTest(results=results), tempfile.TemporaryDirectory() as temp, patch("osv.cli.build_registry", return_value=registry):
                directory = Path(temp)
                report = run(load_config(self.manifest(directory, {"compatibility_suites":["broken"]})), directory/"results")
                self.assertEqual(report["samples"], [])
                self.assertTrue(any(c["status"] == "fail" and c["case"] == "setup" for c in report["cases"]))

    def test_catalog_cli_lists_selected_cases_without_worker_start(self):
        """List complete coverage with a missing SDK executable; expect JSON inventory and zero cloud/SDK execution."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self.manifest(directory)
            data = json.loads(path.read_text())
            data["targets"][0]["command"] = ["/does-not-exist/sdk"]
            path.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, "-m", "osv", "catalog", str(path), "--profile", "complete"],
                                    capture_output=True, text=True, timeout=10, check=True)
            inventory = json.loads(result.stdout)
            self.assertEqual(inventory["profile"], "complete")
            self.assertEqual(len(inventory["catalog"]), 825)
            self.assertFalse((directory/"objects").exists())
