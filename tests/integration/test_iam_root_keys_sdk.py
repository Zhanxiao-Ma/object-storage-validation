"""Native IAM root-key vectors with independent pre-existing-key safety inputs."""
import os
from pathlib import Path
import tempfile
import unittest
from osv.ceph_iam_root_keys import current_key
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from test_iam_accounts_sdk import iam_fixture,verify_requests


def root_plan(mode,existing_fake=False,collision=False):
    plan=[];fake='abcdefghijklmnopqrstu';existing=fake if existing_fake else 'EXISTING_ROOT_KEY';fresh='EXISTING_ROOT_KEY' if collision else 'FRESH_RETURNED_ROOT_KEY'
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    def listed(keys):add('ListAccessKeys',data={'AccessKeyMetadata':[{'AccessKeyId':key,'Status':status,'CreateDate':'2025-01-01T00:00:00Z'} for key,status in keys],'IsTruncated':False})
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}});listed([(existing,'Active')])
    if existing_fake:return plan
    if mode in ('update','delete'):add('UpdateAccessKey' if mode=='update' else 'DeleteAccessKey',{'AccessKeyId':fake,**({'Status':'Active'} if mode=='update' else {})},code='NoSuchEntity')
    add('CreateAccessKey',data={'AccessKey':{'AccessKeyId':fresh,'SecretAccessKey':'fresh-root-dynamic-secret','Status':'Active','CreateDate':'2025-01-01T00:00:00Z'}})
    if collision:return plan
    if mode=='update':
        for status in ('Active','Inactive'):add('UpdateAccessKey',{'AccessKeyId':fresh,'Status':status})
        listed([(existing,'Active'),(fresh,'Inactive')])
    elif mode=='delete':
        add('DeleteAccessKey',{'AccessKeyId':fresh});add('DeleteAccessKey',{'AccessKeyId':fresh},code='NoSuchEntity');listed([(existing,'Active')])
    add('DeleteAccessKey',{'AccessKeyId':fresh},code='NoSuchEntity' if mode=='delete' else None);return plan


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM SDK Query contracts')
class IamRootKeysSdkTest(unittest.TestCase):
    """Verify that source root-key operations use real returned IDs and never modify an existing root key."""
    def test_three_original_root_key_vectors_omit_username_and_keep_existing_key(self):
        """Execute three complete root-key sources on both official SDKs; expect omitted UserName, actual generated key IDs, original Active/Inactive/date/pagination/error assertions and finally deletion of only the fresh key."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('create','update','delete'):
                plan=root_plan(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}));current_key(p,mode);verify_requests(self,server,plan)
                    from urllib.parse import parse_qs,urlsplit
                    for method,path,headers,body in server.requests:self.assertNotIn('UserName',parse_qs(body.decode() if method=='POST' else urlsplit(path).query))

    def test_existing_fixed_negative_key_causes_not_run_before_any_mutation(self):
        """Supply the source's fixed negative key in actual ListAccessKeys output; expect not_run before Update/Delete/Create so an existing root credential cannot be modified by a negative vector."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in ('update','delete'):
                plan=root_plan(mode,existing_fake=True)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with self.assertRaises(CaseNotRun):current_key(p,mode)
                    verify_requests(self,server,plan)

    def test_create_returning_an_existing_key_never_grants_cleanup_ownership(self):
        """Return an already existing key from actual SDK CreateAccessKey XML; expect a safety assertion and no Update/Delete of that credential, including finally."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=root_plan('create',collision=True)
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                with self.assertRaisesRegex(AssertionError,'pre-existing root key'):current_key(p,'create')
                verify_requests(self,server,plan)
