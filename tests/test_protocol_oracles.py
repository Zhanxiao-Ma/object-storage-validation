"""Check semantic oracles with independent raw values, including rejection boundaries."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from osv.ceph_tags import check_tags
from osv.ceph_acl import actor,grant,check_grants,check_owner,owner_document
from osv.ceph_versioning import check_marker_response
from osv.suites import CaseNotRun

class ProtocolOracleTest(unittest.TestCase):
    """Keep optional presentation fields separate from required identity, content and cardinality."""
    def test_tag_order_is_irrelevant_but_missing_extra_duplicate_and_changed_values_fail(self):
        """Compare independently specified tag sets; expect reordered/empty tags to pass and any content or uniqueness violation to fail."""
        wanted=[{'key':'a','value':''},{'key':'b','value':'2'}]
        check_tags(list(reversed(wanted)),wanted);check_tags([],[])
        for got in (wanted[:1],[],wanted+[{'key':'c','value':'3'}],wanted+[wanted[0]],
                    [{'key':'a','value':'bad'},wanted[1]],[{'key':'a','value':None},wanted[1]]):
            with self.subTest(got=got),self.assertRaises(AssertionError):check_tags(got,wanted)

    def test_grants_match_canonical_identity_without_optional_name_or_order(self):
        """Allow missing labels and reordered grants, but reject wrong ID, permission, URI, count, labels supplied by both sides, and unknown fields."""
        expected=[grant('READ',uri='urn:group'),grant('FULL_CONTROL',{'canonical_id':'owner','display_name':'label'})]
        actual=[{'Permission':'FULL_CONTROL','Grantee':{'Type':'CanonicalUser','ID':'owner'}},
                {'Permission':'READ','Grantee':{'Type':'Group','URI':'urn:group'}}]
        check_grants(actual,expected);check_grants([],[])
        variants=[actual[:1],actual+actual[:1]]
        for field,value in [('ID','other'),('Type','Group'),('DisplayName','wrong'),('Extra','unexpected')]:
            v=deepcopy(actual);v[0]['Grantee'][field]=value;variants.append(v)
        v=deepcopy(actual);v[0]['Permission']='READ';variants.append(v)
        v=deepcopy(actual);v[1]['Grantee']['URI']='urn:other';variants.append(v)
        for got in variants:
            with self.subTest(got=got),self.assertRaises(AssertionError):check_grants(got,expected)

    def test_actor_needs_independent_id_but_not_display_name(self):
        """Supply only independent canonical ID; expect a valid actor/owner document and correct owner checks, while absent/empty IDs and a wrong actual ID fail."""
        p=SimpleNamespace(ctx=SimpleNamespace(workload={'identities':{'main':{'canonical_id':'owner'}}}))
        value=actor(p);self.assertEqual(owner_document(value),{'ID':'owner'})
        check_owner({'ID':'owner'},value)
        with self.assertRaises(AssertionError):check_owner({'ID':'wrong'},value)
        for value in (None,'',False,123):
            p.ctx.workload['identities']['main']={'canonical_id':value}
            with self.subTest(value=value),self.assertRaises(CaseNotRun):actor(p)

    def test_delete_marker_optional_only_for_plain_404(self):
        """Accept plain HEAD404 without marker or with explicit false; require true for actual markers and reject success, wrong status and false marker claims."""
        def response(value=None,status=404):
            return {'ok':status==200,'status_code':status,'http_headers':{} if value is None else {'X-Amz-Delete-Marker':value}}
        check_marker_response(response(),False);check_marker_response(response('false'),False)
        check_marker_response(response(['true']),True)
        for result,expected in ((response('true'),False),(response(),True),(response('false'),True),
                                (response(status=200),False),(response(status=403),False),(response(''),False)):
            with self.subTest(result=result,expected=expected),self.assertRaises(AssertionError):check_marker_response(result,expected)
