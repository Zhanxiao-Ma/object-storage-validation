"""Control-plane source prerequisites on real workers without configured IAM/STS identities."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from osv.scenarios import Context
from osv.suites import run_cases
from sdk_fixtures import fixture_worker

MODULES=('ceph_iam_accounts','ceph_iam_inline','ceph_iam_policy_entities','ceph_iam_oidc','ceph_iam_role_policies','ceph_iam_root_keys','ceph_iam_tenant_actions','ceph_iam_assumed_roles','ceph_iam_cross_resources','ceph_sts_callers','ceph_sts_basic')


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable complete IAM/STS default prerequisite contract')
class AllIamDefaultsSdkTest(unittest.TestCase):
    """Ensure missing advanced identities never become implicit S3-main/admin fallback or a fake compatibility pass."""
    def test_all_ninety_iam_and_nine_sts_sources_are_precise_not_run_without_control_plane_configuration(self):
        """Execute all 99 registered source objects on both real SDKs with no control-plane endpoints/identities; expect 99 not_run rows, zero pass/fail and zero HTTP, including no accidental 900-second expiry execution."""
        cases=tuple(c for module in MODULES for c in importlib.import_module('osv.'+module).CASES);self.assertEqual(len(cases),99)
        environment={'OSV_IAM_ENDPOINT':'','OSV_IAM_REGION':'','OSV_STS_ENDPOINT':'','OSV_STS_REGION':'','OSV_IAM_ACCESS_KEY':'','OSV_IAM_SECRET_KEY':'','OSV_IAM_ROOT_ACCESS_KEY':'','OSV_IAM_ROOT_SECRET_KEY':'','OSV_IAM_ALT_ROOT_ACCESS_KEY':'','OSV_IAM_ALT_ROOT_SECRET_KEY':''}
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3',environment) as (worker,server):
                d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'out',{}),cases);self.assertEqual(len(rows),99);self.assertEqual({row['status'] for row in rows},{'not_run'});self.assertEqual(server.requests,[])
