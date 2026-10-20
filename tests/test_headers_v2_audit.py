"""Pinned V2 header source audit; this is source fidelity evidence, not an executable V2 SDK implementation."""
import ast,hashlib,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

class HeadersV2AuditTest(unittest.TestCase):
    """Keep unsupported real V2 request production visible instead of inflating completed compatibility coverage."""
    def setUp(self):self.audit=json.loads((ROOT/'osv/data/ceph_headers_v2_audit.json').read_text())
    def test_all_twenty_one_original_bodies_and_markers_match_pinned_inventory(self):
        """Check exact source-body fingerprints and complete marker lists for all 21 original V2 definitions; expect 12 object/9 bucket sources and no changed source assertion or input."""
        inventory=json.loads((ROOT/'osv/data/upstream.json').read_text())['ceph']['cases']
        original={row['id']:row for row in inventory if row['id'].startswith('s3tests/functional/test_headers.py::') and 'pytest.mark.auth_aws2' in row['markers']}
        cases=self.audit['cases'];self.assertEqual(len(cases),21);self.assertEqual({row['source_id'] for row in cases},set(original))
        self.assertEqual(sum('::test_object_' in row['source_id'] for row in cases),12);self.assertEqual(sum('::test_bucket_' in row['source_id'] for row in cases),9)
        for row in cases:
            parent=original[row['source_id']];self.assertEqual(row['markers'],parent['markers']);self.assertEqual(row['source_sha256'],parent['sha256']);self.assertEqual(hashlib.sha256(row['source'].encode()).hexdigest(),parent['sha256'])
            function=ast.parse(row['source']).body[0]
            self.assertTrue(any(isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='get_v2_client' for node in ast.walk(function)))
            self.assertEqual([ast.get_source_segment(row['source'],node) for node in ast.walk(function) if isinstance(node,ast.Assert)],row['assertions'])
    def test_original_eight_helpers_preserve_before_call_and_empty_body_vectors(self):
        """Parse the preserved helpers; expect all eight actual before-call hooks, no post-sign substitution, and original empty-body success versus bar-body rejection paths."""
        helpers=self.audit['helpers'];self.assertEqual(len(helpers),8)
        events=[]
        for helper in helpers:
            self.assertEqual(hashlib.sha256(helper['source'].encode()).hexdigest(),helper['source_sha256'])
            tree=ast.parse(helper['source']);hooks=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='register']
            self.assertEqual(len(hooks),1);events.append(ast.literal_eval(hooks[0].args[0]))
            if 'object' in helper['name']:
                writes=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='put_object']
                # Bad helpers pass put_object to assert_raises instead of calling it directly.
                body=[kw.value.value for node in ast.walk(tree) if isinstance(node,ast.Call) for kw in node.keywords if kw.arg=='Body' and isinstance(kw.value,ast.Constant)]
                self.assertEqual(body,['bar'] if '_bad_' in helper['name'] else [])
        self.assertEqual(events.count('before-call.s3.PutObject'),4);self.assertEqual(events.count('before-call.s3.CreateBucket'),4)
    def test_unsupported_v2_audit_never_claims_aligned_or_registered_execution(self):
        """Ensure the preserved source data remains explicitly partial with a real signer gap; expect no aligned source review, no executable registration claim and no V4 substitution."""
        self.assertEqual(self.audit['state'],'partial_static_source_only');self.assertIs(self.audit['register_executable_cases'],False)
        reviewed=json.loads((ROOT/'osv/data/ceph_reviews.json').read_text())['reviews'];ids={row['source_id'] for row in self.audit['cases']}
        self.assertFalse([row for row in reviewed if row['source_id'] in ids])
        self.assertTrue(all(row['alignment_status']=='partial' and 'SignatureV4 cannot substitute' in row['blocker'] for row in self.audit['cases']))
