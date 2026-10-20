"""Exercise production runner, worker transport and report semantics together."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from osv.cli import load_config, run
from osv.report import comparisons, percentile
from osv.worker import Worker
from osv.scenarios import Registry, Operation, Suite, build_registry

ROOT = Path(__file__).resolve().parents[1]


def config(directory):
    return {"schema_version": 1, "targets": [{
        "id": "local", "provider": "filesystem", "language": "python", "api": "local",
        "sdk": "reference", "sdk_version": "1", "command": [sys.executable, str(ROOT/"drivers/reference.py")],
        "env": {"OSV_REFERENCE_ROOT": str(directory/"objects"), "OSV_REFERENCE_MIN_PART_SIZE":"128"}}],
        "workload": {"performance_model":"operations", "large_object_size":512, "object_size": 256, "range_size": 17, "part_size": 128,
                     "requests": 7, "concurrency": 2, "rounds": 2, "warmup_requests": 2,
                     "operations": ["get", "range", "put", "head", "list"]}}


class HarnessTest(unittest.TestCase):
    """Validate orchestration with a real filesystem producer and consumer, not fake cloud results."""

    def test_extension_and_account_mutation_flags_are_explicit_booleans(self):
        """Purpose: load real extension/account manifests; expect opt-ins disabled by default and reject truthy strings/numbers or absent account identifiers before SDK IO."""
        flags=('ceph_logging_wire_extensions','sns_mutation_authorized','isolated_account_mutation_authorized')
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);manifest=directory/'extensions.json'
            manifest.write_text(json.dumps(config(directory)))
            loaded=load_config(manifest)['workload']
            for key in flags:
                self.assertIs(loaded[key],False)
                for value in (True,False,'true',1,None):
                    changed=config(directory);changed['workload'][key]=value
                    manifest.write_text(json.dumps(changed))
                    if type(value) is bool:self.assertIs(load_config(manifest)['workload'][key],value)
                    else:
                        with self.assertRaises(ValueError):load_config(manifest)
            for value in ('isolated-account','',None,123):
                changed=config(directory);changed['workload']['account_id']=value
                manifest.write_text(json.dumps(changed))
                if isinstance(value,str) and value:self.assertEqual(load_config(manifest)['workload']['account_id'],value)
                else:
                    with self.assertRaises(ValueError):load_config(manifest)

    def test_real_worker_to_report_and_cleanup(self):
        """Run actual worker IO and reporting; content gates pass and all run objects are removed."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = directory/"manifest.json"
            manifest.write_text(json.dumps(config(directory)))
            report = run(load_config(manifest), directory/"results")
            self.assertFalse([c for c in report["cases"] if c["status"] == "fail"])
            self.assertEqual(len(report["summary"]), 5)
            self.assertTrue(all(row["attempts"] == 14 for row in report["summary"]))
            self.assertTrue(all(row["failures"] == 0 for row in report["summary"]))
            self.assertEqual(report["comparisons"], [])
            persisted = json.loads((directory/"results/report.json").read_text())
            self.assertEqual(persisted["run_id"], report["run_id"])
            self.assertEqual([p for p in (directory/"objects").rglob("*") if p.is_file()], [])
            self.assertIn("没有 SDK 性能对比结论", (directory/"results/report.md").read_text())

    def test_zero_and_unbounded_parameters_rejected_before_process_start(self):
        """Reject invalid or excessive load sizes without starting workers or cloud operations."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for key, value in [("requests",0),("concurrency",129),("range_size",257),("object_size",-1),("warmup_requests",-1)]:
                with self.subTest(key=key):
                    data=config(directory)
                    data["workload"][key]=value
                    manifest=directory/"input.json"
                    manifest.write_text(json.dumps(data))
                    with self.assertRaises(ValueError): load_config(manifest)

    def test_iam_configuration_requires_explicit_typed_authorization(self):
        """Load actual manifests; expect IAM mutation opt-in false by default and reject ambiguous flags or empty fixture identifiers before IO."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            data=config(directory)
            manifest=directory/"iam.json"
            manifest.write_text(json.dumps(data))
            self.assertIs(load_config(manifest)["workload"]["iam_mutation_authorized"],False)
            for key,value in (("iam_mutation_authorized",True),("iam_alt_user_name","isolated-user"),("iam_root_account_id","123456789012"),("iam_root_user_id","root-user-id")):
                changed=config(directory);changed["workload"][key]=value
                manifest.write_text(json.dumps(changed))
                self.assertEqual(load_config(manifest)["workload"][key],value)
            for key,value in (("iam_mutation_authorized","true"),("iam_mutation_authorized",1),("iam_mutation_authorized",None),("iam_alt_user_name",""),("iam_alt_user_name",False),("iam_root_account_id",123),("iam_root_user_id",""),("iam_root_user_id",None)):
                with self.subTest(key=key,value=value):
                    changed=config(directory);changed["workload"][key]=value
                    manifest.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):load_config(manifest)

    def test_web_identity_configuration_contains_environment_names_only(self):
        """Load actual WebIdentity manifests; expect typed explicit fixture metadata and reject raw token fields, ambiguous settings and invalid environment names before SDK startup."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);manifest=directory/"web.json"
            valid={"realm":"test-realm","aud":"test-client","sub":"test-user","azp":"test-authorized-party","thumbprint":"EXAMPLE","token_env":"OSV_WEB_TOKEN","user_token_env":"OSV_USER_WEB_TOKEN"}
            data=config(directory);data["workload"]["sts_web_identity"]=valid
            manifest.write_text(json.dumps(data));self.assertEqual(load_config(manifest)["workload"]["sts_web_identity"],valid)
            for settings in ([],None,{"token":"raw-jwt-value"},{"realm":""},{"aud":True},{"token_env":"raw.jwt.value"},{"token_env":"9TOKEN"},{"user_token_env":False}):
                with self.subTest(settings=settings):
                    data=config(directory);data["workload"]["sts_web_identity"]=settings
                    manifest.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):load_config(manifest)

    def test_worker_timeout_terminates_process(self):
        """A worker that never responds must time out and be reaped, not hang the test run."""
        with self.assertRaises(TimeoutError):
            Worker({"command":[sys.executable,"-c","import time; time.sleep(60)"]},timeout=.1)

    def test_failed_worker_exposes_sanitized_initialization_reason(self):
        """Exit a real worker with a diagnostic containing inherited credentials; expect useful reason with the secret redacted."""
        with patch.dict(os.environ,{"OSV_SECRET_KEY":"never-print-this-test-secret"}):
            with self.assertRaises(RuntimeError) as caught:
                Worker({"command":[sys.executable,"-c","import os,sys; print('invalid endpoint '+os.environ['OSV_SECRET_KEY'],file=sys.stderr); raise SystemExit(2)"]},timeout=5)
        self.assertIn("invalid endpoint",str(caught.exception))
        self.assertIn("[REDACTED]",str(caught.exception))
        self.assertNotIn("never-print-this-test-secret",str(caught.exception))

    def test_worker_identity_mismatch_is_rejected(self):
        """A real Python reference process must never be accepted as C++ SDK evidence."""
        with tempfile.TemporaryDirectory() as temp:
            target=config(Path(temp))["targets"][0]
            target["language"]="cpp"
            with self.assertRaisesRegex(RuntimeError,"language differs"):
                Worker(target,timeout=5)

    def test_external_exit_and_redaction_are_preserved(self):
        """Actual subprocess stdout reaches the external recorder; secrets are redacted and nonzero exit is retained."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            data={"schema_version":1,"external":[{"id":"stdout-producer","tool":"juicefs",
                  "command":[sys.executable,"-c","import os; print(os.environ['OSV_SECRET_KEY']); raise SystemExit(3)"]}]}
            manifest=directory/"input.json"
            manifest.write_text(json.dumps(data))
            with patch.dict(os.environ,{"OSV_SECRET_KEY":"test-secret-not-a-credential"}):
                report=run(load_config(manifest),directory/"results")
            self.assertEqual(report["external"][0]["exit_code"],3)
            self.assertEqual(report["external"][0]["evidence"],"supplementary")
            self.assertEqual((directory/"results/stdout-producer.stdout.txt").read_text(),"[REDACTED]\n")
            self.assertEqual(report["cases"],[])

    def test_missing_worker_is_failed_evidence_not_a_pass(self):
        """An absent SDK executable produces a failed setup gate and no performance measurements."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            data=config(directory)
            data["targets"][0]["command"]=["/does-not-exist/osv-worker"]
            manifest=directory/"input.json"
            manifest.write_text(json.dumps(data))
            report=run(load_config(manifest),directory/"results")
            self.assertTrue(any(c["case"]=="setup" and c["status"]=="fail" for c in report["cases"]))
            self.assertEqual(report["summary"],[])

    def test_credentials_rejected_in_manifest_and_redacted_in_evidence(self):
        """Prevent credential-bearing manifests and redact inherited credential strings in errors."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            data=config(directory)
            data["targets"][0]["env"]["OSV_SECRET_KEY"]="do-not-save-this"
            manifest=directory/"input.json"
            manifest.write_text(json.dumps(data))
            with self.assertRaises(ValueError): load_config(manifest)
            data=config(directory)
            data["targets"][0]["command"]=["/missing/SECRET-FOR-TEST-ONLY"]
            manifest.write_text(json.dumps(data))
            with patch.dict(os.environ,{"OSV_SECRET_KEY":"SECRET-FOR-TEST-ONLY"}):
                run(load_config(manifest),directory/"results")
            self.assertNotIn("SECRET-FOR-TEST-ONLY",(directory/"results/report.json").read_text())

    def test_plugin_third_language_runs_real_worker_without_scheduler_changes(self):
        """Load external operation/suite against the real Python worker; verify reporting and scoped cleanup."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            data = config(directory)
            data["plugins"] = ["examples.inventory_plugin"]
            data["workload"]["operations"] = ["inventory"]
            data["workload"]["compatibility_suites"] = ["baseline", "inventory_semantics"]
            manifest = directory/"input.json"
            manifest.write_text(json.dumps(data))
            report = run(load_config(manifest), directory/"results")
            self.assertEqual(report["targets"][0]["language"], "python")
            self.assertFalse([c for c in report["cases"] if c["status"] == "fail"])
            self.assertTrue(any(c.get("suite") == "inventory_semantics" and c["status"] == "pass" for c in report["cases"]))
            self.assertEqual(report["summary"][0]["attempts"], 14)
            self.assertEqual(report["summary"][0]["failures"], 0)
            self.assertEqual(report["operation_metrics"], {"inventory": "verified_ops_s"})
            self.assertEqual([p for p in (directory/"objects").rglob("*") if p.is_file()], [])

    def test_unsupported_extension_fails_before_measurement(self):
        """A real worker lacking an extension capability must produce a failed setup, never a passing sample."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            data = config(directory)
            manifest = directory/"input.json"
            manifest.write_text(json.dumps(data))
            data = load_config(manifest)
            registry = build_registry(data)
            registry.add_operation(Operation("unsupported_probe", frozenset({"unimplemented_operation"}), lambda ctx: {"op":"unimplemented_operation"}, lambda *args: 0))
            data["workload"]["operations"] = ["unsupported_probe"]
            with patch("osv.cli.build_registry", return_value=registry):
                report = run(data, directory/"results")
            self.assertEqual(report["samples"], [])
            self.assertTrue(any(c["case"] == "setup" and c["status"] == "fail" and "unimplemented_operation" in c["error"] for c in report["cases"]))

    def test_extension_registration_and_configuration_boundaries(self):
        """Reject duplicate names, invalid metrics, unknown operations, and repeated plugins before worker startup."""
        registry = Registry()
        item = Operation("probe", frozenset({"head"}), lambda ctx: {}, lambda *args: 0)
        registry.add_operation(item)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.add_operation(item)
        with self.assertRaisesRegex(ValueError, "metric"):
            registry.add_operation(Operation("bad_metric", frozenset({"head"}), lambda ctx: {}, lambda *args: 0, "latency"))
        with self.assertRaisesRegex(ValueError, "required"):
            registry.add_suite(Suite("missing", frozenset(), lambda ctx: []))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for override in ({"operations": ["unregistered"]}, {"operations": ["head", "head"]}, {"compatibility_suites": []}):
                data = config(directory)
                data["workload"].update(override)
                manifest = directory/"input.json"
                manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_config(manifest)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_registry({"plugins": ["examples.inventory_plugin", "examples.inventory_plugin"]})
        self.assertNotIn("inventory", build_registry({}).operations)

    def test_plugin_metric_is_used_without_report_operation_whitelist(self):
        """A new byte-transferring operation must use its declared metric even when the reporter has never seen its name."""
        cfg={"targets":[{"id":"a","provider":"x","language":"rust","api":"s3"},
                        {"id":"b","provider":"x","language":"rust","api":"native"}],
             "comparisons":[{"s3":"a","native":"b","min_samples":10,"min_throughput_ratio":.9,
                             "max_p95_ratio":1.1,"same_storage_and_network_confirmed":True}]}
        rows=[{"target":"a","op":"custom_read","successes":10,"failures":0,"verified_mib_s":80,"verified_ops_s":100,"p95_ms":10},
              {"target":"b","op":"custom_read","successes":10,"failures":0,"verified_mib_s":100,"verified_ops_s":100,"p95_ms":10}]
        gates=[{"target":"a","status":"pass"},{"target":"b","status":"pass"}]
        result=comparisons(cfg,gates,rows,{"custom_read":"verified_mib_s"})[0]
        self.assertEqual(result["throughput_metric"], "verified_mib_s")
        self.assertEqual(result["status"], "outside_budget")

    def test_comparison_budget_rejects_nonfinite_boolean_and_fractional_counts(self):
        """Reject meaningless budgets and sample counts before any driver starts; accept finite positive values."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for key, value in (("min_samples", 1.5), ("min_samples", True),
                               ("min_throughput_ratio", float("nan")),
                               ("max_p95_ratio", float("inf")), ("max_p95_ratio", 0)):
                with self.subTest(key=key, value=value):
                    data = config(directory)
                    data["comparisons"] = [{"s3":"local", "native":"local", "min_samples":10,
                        "min_throughput_ratio":.9, "max_p95_ratio":1.1}]
                    data["comparisons"][0][key] = value
                    manifest = directory/"input.json"
                    manifest.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        load_config(manifest)

    def test_quantile_singleton_and_empty(self):
        """Nearest-rank quantiles preserve singleton values and leave empty measurements undefined."""
        self.assertIsNone(percentile([],.99))
        self.assertEqual(percentile([7],.99),7)
        self.assertEqual(percentile(list(range(1,101)),.95),95)

    def test_comparison_gates_budgets_and_language_boundaries(self):
        """Apply independent ratio expectations; failures, missing samples and cross-language pairs remain inconclusive."""
        targets=[{"id":"s3","provider":"oss","language":"cpp","api":"s3"},
                 {"id":"native","provider":"oss","language":"cpp","api":"native"}]
        cfg={"targets":targets,"comparisons":[{"s3":"s3","native":"native","min_samples":100,
             "min_throughput_ratio":.9,"max_p95_ratio":1.1,"same_storage_and_network_confirmed":True}]}
        summary=[{"target":"s3","op":"range","failures":0,"successes":100,"verified_mib_s":95.,"p95_ms":10.5},
                 {"target":"native","op":"range","failures":0,"successes":100,"verified_mib_s":100.,"p95_ms":10.}]
        cases=[{"target":"s3","status":"pass"},{"target":"native","status":"pass"}]
        self.assertEqual(comparisons(cfg,cases,summary)[0]["status"],"within_budget")
        slow=copy.deepcopy(summary); slow[0]["verified_mib_s"]=80
        self.assertEqual(comparisons(cfg,cases,slow)[0]["status"],"outside_budget")
        failed=copy.deepcopy(summary); failed[0]["failures"]=1
        self.assertEqual(comparisons(cfg,[],failed)[0]["status"],"inconclusive")
        few=copy.deepcopy(summary); few[0]["successes"]=99
        self.assertEqual(comparisons(cfg,cases,few)[0]["reason"],"insufficient sample count")
        self.assertEqual(comparisons(cfg,[{"target":"s3","status":"fail"}],summary)[0]["status"],"inconclusive")
        self.assertEqual(comparisons(cfg,[],[])[0]["status"],"inconclusive")
        self.assertEqual(comparisons(cfg,[],summary)[0]["status"],"inconclusive")
        cross=copy.deepcopy(cfg); cross["targets"][1]["language"]="java"
        self.assertEqual(comparisons(cross,[],summary)[0]["status"],"inconclusive")
        cfg["comparisons"][0]["same_storage_and_network_confirmed"]=False
        self.assertEqual(comparisons(cfg,[],summary)[0]["status"],"inconclusive")

    def test_metadata_comparison_uses_operations_not_bytes(self):
        """HEAD has zero payload bytes; comparisons must use operations/s rather than divide zero-byte throughput."""
        cfg={"targets":[{"id":"a","provider":"x","language":"java","api":"s3"},
                        {"id":"b","provider":"x","language":"java","api":"native"}],
             "comparisons":[{"s3":"a","native":"b","min_samples":10,"min_throughput_ratio":.9,
                             "max_p95_ratio":1.1,"same_storage_and_network_confirmed":True}]}
        rows=[{"target":"a","op":"head","successes":10,"failures":0,"verified_mib_s":0,"verified_ops_s":90,"p95_ms":10},
              {"target":"b","op":"head","successes":10,"failures":0,"verified_mib_s":0,"verified_ops_s":100,"p95_ms":10}]
        gates=[{"target":"a","status":"pass"},{"target":"b","status":"pass"}]
        result=comparisons(cfg,gates,rows)[0]
        self.assertEqual(result["status"],"within_budget")
        self.assertEqual(result["throughput_metric"],"verified_ops_s")


if __name__ == "__main__": unittest.main()
