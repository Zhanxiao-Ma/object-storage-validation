"""Actual SDK logging account-wildcard and live policy-transition regressions."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.ceph_logging_policy_transitions import CASES,wildcard,permission_change
from sdk_fixtures import fixture_worker
from test_logging_workloads_sdk import listing,log_line

OK=(200,{},b'');DEL=(204,{},b'');NO=(404,{},b'');EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
NAMES=[uuid.UUID(int=n) for n in (1,2,3)]

def denied(code='AccessDenied'):return 403,{},('<Error><Code>'+code+'</Code><Message>Logging bucket permission denied</Message></Error>').encode()
def account_workload(objects):
    sequence=[NO,OK]*3+[DEL,OK,OK,DEL,denied(),DEL,OK,OK]
    if objects:
        body='\n'.join(log_line('osv-'+NAMES[0 if part==1 else 2].hex,'REST.PUT.OBJECT','myobject_'+str(part)+'_'+str(index)) for index in range(5) for part in (1,2)).encode()
        sequence += [OK]*11+[(200,{},listing(['log/record'])),(200,{},body),DEL]+[OK]*5+[denied(),(200,{},listing(['log/record']))]
    sequence += [OK]*3+[DEL]*3
    sequence += [OK]+[DEL]*(6 if objects else 0)+[(200,{},EMPTY),DEL,NO]
    sequence += [OK,(200,{},listing(['log/record']) if objects else EMPTY)]+([DEL] if objects else [])+[DEL,NO]
    sequence += [OK]+[DEL]*(6 if objects else 0)+[(200,{},EMPTY),DEL,NO]
    return sequence

def changing_workload(unexpected=None):
    sequence=[NO,OK]*2+[DEL,OK]+[OK]*5+[DEL]+[OK]*5+[OK if unexpected=='success' else denied('InvalidRequest' if unexpected=='wrong_code' else 'AccessDenied')]
    if unexpected is None:sequence += [(200,{},EMPTY)]
    sequence += [OK]*2+[DEL]*2+[OK,(200,{},EMPTY),DEL,NO]+[OK]+[DEL]*11+[(200,{},EMPTY),DEL,NO]
    return sequence

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_POLICY_TRANSITION_TESTS')=='1','enable real SDK logging policy transitions')
class LoggingPolicyTransitionsSdkTest(unittest.TestCase):
    """Consume independent policy/error/log fixtures through actual Java/C++ SDK clients and explicit alternate identities."""
    def execute_wildcard(self,language,objects):
        with tempfile.TemporaryDirectory() as temporary,fixture_worker(language,account_workload(objects),'s3',{'OSV_ALT_ACCESS_KEY':'genuine-alt','OSV_ALT_SECRET_KEY':'genuine-alt-secret'}) as (worker,server):
            d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'main-user','canonical_id':'different-owner'},'alt':{'user_id':'alt-user','canonical_id':'different-alt-owner'}}})
            started=time.monotonic()
            with patch('osv.bucket_scope.uuid.uuid4',side_effect=NAMES):wildcard(Probe(ctx),objects)
            if objects:self.assertGreaterEqual(time.monotonic()-started,10.8)
            self.assertFalse(server.fixtures);self.assertEqual(len(server.requests),66 if objects else 32)
            policies=[json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path]
            self.assertEqual(policies[0]['Statement'][0]['Condition']['StringLike']['aws:SourceAccount'],'main-user');self.assertEqual(policies[0]['Statement'][1]['Condition']['StringLike']['aws:SourceAccount'],'alt-user')
            self.assertEqual(len(policies[1]['Statement']),1);self.assertEqual(policies[2]['Statement'][0]['Condition']['StringLike']['aws:SourceAccount'],'*');self.assertEqual(policies[2]['Statement'][0]['Condition']['ArnLike']['aws:SourceArn'],'arn:aws:s3:::*')
            for method,path,headers,body in server.requests:
                if 'osv-'+NAMES[2].hex in path:self.assertIn('Credential=genuine-alt/',{key.lower():value for key,value in headers.items()}['authorization'])
            if objects:self.assertEqual(policies[3]['Statement'][0]['Condition']['StringLike']['aws:SourceAccount'],'main-user')

    def test_wildcard_policy_two_accounts_and_complete_object_vectors(self):
        """Exercise both original wildcard sources on both real SDKs; expect exact two-user policy mutation inputs, genuine alt signatures for every alt-owned request, ten mixed log records and no extra log after denied alt flush."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for objects in (False,True):
                with self.subTest(language=language,objects=objects):self.execute_wildcard(language,objects)

    def test_standard_permission_change_preserves_all_ten_puts_and_denied_real_dummy(self):
        """Keep Standard writes successful after invalid SourceArn and reject only the original real 5.5-second flush dummy; expect AccessDenied, zero target log objects and complete cleanup."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,changing_workload(),'s3') as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'main-user'}}})
                started=time.monotonic();permission_change(Probe(ctx),False);self.assertGreaterEqual(time.monotonic()-started,5.4);self.assertFalse(server.fixtures);self.assertEqual(len(server.requests),42)
                policies=[json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path];self.assertEqual(policies[1]['Statement'][0]['Condition']['ArnLike']['aws:SourceArn'],'arn:aws:s3:::kaboom')
                self.assertEqual(sum(method=='PUT' and '/myobject' in path for method,path,headers,body in server.requests),10);self.assertEqual(server.requests[17][3],b'dummy')

    def test_unexpected_success_or_wrong_flush_error_keeps_source_failure_and_cleanup(self):
        """Let the denied flush unexpectedly succeed or return another real service code; expect the original AccessDenied oracle to fail and all written/ambiguous keys still removed."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for wrong in ('success','wrong_code'):
                with self.subTest(language=language,error=wrong),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,changing_workload(wrong),'s3') as (worker,server):
                    d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'main-user'}}})
                    with self.assertRaises(AssertionError):permission_change(Probe(ctx),False)
                    self.assertFalse(server.fixtures);self.assertEqual(sum(method=='DELETE' and '/myobject' in path for method,path,headers,body in server.requests),10)

    def test_default_four_sources_and_journal_model_requirements_never_fall_back(self):
        """Execute all four sources without IDs/alt metadata and call Journal directly with a real stock SDK; expect all not_run and original missing native extension/POST prerequisites before any HTTP or fallback identity."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{})
                rows=run_cases(ctx,CASES);self.assertEqual(len(rows),4);self.assertEqual({row['status'] for row in rows},{'not_run'})
                with self.assertRaises(CaseNotRun) as missing:permission_change(Probe(ctx),True)
                self.assertIn('ceph_bucket_logging_extensions',missing.exception.requirements);self.assertIn('api.PostBucketLogging',missing.exception.requirements)
                ctx.workload['identities']={'main':{'user_id':'main-user'},'alt':{'canonical_id':'alt-canonical-only'}}
                with self.assertRaises(CaseNotRun) as missing:wildcard(Probe(ctx),False)
                self.assertIn('workload.identities.alt.user_id',missing.exception.requirements)
                ctx.workload['identities']['alt']['user_id']='real-alt-user'
                with self.assertRaises(CaseNotRun) as missing:wildcard(Probe(ctx),False)
                self.assertIn('identity_alt',missing.exception.requirements);self.assertEqual(server.requests,[])
