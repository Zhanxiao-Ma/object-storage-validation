"""Preserve the independent Ceph delete-marker expiration source oracle through genuine SDK response headers."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sdk_fixtures import fixture_worker
from test_lifecycle_expiration_sdk import XML,LC,scope_begin,scope_end,put_result
from osv.ceph_lifecycle_marker_expiration import CASES,check_marker
from osv.ceph_lifecycle_expiration import wait
from osv.scenarios import Context
from osv.suites import Probe,run_cases


@unittest.skipUnless(os.environ.get("OSV_RUN_LIFECYCLE_MARKER_TESTS")=="1","enable real SDK tests")
class LifecycleMarkerExpirationSdkTest(unittest.TestCase):
    """Validate original raw-header string semantics without implementing a lifecycle engine in the fixture."""
    def test_original_marker_true_to_false_and_full_wait(self):
        """Use independent 404 HEAD documents and real SDK errors; expect true before the unchanged six-interval wait, false after it, original rules/body and actual cleanup."""
        failed=lambda value:(404,{"x-amz-delete-marker":value},b"")
        documents=scope_begin(True)+[put_result(0),(204,{"x-amz-version-id":"actual-marker","x-amz-delete-marker":"true"},b""),failed("true"),LC,failed("false")]+scope_end(rows=[])
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,documents,"s3") as (worker,server):
                directory=Path(temporary);context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{"lifecycle_debug_interval_seconds":.001})
                if CASES[0].operations-worker.capabilities:
                    self.assertEqual(run_cases(context,CASES)[0]["status"],"not_run");self.assertEqual(server.requests,[]);continue
                with patch("osv.ceph_lifecycle_marker_expiration.wait",wraps=wait) as observed:CASES[0].execute(Probe(context))
                self.assertEqual([item.args[0] for item in observed.call_args_list],[.006])
                self.assertEqual(len(server.requests),len(documents))
                self.assertTrue(any(row[0]=="PUT" and row[3]==b"body version nugent.ted" for row in server.requests))
                self.assertTrue(any(row[0]=="PUT" and b"dm-1-days" in row[3] and b"noncur-1-days" in row[3] for row in server.requests))

    def test_missing_service_clock_never_creates_bucket(self):
        """Omit backend acceleration on each SDK; expect explicit not_run and zero HTTP instead of compressed client waits."""
        for language in os.environ.get("OSV_S3_TEST_LANGUAGES","java,cpp").split(","):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],"s3") as (worker,server):
                directory=Path(temporary);context=Context(worker,"unused/",directory,b"independent",directory/"body",directory/"output",{})
                self.assertEqual(run_cases(context,CASES)[0]["status"],"not_run");self.assertEqual(server.requests,[])
