"""Replay raw Query XML through the real SDK and the sixteen pinned inline-policy sources."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs,urlsplit
from osv.ceph_iam_inline import inline,MODES,ALLOW,NAMES,BAD_USER
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from test_iam_accounts_sdk import iam_fixture

USER='dedicated-alt-user'


def inline_plan(mode,unexpected_success=False,existing=False):
    plan=[];writes=set()
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    def get_user():add('GetUser',{'UserName':USER},{'User':{'UserName':USER,'UserId':'dedicated-immutable-id'}})
    get_user();add('ListUserPolicies',{'UserName':USER},{'PolicyNames':['AllAccessPolicy'] if existing else [],'IsTruncated':False})
    if existing:return plan
    for policy in ('AllAccessPolicy','AllAccessPolicy'*10,'AllowAccessPolicy','AllowAccessPolicy1','AllowAccessPolicy2','AllowAccessPolicy3','non-existing-policy-name'):
        add('GetUserPolicy',{'UserName':USER,'PolicyName':policy},code='ValidationError' if len(policy)>128 else 'NoSuchEntity')
    add('GetUser',{'UserName':'some-non-existing-user-id'},code='NoSuchEntity')
    def c(op,name=None,status=200,user=USER,document=None):
        values={'UserName':user}
        if name is not None:values['PolicyName']=name
        if document is not None:values['PolicyDocument']=json.dumps(document)
        code='NoSuchEntity' if status==404 else 'MalformedPolicyDocument' if status==400 and not unexpected_success else None
        add(op,values,{} if code is None else None,code)
        if op=='PutUserPolicy':writes.add((user,name))
    def put(name='AllAccessPolicy'):c('PutUserPolicy',name,document={'Version':'2012-10-17','Statement':{'Effect':'Allow','Action':'*','Resource':'*'}})
    def delete(name='AllAccessPolicy'):c('DeleteUserPolicy',name)
    if mode=='put_user_policy':put();delete()
    elif mode=='put_user_policy_invalid_user':c('PutUserPolicy','AllAccessPolicy',404,'some-non-existing-user-id',{'Version':'2012-10-17','Statement':{'Effect':'Allow','Action':'*','Resource':'*'}})
    elif mode=='put_user_policy_parameter_limit':c('PutUserPolicy','AllAccessPolicy'*10,400,document={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]*1000})
    elif mode=='put_user_policy_invalid_element':
        for document in ({'Version':'2010-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*'}]},{'Version':'2012-10-17'},{'Version':'2012-10-17','Statement':[{'Sid':'98AB54CF','Effect':'Allow','Action':'*','Resource':'*'},{'Sid':'98AB54CF','Effect':'Allow','Action':'*','Resource':'*'}]},{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'*','Resource':'*','Principal':'arn:aws:iam:::username'}]}):
            c('PutUserPolicy','AllAccessPolicy',400,document=document)
            if unexpected_success:break
    elif mode=='put_existing_user_policy':put();put();delete()
    elif mode=='list_user_policy':put();c('ListUserPolicies');delete()
    elif mode=='list_user_policy_invalid_user':c('ListUserPolicies',status=404,user='some-non-existing-user-id')
    elif mode=='get_user_policy':put();c('GetUserPolicy','AllAccessPolicy');delete()
    elif mode=='get_user_policy_invalid_user':put();c('GetUserPolicy','AllAccessPolicy',404,'some-non-existing-user-id');delete()
    elif mode=='get_user_policy_invalid_policy_name':put();c('GetUserPolicy','non-existing-policy-name',404);delete()
    elif mode=='get_deleted_user_policy':put();delete();c('GetUserPolicy','AllAccessPolicy',404)
    elif mode=='get_user_policy_from_multiple_policies':
        put('AllowAccessPolicy1');put('AllowAccessPolicy2');c('GetUserPolicy','AllowAccessPolicy2');delete('AllowAccessPolicy1');delete('AllowAccessPolicy2')
    elif mode=='delete_user_policy':put('AllowAccessPolicy');delete('AllowAccessPolicy')
    elif mode=='delete_user_policy_invalid_user':put('AllowAccessPolicy');c('DeleteUserPolicy','AllAccessPolicy',404,'some-non-existing-user-id');delete('AllowAccessPolicy')
    elif mode=='delete_user_policy_invalid_policy_name':put('AllowAccessPolicy');c('DeleteUserPolicy','non-existing-policy-name',404);delete('AllowAccessPolicy')
    elif mode=='delete_user_policy_from_multiple_policies':
        for name in ('AllowAccessPolicy1','AllowAccessPolicy2','AllowAccessPolicy3'):put(name)
        delete('AllowAccessPolicy1');delete('AllowAccessPolicy2');c('GetUserPolicy','AllowAccessPolicy3');delete('AllowAccessPolicy3')
    get_user()
    for user,name in sorted(writes):
        if user!=USER:continue
        # Cleanup is replayed even when a source-level Delete already succeeded.
        add('DeleteUserPolicy',{'UserName':USER,'PolicyName':name},code='ValidationError' if len(name)>128 else 'NoSuchEntity')
        if len(name)>128:add('ListUserPolicies',{'UserName':USER},{'PolicyNames':[],'IsTruncated':False})
    return plan


def verify(test,server,plan):
    test.assertFalse(server.fixtures);test.assertEqual(len(server.requests),len(plan))
    for request,(op,values,data,code) in zip(server.requests,plan):
        method,path,headers,body=request;query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query,keep_blank_values=True);test.assertEqual(query['Action'],[op])
        for key,value in values.items():test.assertEqual(query[key],[str(value)])
        test.assertIn('Credential=iam-tenant-real/',{k.lower():v for k,v in headers.items()}['authorization'])


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM SDK Query contracts')
class IamInlineSdkTest(unittest.TestCase):
    """Check real identity-specific Query serialization and original policy failure oracles."""
    def test_sixteen_sources_retain_all_policy_inputs_and_status_oracles(self):
        """Replay every complete inline source using a dedicated user and raw native SDK responses; expect unchanged fixed names, all four invalid documents, thousand-statement/150-character input and real cleanup after every source."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for mode in MODES:
                plan=inline_plan(mode)
                with self.subTest(language=language,mode=mode),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}));inline(p,mode);verify(self,server,plan)

    def test_existing_source_policy_is_not_overwritten_or_deleted(self):
        """Supply an existing source-fixed policy in the real paginated SDK output; expect not_run before any Put/Delete or per-policy Get, preserving the pre-existing resource."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=inline_plan('put_user_policy',existing=True)
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}))
                with self.assertRaises(CaseNotRun):inline(p,'put_user_policy')
                verify(self,server,plan)

    def test_unexpected_200_keeps_original_failure_and_cleans_attempted_policy(self):
        """Make the first original invalid document unexpectedly return HTTP200; expect the source assertion to fail and the actual attempted policy name to reach real DeleteUserPolicy in finally."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=inline_plan('put_user_policy_invalid_element',unexpected_success=True)
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True,'iam_alt_user_name':USER}))
                with self.assertRaises(AssertionError):inline(p,'put_user_policy_invalid_element')
                verify(self,server,plan)
