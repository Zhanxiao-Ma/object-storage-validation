"""Exercise generic profile isolation and real IO through the production scheduler."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from osv.cli import load_config, run
from osv.scenarios import Suite, build_registry
from osv.s3_profiles import subset
from osv.suites import case, run_cases
from test_harness import config


class GenericProfileTest(unittest.TestCase):
    def test_only_generic_profiles_are_exposed(self):
        """Inspect real registered catalogs; expect only normal/complete, no vendor/control operations in complete, and rejection of all three removed profile names before IO."""
        registry = build_registry({})
        def rows(profile):
            return [row for name in registry.profiles[profile].suites for row in registry.suites[name].catalog]
        generic = rows("complete")
        self.assertEqual(len(generic), 780)
        self.assertEqual(len({row["case"] for row in generic}), 780)
        self.assertEqual(sum(row["case"].startswith("s3_lifecycle_configuration_") for row in generic), 9)
        self.assertEqual(sum(row["case"].startswith("s3_logging_configuration_") for row in generic), 4)
        names = {row["case"] for row in generic}
        self.assertIn("ceph_presigned_not_expired_tenant_v4", names)
        self.assertIn("ceph_lc_expiration_header_head", names)
        self.assertFalse(names & {"ceph_query_head_usage", "ceph_query_unordered_v2",
            "ceph_remaining_policy_tenant_global", "ceph_logging_records_mtime", "ceph_lc_expiration_prefix", "ceph_body_account_usage"})
        for row in generic:
            self.assertFalse(any(op.startswith(("api.iam.", "api.sts.", "api.sns.", "api.s3control.", "api.ceph."))
                or op in {"ceph_logging_wire_extensions", "ceph_bucket_logging_extensions", "api.PostBucketLogging"}
                for op in row["operations"]), row)
        self.assertEqual(set(registry.profiles), {"normal", "complete"})
        for removed in ("legacy-complete", "ceph-extensions", "identity-services"):
            with tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                data = config(directory)
                data["workload"]["profile"] = removed
                path = directory/"manifest.json"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "unknown profile"):
                    load_config(path)

    def test_normal_includes_common_api_contracts_and_remains_complete_subset(self):
        """Inspect actual selected production catalogs; expect all original checks and independently named common API scenarios once in normal, exact complete metadata reuse, and advanced lock/version/Select cases excluded."""
        registry = build_registry({})
        def catalog(profile):
            return [row for name in registry.profiles[profile].suites for row in registry.suites[name].catalog]
        normal, complete = catalog("normal"), catalog("complete")
        names = {row["case"] for row in normal}
        self.assertEqual(len(normal), 96)
        self.assertEqual(len(names), 96)
        self.assertEqual(len(complete), 780)
        complete_by_name = {row["case"]: row for row in complete}
        self.assertTrue(names <= complete_by_name.keys())
        for row in normal:
            self.assertEqual(row, complete_by_name[row["case"]])
        original = {row["case"] for name in ("core_objects", "objbench_objects") for row in registry.suites[name].catalog}
        self.assertEqual(len(original), 24)
        self.assertTrue(original <= names)
        self.assertTrue({"ceph_copy_same_bucket", "ceph_copy_missing_key", "ceph_bucket_head", "ceph_bucket_create_delete",
            "ceph_exact_list_v2_continuationtoken_walk", "ceph_exact_list_v2_delimiter_prefix",
            "ceph_delete_many_v2_repeat", "ceph_delete_many_v2_limit", "s3_delete_many_quiet", "s3_delete_many_verbose",
            "ceph_exact_multipart_full", "ceph_exact_multipart_resend", "ceph_exact_multipart_abort",
            "s3_lifecycle_configuration_abort_multipart"} <= names)
        operations = {op for row in normal for op in row["operations"]}
        self.assertTrue({"put", "get", "head", "delete", "copy", "delete_many", "bucket_head", "bucket_create_raw",
            "list_v2_observed", "mp_part", "mp_create_metadata", "mp_complete_observed", "mp_abort_raw",
            "api.GetBucketLifecycleConfiguration", "api.PutBucketLifecycleConfiguration"} <= operations)
        self.assertFalse({"s3_delete_many_quiet_versions", "s3_delete_many_quiet_legal_hold", "s3_versions_cursor_walk",
            "s3_logging_configuration_default", "ceph_select_count_operation"} & names)

    def test_mixed_suite_selection_precedes_real_io(self):
        """Run real file-producing/consuming cases via a selected production suite; expect excluded executable never invoked or reported."""
        calls = []
        def selected(p):
            calls.append("selected")
            p.put(b"independent-selected-content")
            self.assertEqual(p.read(), b"independent-selected-content")
        def excluded(p):
            calls.append("excluded")
            raise AssertionError("excluded case executed")
        cases = tuple(case(name, "selection test", "real IO only for selected case", "put get", execute,
            "selection-contract") for name, execute in (("selected_case", selected), ("excluded_case", excluded)))
        registry = build_registry({})
        original = Suite("mixed_selection", frozenset({"put", "get"}), lambda ctx: run_cases(ctx, cases),
            tuple(c.metadata() for c in cases), case_capabilities=True)
        name = subset(registry, original, [cases[0].metadata()], "complete")
        with tempfile.TemporaryDirectory() as temp, patch("osv.cli.build_registry", return_value=registry):
            directory = Path(temp)
            manifest = config(directory)
            manifest["workload"].update(compatibility_suites=[name], mode="functional")
            path = directory / "manifest.json"
            path.write_text(json.dumps(manifest))
            report = run(load_config(path), directory / "results")
            self.assertEqual(calls, ["selected"])
            self.assertEqual([(row["case"], row["status"]) for row in report["cases"]], [("selected_case", "pass")])
            self.assertFalse([p for p in (directory / "objects").rglob("*") if p.is_file()])

    def test_generic_complete_real_runner_and_missing_worker(self):
        """Run generic complete with actual local IO and then a missing executable; expect exact 780 catalog rows and explicit unavailable checks."""
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                manifest = config(directory)
                manifest["workload"].update(profile="complete", mode="functional")
                if missing:
                    manifest["targets"][0]["command"] = ["/does-not-exist/osv-worker"]
                path = directory / "manifest.json"
                path.write_text(json.dumps(manifest))
                report = run(load_config(path), directory / "results")
                rows = [row for row in report["cases"] if row.get("suite")]
                self.assertEqual(len(report["coverage"]["catalog"]), 780)
                if missing:
                    self.assertEqual(rows, [])
                    self.assertEqual(report["cases"][0]["status"], "fail")
                    self.assertIn("| local | 780 | 0 | 0 | 0 | 780 |", (directory / "results/report.md").read_text())
                else:
                    self.assertEqual(len(rows), 780)
                    self.assertEqual({row["case"] for row in rows}, {row["case"] for row in report["coverage"]["catalog"]})
                    self.assertEqual([row["case"] for row in rows if row["status"] == "fail"], ["ceph_tail_unreadable"])
                self.assertEqual(report["samples"], [])


class ConfigurationOracleTest(unittest.TestCase):
    def test_standard_fields_and_optional_defaults(self):
        """Supply independent raw configurations to the real validator; expect optional defaults accepted and missing, changed, extra-rule or malformed standard structures rejected."""
        from osv.s3_configuration import contains
        wanted = {"Rules": [{"ID": "rule", "Expiration": {"Days": 30}}]}
        contains({"Rules": [{"ID": "rule", "Expiration": {"Days": 30}, "Status": "Enabled"}]}, wanted)
        contains({"SimplePrefix": {}}, {"SimplePrefix": {}})
        for actual in ({}, {"Rules": []}, {"Rules": [{"ID": "rule", "Expiration": {"Days": 31}}]},
                       {"Rules": [{"ID": "rule", "Expiration": {}}]}, {"Rules": [{"ID": "rule", "Expiration": {"Days": 30}}] * 2},
                       {"Rules": {}}, {"Rules": [None]}):
            with self.subTest(actual=actual), self.assertRaises(AssertionError):
                contains(actual, wanted)
        with self.assertRaises(AssertionError):
            contains({"SimplePrefix": None}, {"SimplePrefix": {}})
