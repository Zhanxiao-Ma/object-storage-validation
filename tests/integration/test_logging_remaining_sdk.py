"""Independent raw IAM/STS/S3 documents validate complete standard logging source paths."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from osv.ceph_logging_remaining import execute
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from test_iam_accounts_sdk import iam_fixture,verify_requests,entity
from test_logging_workloads_sdk import listing,log_line,EMPTY,OK,ABSENT,DELETED

PREFIX='osv'+uuid.UUID(int=1).hex[:18];ROLE=PREFIX+'p2';SESSION=PREFIX+'p1'
ROLE_ARN='arn:aws:iam::123456789012:role/'+ROLE
TEMP={'AccessKeyId':'LOG_ACTUAL_RETURNED_ACCESS','SecretAccessKey':'log-actual-returned-secret','SessionToken':'log-actual-returned-token','Expiration':'2028-01-01T00:00:00Z'}
WORK={'identities':{'main':{'user_id':'independent-main-user'},'alt':{'user_id':'independent-alt-user'}},'iam_mutation_authorized':True}

def name(n):return 'osv-'+uuid.UUID(int=n).hex

def cleanup(keys,discovered=()):
    return [OK]+[DELETED]*len(keys)+[(200,{},listing(discovered))]+[DELETED]*len(discovered)+[DELETED,ABSENT]

def vectors(mode,bad_requester=False):
    """Provide raw predetermined service responses; never implement logging, authorization or the production runtime."""
    iam=[];sts=[];s3=[]
    if mode=='account':
        for account in ('111111111111','222222222222','333333333333'):iam.append(('GetUser',{}, {'User':{'Arn':'arn:aws:iam::'+account+':root'}},None))
        for source in (name(2),name(6),name(10)):
            s3 += [ABSENT,OK,ABSENT,OK,DELETED,OK]+[OK]*5+[(200,{},listing(['myobject'+str(i) for i in range(5)])),OK,(200,{},listing(['log/account-record'])),(200,{},'\n'.join(log_line(source,'REST.PUT.OBJECT','myobject'+str(i)) for i in range(5)).encode())]
        s3 += [OK]*6+[DELETED]*6
        for _ in range(3):s3 += cleanup([],['log/account-record'])+cleanup(['dummy',*['myobject'+str(i) for i in range(5)]])
    else:
        role=entity('Role',ROLE,'/',uid='immutable-log-role');role['Arn']=ROLE_ARN
        trust=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':['arn:aws:iam:::user/independent-alt-user']},'Action':['sts:AssumeRole']}]})
        policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'s3:*','Resource':'*'}]})
        iam=[('GetRole',{'RoleName':ROLE},None,'NoSuchEntity'),('CreateRole',{'Path':'/','RoleName':ROLE,'AssumeRolePolicyDocument':trust},{'Role':role},None),('PutRolePolicy',{'RoleName':ROLE,'PolicyName':'S3FullAccess','PolicyDocument':policy},{},None),('GetRole',{'RoleName':ROLE},{'Role':role},None),('DeleteRolePolicy',{'RoleName':ROLE,'PolicyName':'S3FullAccess'},{},None),('DeleteRole',{'RoleName':ROLE},{},None),('GetRole',{'RoleName':ROLE},None,'NoSuchEntity')]
        sts=[('AssumeRole',{'RoleArn':ROLE_ARN,'RoleSessionName':SESSION},{'Credentials':TEMP},None)]
        requester='arn:aws:sts::123456789012:assumed-role/'+ROLE+'/'+('wrong-session' if bad_requester else SESSION)
        body=log_line(name(2),'REST.PUT.OBJECT','my-test-object').replace(' requester ',' '+requester+' ')
        s3=[ABSENT,OK,ABSENT,OK,DELETED,OK,OK,OK,(200,{},listing(['log/role-record'])),(200,{},body.encode())]+[OK]*2+[DELETED]*2+cleanup([],['log/role-record'])+cleanup(['dummy','my-test-object'])
    return iam,sts,s3

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_REMAINING_TESTS')=='1','enable real SDK remaining logging fixtures')
class LoggingRemainingSdkTest(unittest.TestCase):
    """Exercise complete pinned source bodies, actual producer credentials/accounts and owned cleanup through official Java/C++ SDKs."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def run_source(self,language,mode,bad=False):
        iam,sts,s3=vectors(mode,bad);started=time.monotonic()
        with tempfile.TemporaryDirectory() as directory,iam_fixture(language,iam,sts_plan=sts if mode=='requester' else None) as (worker,server):
            server.s3.fixtures.extend(s3);d=Path(directory);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'output',WORK)
            with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,40)]):
                if bad:
                    with self.assertRaisesRegex(AssertionError,'Requester missing'):execute(Probe(ctx),mode)
                else:self.assertEqual(execute(Probe(ctx),mode)['actual_model_rejections'],3 if mode=='account' else 1)
            self.assertGreaterEqual(time.monotonic()-started,16.4 if mode=='account' else 5.4)
            self.assertFalse(server.s3.fixtures)
            verify_requests(self,server,iam,identities={i:'iam-tenant-real' for i in range(len(iam))} if mode=='requester' else None)
            policies=[json.loads(body) for method,path,headers,body in server.s3.requests if method=='PUT' and 'policy' in path]
            self.assertEqual([p['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'] for p in policies],['independent-main-user','222222222222','333333333333'] if mode=='account' else ['independent-alt-user'])
            if mode=='requester':
                self.assertFalse(server.sts.fixtures);self.assertEqual(len(server.sts.requests),1)
                query=parse_qs(server.sts.requests[0][3].decode());self.assertEqual(query['RoleArn'],[ROLE_ARN]);self.assertEqual(query['RoleSessionName'],[SESSION])
                self.assertIn('Credential=s3-alt-real/',{k.lower():v for k,v in server.sts.requests[0][2].items()}['authorization'])
                for method,path,headers,body in server.s3.requests:
                    lowered={k.lower():v for k,v in headers.items()}
                    self.assertIn('Credential='+TEMP['AccessKeyId']+'/',lowered['authorization']);self.assertEqual(lowered['x-amz-security-token'],TEMP['SessionToken'])
            else:
                # All real requests to each owned bucket retain the original source identity.
                expected={name(2):'local-test',name(3):'iam-root-real',name(6):'iam-root-real',name(7):'iam-root-real',name(10):'iam-root-real',name(11):'local-test'}
                for method,path,headers,body in server.s3.requests:
                    bucket=urlsplit(path).path.split('/')[1];self.assertIn('Credential='+expected[bucket]+'/',{k.lower():v for k,v in headers.items()}['authorization'])
            self.assertEqual(sum(method=='PUT' and path.endswith('/dummy') for method,path,headers,body in server.s3.requests),3 if mode=='account' else 1)

    def test_all_three_original_account_vectors_use_actual_get_user_arns(self):
        """Run all three cross-account source vectors with original five objects and real 5.5s waits; expect distinct actual IAM ARN accounts in real policies and all six buckets deleted."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'account')

    def test_actual_alt_sts_credentials_sign_source_and_log_cleanup(self):
        """Feed real IAM CreateRole output into alt STS and its returned keys/token into real S3; expect original Requester ARN assertions and cleanup before role permissions are revoked."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'requester')

    def test_original_requester_mismatch_fails_and_still_cleans_role_and_buckets(self):
        """Return an independent wrong session in the actual downloaded log; expect the original strict substring assertion to fail while real owned bucket/policy/role cleanup completes."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'requester',True)

    def test_missing_account_metadata_is_not_run_before_any_sdk_resource_request(self):
        """Omit independently configured user identity; expect explicit prerequisite rejection before any S3/IAM request, never a gate-only pass."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,iam_fixture(language,[]) as (worker,server):
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'output',{})
                with self.assertRaises(CaseNotRun):execute(Probe(ctx),'account')
                self.assertEqual(server.requests,[]);self.assertEqual(server.s3.requests,[])

    def test_nonroot_actual_get_user_arn_prevents_bucket_creation(self):
        """Return an actual IAM user ARN rather than root to the ownership prerequisite; expect not_run and zero S3 requests, preventing mutation of an unintended identity's buckets."""
        plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:user/unexpected-user'}},None)]
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,iam_fixture(language,plan) as (worker,server):
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'output',WORK)
                with self.assertRaises(CaseNotRun):execute(Probe(ctx),'account')
                verify_requests(self,server,plan);self.assertEqual(server.s3.requests,[])


    def test_default_missing_control_identities_are_exact_not_run_with_zero_http(self):
        """Run both standard logging sources on actual S3-only SDK workers; expect exact missing root versus alt/IAM identities, no resource mutation and zero HTTP."""
        from sdk_fixtures import fixture_worker
        from osv.ceph_logging_remaining import CASES
        from osv.suites import run_cases
        expected={'ceph_logging_remaining_account_s':['identity_iam_root'],'ceph_logging_remaining_assumed_requester':['identity_alt','identity_iam']}
        missing={key:'' for key in ('OSV_IAM_ENDPOINT','OSV_IAM_REGION','OSV_IAM_ACCESS_KEY','OSV_IAM_SECRET_KEY','OSV_IAM_ROOT_ACCESS_KEY','OSV_IAM_ROOT_SECRET_KEY','OSV_ALT_ACCESS_KEY','OSV_ALT_SECRET_KEY')}
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3',missing) as (worker,server):
                d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'output',{}),CASES)
                self.assertEqual(len(rows),2)
                for row in rows:
                    self.assertEqual(row['status'],'not_run');self.assertEqual(row['reason'],'unsupported_operations');self.assertEqual(row['missing_operations'],expected[row['case']])
                self.assertEqual(server.requests,[])
