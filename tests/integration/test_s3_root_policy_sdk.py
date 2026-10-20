"""Raw independent IAM XML and S3 responses feed real source credential chains."""
from collections import deque
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import uuid
from test_iam_accounts_sdk import iam_fixture,entity,verify_requests,PREFIX,PATH
from osv.ceph_s3_root_policy import deny_self
from osv.scenarios import Context
from osv.suites import Probe


@unittest.skipUnless(os.environ.get("OSV_RUN_S3_ROOT_POLICY_TESTS")=="1","enable real root-policy SDK contracts")
class RootPolicySdkTest(unittest.TestCase):
    """Verify root/fresh-user authorization requests and owned cleanup without simulating policy enforcement."""
    def exercise(self,language,confirmed,wrong_policy=False):
        """Feed literal independent responses; expect the original exact policy text/403 assertions and genuine cleanup regardless of assertion success."""
        name=PREFIX+'policy-user';user=entity('User',name);key={'UserName':name,'AccessKeyId':'fresh-policy-key','SecretAccessKey':'fresh-policy-secret','Status':'Active','CreateDate':'2025-01-01T00:00:00Z'}
        plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:root'}},None),('CreateUser',{'UserName':name,'Path':PATH},{'User':user},None),('CreateAccessKey',{'UserName':name},{'AccessKey':key},None),('GetUser',{'UserName':name},{'User':user},None),('DeleteAccessKey',{'UserName':name,'AccessKeyId':key['AccessKeyId']},None,None),('GetUser',{'UserName':name},{'User':user},None),('DeleteUser',{'UserName':name},None,None)]
        bucket='osv-'+uuid.UUID(int=1).hex
        document=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Deny','Principal':'*','Action':['s3:PutBucketPolicy','s3:GetBucketPolicy','s3:DeleteBucketPolicy'],'Resource':['arn:aws:s3:::'+bucket,'arn:aws:s3:::'+bucket+'/*']}]})
        empty=(200,{},b'');denied=(403,{'Content-Type':'application/xml'},b'<Error><Code>AccessDenied</Code><Message>independent denied policy request</Message></Error>')
        docs=[(404,{},b''),empty,empty]+[denied]*3
        docs+=([denied]*3 if confirmed else [(200,{'Content-Type':'application/json'},('{}' if wrong_policy else document).encode())]+([] if wrong_policy else [empty,empty]))
        docs += [empty,(200,{'Content-Type':'application/xml'},b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>owned</Name><IsTruncated>false</IsTruncated></ListBucketResult>'),empty,(404,{},b'')]
        with tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
            server.s3.fixtures=deque(docs)
            d=Path(temporary);probe=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
            with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                if wrong_policy:
                    with self.assertRaises(AssertionError):deny_self(probe,confirmed)
                else:deny_self(probe,confirmed)
            verify_requests(self,server,plan)
            self.assertFalse(server.s3.fixtures)
            requests=server.s3.requests
            self.assertEqual([(m,p) for m,p,_,_ in requests[:6]],[('HEAD','/'+bucket),('PUT','/'+bucket),('PUT','/'+bucket+'?policy'),('GET','/'+bucket+'?policy'),('DELETE','/'+bucket+'?policy'),('PUT','/'+bucket+'?policy')])
            first_headers={k.lower():v for k,v in requests[2][2].items()}
            self.assertIn('Credential=iam-root-real/',first_headers['authorization'])
            self.assertEqual(first_headers.get('x-amz-confirm-remove-self-bucket-access'),'true' if confirmed else None)
            self.assertEqual(requests[2][3].decode(),document)
            for _,_,headers,_ in requests[3:6]:self.assertIn('Credential=fresh-policy-key/',next(v for k,v in headers.items() if k.lower()=='authorization'))
            for _,_,headers,_ in requests[6:]:self.assertIn('Credential=iam-root-real/',next(v for k,v in headers.items() if k.lower()=='authorization'))
            self.assertEqual(requests[-2][0],'DELETE');self.assertEqual(requests[-1][0],'HEAD')

    def test_both_original_root_policy_vectors_keep_exact_identity_header_and_assertions(self):
        """Run both pinned sources on actual Java/C++ IAM/S3 SDKs; expect real fresh-key signatures, strict root/child 403 branches and ConfirmRemoveSelf header only when originally present."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for confirmed in (False,True):
                with self.subTest(language=language,confirmed=confirmed):self.exercise(language,confirmed)

    def test_wrong_actual_policy_does_not_pass_and_owned_cleanup_still_runs(self):
        """Return an independent wrong real policy body; expect source assertion failure while actual owned bucket/key/user cleanup still executes."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language):self.exercise(language,False,True)
