"""Keep the complete unsupported logging-auth source visible without claiming its legacy SigV2 backend exists."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]

class LoggingAuthAuditTest(unittest.TestCase):
    """Verify immutable source/identity-factory evidence and its explicit unimplemented status, rather than simulating authentication or log production."""
    def setUp(self):
        """Load packaged static evidence; expect no upstream module import or SDK process."""
        self.audit=json.loads((ROOT/'osv/data/ceph_logging_auth_type_audit.json').read_text())
    def test_exact_source_body_markers_assertions_and_helper_fingerprints(self):
        """Compare the entire original function against pinned inventory and every helper's hash; expect all original inputs/assertions and marker metadata preserved unchanged."""
        row=self.audit['cases'][0];inventory=json.loads((ROOT/'osv/data/upstream.json').read_text())['ceph']['cases'];original=next(case for case in inventory if case['id']==row['source_id'])
        self.assertEqual(row['source_sha256'],original['sha256']);self.assertEqual(row['markers'],original['markers'])
        for item in [row,*self.audit['helpers'],*self.audit['external_identity_factories']]:
            self.assertEqual(hashlib.sha256(item['source'].encode()).hexdigest(),item['source_sha256']);ast.parse(item['source'])
        self.assertEqual([ast.get_source_segment(row['source'],node) for node in ast.walk(ast.parse(row['source'])) if isinstance(node,ast.Assert)],row['assertions'])
        self.assertIn('Permission is hereby granted',self.audit['license_notice'])
    def test_complete_four_auth_phases_preserve_both_real_query_signature_versions(self):
        """Inspect the static original source and identity factories; expect signed/anonymous GETs, exactly two100000-second GET presigns, both OPTIONS400/GET200 paths and separate SigV4/SigV2 log assertions."""
        row=self.audit['cases'][0];tree=ast.parse(row['source']);calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call)]
        urls=[call for call in calls if isinstance(call.func,ast.Attribute) and call.func.attr=='generate_presigned_url']
        self.assertEqual(len(urls),2)
        for call in urls:
            values={kw.arg:ast.literal_eval(kw.value) for kw in call.keywords if kw.arg in ('ClientMethod','ExpiresIn','HttpMethod')}
            self.assertEqual(values,{'ClientMethod':'get_object','ExpiresIn':100000,'HttpMethod':'GET'})
        requests=[call for call in calls if isinstance(call.func,ast.Attribute) and isinstance(call.func.value,ast.Name) and call.func.value.id=='requests']
        self.assertEqual([call.func.attr for call in requests],['options','get','options','get'])
        phases=self.audit['required_full_stages'];self.assertEqual([phase['phase'] for phase in phases],['AuthHeader','Anonymous','QueryStringSigV4','QueryStringSigV2'])
        self.assertEqual([phase['raw_http'] for phase in phases[2:]],[{'OPTIONS':400,'GET':200}]*2)
        factories={item['name']:item['source'] for item in self.audit['external_identity_factories']}
        self.assertIn("Config(signature_version='s3v4')",factories['get_client']);self.assertIn("Config(signature_version='s3')",factories['get_v2_client']);self.assertIn('UNSIGNED',factories['get_unauthenticated_client'])
        for version in ('SigV4','SigV2'):self.assertIn('"SigVersion", "'+version+'"',row['source'])
    def test_unimplemented_legacy_signer_cannot_be_counted_as_executable_alignment(self):
        """Keep this full-body audit distinct from completed test coverage; expect partial state, no executable registration, no aligned source review and an explicit genuine V2 signer gap."""
        self.assertEqual(self.audit['state'],'partial_static_source_only');self.assertIs(self.audit['register_executable_cases'],False);self.assertEqual(self.audit['active_case_count'],1)
        row=self.audit['cases'][0];self.assertEqual(row['alignment_status'],'partial');self.assertIn('SignatureV4 cannot substitute',row['blocker'])
        reviews=json.loads((ROOT/'osv/data/ceph_reviews.json').read_text())['reviews'];self.assertFalse([review for review in reviews if review['source_id']==row['source_id']])
