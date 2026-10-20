"""Independent original-policy golden inputs and raw IAM/STS/S3 documents exercise all 27 source consumers."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs,quote,urlsplit,unquote
from xml.etree import ElementTree as ET
from osv.ceph_sts_web import execute,SPECS,CASES
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun,run_cases
from test_iam_accounts_sdk import iam_fixture,verify_requests

GOLDEN=json.loads((Path(__file__).parent/'fixtures/sts_web_policy_vectors.json').read_text())['vectors']
LINES=(403,502,571,628,690,747,809,870,931,1007,1081,1162,1214,1288,1337,1387,1436,1485,1534,1585,1634,1683,1742,1801,1863,1927,2018)
PREFIX='osv'+uuid.UUID(int=1).hex[:18];ROLE=PREFIX+'p2';POLICY=PREFIX+'p3';SESSION=PREFIX+'p1'
URL='http://localhost:8080/auth/realms/fixture-realm';OIDC='arn:aws:iam:::oidc-provider/localhost:8080/auth/realms/fixture-realm';ROLE_ARN='arn:aws:iam:::role/'+ROLE
TOKEN='opaque-wire-fixture-input';USER_TOKEN='opaque-user-wire-fixture-input'
TEMP={'AccessKeyId':'WEB_RETURNED_ACCESS','SecretAccessKey':'web-returned-secret','SessionToken':'web-returned-session-token','Expiration':'2028-01-01T00:00:00Z'}
ENV={'OSV_ALT_ACCESS_KEY':'web-alt-key','OSV_ALT_SECRET_KEY':'web-alt-secret','OSV_WEB_IDENTITY_TOKEN':TOKEN,'OSV_WEB_IDENTITY_USER_TOKEN':USER_TOKEN}
WORK={'iam_mutation_authorized':True,'ceph_literal_bucket_names_authorized':True,'sts_web_identity':{'realm':'fixture-realm','thumbprint':'d'*40,'aud':'fixture-app','sub':'fixture-sub','azp':'fixture-azp'}}


def ok(status=200,body=b'',headers=None):return status,headers or {},body

def denied(code='AccessDenied',status=403):return status,{'Content-Type':'application/xml'},('<Error><Code>'+code+'</Code><Message>independent source rejection</Message></Error>').encode()


def plans(line,wrong_error=False):
    """Supply predetermined source outcomes; this fixture never evaluates any authorization policy."""
    v=GOLDEN[str(line)];iam=[];sts=[];s3=[];checks=[]
    def i(op,values=None,data=None,code=None):iam.append((op,values or {},data,code))
    role={'RoleName':ROLE,'RoleId':'immutable-web-role','Arn':ROLE_ARN,'Path':'/','AssumeRolePolicyDocument':quote(v['policy_document'],safe='')}
    provider={'Url':URL,'ThumbprintList':['d'*40],'ClientIDList':[]}
    i('ListOpenIDConnectProviders',data={'OpenIDConnectProviderList':[]})
    i('CreateOpenIDConnectProvider',{'Url':URL,'ThumbprintList.member.1':'d'*40},{'OpenIDConnectProviderArn':OIDC})
    i('GetRole',{'RoleName':ROLE},code='NoSuchEntity')
    role_values={'RoleName':ROLE,'Path':'/','AssumeRolePolicyDocument':v['policy_document']}
    for index,tag in enumerate(v.get('tags_list',[]),1):
        role_values['Tags.member.'+str(index)+'.Key']=tag['Key'];role_values['Tags.member.'+str(index)+'.Value']=tag['Value']
    i('CreateRole',role_values,{'Role':role})
    policy=v.get('role_policy_new',v.get('role_policy'))
    if policy is not None:i('PutRolePolicy',{'RoleName':ROLE,'PolicyName':POLICY,'PolicyDocument':policy})
    values={'RoleArn':ROLE_ARN,'RoleSessionName':SESSION,'WebIdentityToken':USER_TOKEN if line>=1387 else TOKEN}
    if 'session_policy' in v:values['Policy']=v['session_policy']
    sts.append(('AssumeRoleWithWebIdentity',values,{'Credentials':TEMP},None))
    def provider_delete():
        i('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},provider)
        i('DeleteOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC})
        i('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},code='NoSuchEntity')
    if not wrong_error:provider_delete()
    i('GetRole',{'RoleName':ROLE},{'Role':role})
    if policy is not None:i('DeleteRolePolicy',{'RoleName':ROLE,'PolicyName':POLICY})
    i('DeleteRole',{'RoleName':ROLE});i('GetRole',{'RoleName':ROLE},code='NoSuchEntity')
    if wrong_error:provider_delete()
    def add(method,bucket,key='',result=None,identity='owner',body=None,tags=None):
        s3.append(result or ok());checks.append((method,bucket,key,identity,body,tags))
    def head(name,status=404):add('HEAD',name,result=ok(status))
    def create(name,identity='owner',reject=False):add('PUT',name,result=denied('WrongIndependentCode' if wrong_error else 'AccessDenied') if reject else ok(),identity=identity)
    def put(name,reject=False,missing=False,tags=None):
        body=b'please-write-something' if line==502 else b'this is a test file'
        add('PUT',name,'test-1.txt',denied('NoSuchBucket',404) if missing else denied('WrongIndependentCode' if wrong_error else 'AccessDenied') if reject else ok(),identity='temporary',body=body,tags=tags)
    def get(name,key='test-1.txt',reject=False):add('GET',name,key,denied('WrongIndependentCode' if wrong_error else 'AccessDenied') if reject else ok(body=b'this is a test file'),identity='temporary')
    def copy(name,target,target_key):
        add('HEAD',name,'test-1.txt',ok(headers={'Content-Length':str(len(b'this is a test file'))}),identity='temporary')
        add('PUT',target,target_key,ok(body=b'<CopyObjectResult><ETag>"copied"</ETag><LastModified>2025-01-01T00:00:00Z</LastModified></CopyObjectResult>'),identity='temporary')
    def cleanup(name,keys):
        for key in sorted(keys):add('DELETE',name,key,ok(204))
        add('DELETE',name,result=ok(204));head(name)
    first='osv-'+uuid.UUID(int=2).hex;second='osv-'+uuid.UUID(int=3).hex
    if line==502:
        head('test1');head('test2');create('test1','temporary',True)
        if not wrong_error:create('test2','temporary',True);put('test1',missing=True)
    elif 571<=line<=1214:
        head('test1');create('test1')
        if 'bucket_policy' in v:add('PUT','test1','?policy',ok(),body=v['bucket_policy'].encode())
        reject=line in (628,747,809,870,1162,1214);put('test1',reject=reject)
        keys=['test-1.txt']
        if line==931:get('test1',reject=True)
        elif line==1007:get('test1')
        elif line==1081:copy('test1','test1','test-2.txt');get('test1','test-2.txt');keys.append('test-2.txt')
        cleanup('test1',keys)
    elif line in (1683,1742,1801,1863,1927,2018):
        head(first);create(first)
        if line!=1742:
            values=['WrongResourcetag'] if line==1801 else ['Engineering','Marketing'] if line in (1683,2018) else ['Engineering']
            add('PUT',first,'?tagging',ok(),tags=values)
        if line==1927:head(second);create(second);add('PUT',second,'?tagging',ok(),tags=['Engineering'])
        put(first,reject=line in (1742,1801),tags=v.get('tags'))
        if line==1863:get(first)
        if line==1927:copy(first,first,'test-2.txt');get(first,'test-2.txt');copy(first,second,'test-1.txt');get(second)
        if line==1927:cleanup(second,['test-1.txt'])
        cleanup(first,['test-1.txt','test-2.txt'] if line==1927 else ['test-1.txt'])
    else:
        head(first);create(first,'temporary',line==1534)
        if line!=1534:add('DELETE',first,result=ok(204),identity='temporary');head(first)
    return iam,sts,s3,checks


def run_source(test,language,line,wrong_error=False):
    name,_,needs,literals=next(spec for spec in SPECS if spec[1]==line);iam,sts,s3,checks=plans(line,wrong_error)
    with patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
        server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
        with patch('osv.ceph_sts_web_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=n) for n in range(1,10)]):
            if wrong_error:
                with test.assertRaises(AssertionError):execute(p,name,needs,literals)
            else:execute(p,name,needs,literals)
        verify_requests(test,server,iam,{index:'iam-tenant-real' for index in range(len(iam))})
        test.assertFalse(server.sts.fixtures);test.assertEqual(len(server.sts.requests),1)
        query=parse_qs(server.sts.requests[0][3].decode());test.assertEqual(query['Action'],['AssumeRoleWithWebIdentity'])
        for key,value in sts[0][1].items():test.assertEqual(query[key],[value])
        # Keep the pinned SDK's actual behavior: Java signs with the source's
        # configured alt key even though the newer model documents noAuth.
        sts_headers={key.lower():value for key,value in server.sts.requests[0][2].items()}
        if 'authorization' in sts_headers:
            test.assertIn('/us-east-1/sts/aws4_request',sts_headers['authorization']);test.assertIn('Credential=s3-alt-real/',sts_headers['authorization'])
        test.assertFalse(server.s3.fixtures);test.assertEqual(len(server.s3.requests),len(checks))
        for actual,expected in zip(server.s3.requests,checks):
            method,path,headers,body=actual;verb,bucket,key,identity,expected_body,tags=expected
            test.assertEqual(method,verb);parsed=urlsplit(path);test.assertEqual(unquote(parsed.path),'/' +bucket+('/'+key if key and not key.startswith('?') else ''))
            if key.startswith('?'):test.assertIn(key[1:],parse_qs(parsed.query,keep_blank_values=True))
            lower={k.lower():v for k,v in headers.items()};test.assertIn('Credential='+('WEB_RETURNED_ACCESS' if identity=='temporary' else 'iam-tenant-real')+'/',lower['authorization'])
            if identity=='temporary':test.assertEqual(lower['x-amz-security-token'],TEMP['SessionToken'])
            if expected_body is not None:test.assertEqual(body,expected_body)
            if isinstance(tags,str):test.assertEqual(lower.get('x-amz-tagging'),tags)
            elif tags is not None:
                values=[node.text for node in ET.fromstring(body).iter() if node.tag.split('}')[-1]=='Value'];test.assertEqual(values,tags)


@unittest.skipUnless(os.environ.get('OSV_RUN_STS_WEB_TESTS')=='1','enable real IAM/STS/S3 source wire fixtures; external IdP authentication is not simulated')
class StsWebSdkTest(unittest.TestCase):
    """Verify every source branch's request/response plumbing using actual SDKs and independent service documents, not a local authorization evaluator."""
    def test_all_twenty_seven_sources_preserve_full_policies_and_real_temporary_credential_consumers(self):
        """Run all 27 complete original bodies with independent source service outcomes; expect exact policy bytes, claims/tags, parameter order, original success/denial assertions and actual STS credentials in every S3 consumer and cleanup."""
        self.assertEqual(tuple(spec[1] for spec in SPECS),LINES)
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for line in LINES:
                with self.subTest(language=language,source=line):run_source(self,language,line)

    def test_independent_wrong_denial_code_fails_original_assertions_and_still_cleans_owned_resources(self):
        """Return a different real service error to each source PUT/GET/create-denial branch; expect the untouched original error-code assertions to fail while actual role/provider/bucket cleanup completes."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for line in (502,628,747,809,870,931,1162,1214,1534,1742,1801):
                with self.subTest(language=language,source=line):run_source(self,language,line,True)

    def test_missing_external_oidc_configuration_and_tokens_never_start_source_mutations(self):
        """Omit actual issuer/token configuration; all 27 original bodies must truthfully be not_run with zero IAM, STS and S3 HTTP, rather than manufacturing a JWT or skipping source steps as a pass."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,[],sts_plan=[]) as (worker,server):
                d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'out',{}),CASES)
                self.assertEqual(len(rows),27);self.assertEqual({r['status'] for r in rows},{'not_run'})
                for workload,environment,cases in [(WORK,{'OSV_WEB_IDENTITY_TOKEN':'','OSV_WEB_IDENTITY_USER_TOKEN':''},CASES),({**WORK,'iam_mutation_authorized':False},{},CASES),({**WORK,'ceph_literal_bucket_names_authorized':False},{},(CASES[1],))]:
                    with patch.dict(os.environ,environment):
                        guarded=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'out',workload),cases)
                        self.assertEqual({r['status'] for r in guarded},{'not_run'})
                self.assertEqual(server.requests,[]);self.assertEqual(server.sts.requests,[]);self.assertEqual(server.s3.requests,[])

    def test_existing_fixed_provider_and_literal_buckets_are_never_adopted_or_deleted(self):
        """Offer an existing fixed issuer ARN or literal test1 bucket; expect explicit not_run before role/provider/bucket mutations and no deletion of either external resource."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for line,iam,s3 in [(403,[('ListOpenIDConnectProviders',{}, {'OpenIDConnectProviderList':[{'Arn':OIDC}]},None)],[]),(571,[],[ok(200)])]:
                name,_,needs,literals=next(s for s in SPECS if s[1]==line)
                with self.subTest(language=language,source=line),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=[]) as (worker,server):
                    server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                    with self.assertRaises(CaseNotRun):execute(p,name,needs,literals)
                    self.assertEqual(len(server.requests),len(iam));self.assertEqual(len(server.s3.requests),len(s3));self.assertFalse(server.sts.requests)
                    self.assertTrue(all(row[0]=='HEAD' for row in server.s3.requests))
                    self.assertTrue(all(parse_qs(row[3].decode())['Action']==['ListOpenIDConnectProviders'] for row in server.requests))

    def test_actual_changed_role_id_stops_cleanup_before_mutating_that_identity(self):
        """Return a different immutable ID from the real GetRole cleanup producer; expect refusal before DeleteRolePolicy/DeleteRole, preserving the replacement identity even after the full source succeeds."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            iam,sts,s3,checks=plans(403);index=next(i for i,item in enumerate(iam) if item[0]=='GetRole' and item[2] is not None)
            item=iam[index];iam=iam[:index]+[(item[0],item[1],{'Role':{**item[2]['Role'],'RoleId':'external-replacement-id'}},None)]
            name,_,needs,literals=SPECS[0]
            with self.subTest(language=language),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
                server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                with patch('osv.ceph_sts_web_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=n) for n in range(1,10)]),self.assertRaisesRegex(RuntimeError,'Role immutable ID changed'):
                    execute(p,name,needs,literals)
                verify_requests(self,server,iam,{i:'iam-tenant-real' for i in range(len(iam))})
                actions=[parse_qs(row[3].decode())['Action'][0] for row in server.requests]
                self.assertNotIn('DeleteRole',actions);self.assertNotIn('DeleteRolePolicy',actions);self.assertFalse(server.s3.fixtures)

    def test_failed_create_keeps_original_helper_bug_and_cleans_actual_recovered_owned_identity(self):
        """Reject CreateRole then independently expose either absence or a committed fresh role through actual GetRole; preserve original KeyError('Code') while cleaning only the recovered matching UUID/path/immutable ID and owned provider."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for committed in (False,True):
                baseline,_,_,_=plans(403);iam=baseline[:3];create=baseline[3];iam.append((create[0],create[1],None,'InvalidInput'))
                role=create[2]['Role']
                iam.append(('GetRole',{'RoleName':ROLE},{'Role':role} if committed else None,None if committed else 'NoSuchEntity'))
                if committed:iam.extend([('GetRole',{'RoleName':ROLE},{'Role':role},None),('DeleteRole',{'RoleName':ROLE},None,None),('GetRole',{'RoleName':ROLE},None,'NoSuchEntity')])
                iam.extend([('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},{'Url':URL},None),('DeleteOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,None),('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,'NoSuchEntity')])
                name,_,needs,literals=SPECS[0]
                with self.subTest(language=language,committed=committed),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=[]) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                    with patch('osv.ceph_sts_web_runtime.uuid.uuid4',return_value=uuid.UUID(int=1)),self.assertRaisesRegex(KeyError,'Code'):
                        execute(p,name,needs,literals)
                    verify_requests(self,server,iam,{i:'iam-tenant-real' for i in range(len(iam))});self.assertFalse(server.s3.requests);self.assertFalse(server.sts.requests)

    def test_failed_provider_create_recovers_only_new_actual_arn_with_matching_url(self):
        """After an actual CreateProvider error, feed independent List/Get outputs for a new ARN; cleanup may delete only when its real URL matches, and must refuse a changed external URL."""
        from osv.ceph_sts_web_runtime import ClientError
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for matches in (True,False):
                baseline,_,_,_=plans(403);iam=baseline[:1]+[(baseline[1][0],baseline[1][1],None,'InvalidInput')]
                iam.append(('ListOpenIDConnectProviders',{}, {'OpenIDConnectProviderList':[{'Arn':OIDC}]},None))
                iam.append(('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},{'Url':URL if matches else 'http://external-existing.invalid'},None))
                if matches:iam.extend([('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},{'Url':URL},None),('DeleteOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,None),('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,'NoSuchEntity')])
                name,_,needs,literals=SPECS[0]
                with self.subTest(language=language,url_matches=matches),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=[]) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                    with patch('osv.ceph_sts_web_runtime.uuid.uuid4',return_value=uuid.UUID(int=1)):
                        if matches:
                            with self.assertRaises(ClientError):execute(p,name,needs,literals)
                        else:
                            with self.assertRaisesRegex(RuntimeError,'Ambiguous provider URL changed'):execute(p,name,needs,literals)
                    verify_requests(self,server,iam,{i:'iam-tenant-real' for i in range(len(iam))});self.assertFalse(server.s3.requests);self.assertFalse(server.sts.requests)

    def test_original_oidc_and_inline_policy_helper_error_key_bugs_are_not_silently_fixed(self):
        """Reject the original helper's provider or inline-policy write through real SDK errors; expect untouched top-level Code KeyErrors and cleanup of only actual newly owned resources, tolerating an actually absent failed inline policy."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for operation in ('provider','policy'):
                if operation=='provider':
                    base,_,_,_=plans(502);iam=base[:1]+[(base[1][0],base[1][1],None,'InvalidInput'),('ListOpenIDConnectProviders',{}, {'OpenIDConnectProviderList':[]},None)];s3=[ok(404),ok(404)];spec=next(s for s in SPECS if s[1]==502)
                else:
                    base,_,_,_=plans(403);iam=base[:4]+[(base[4][0],base[4][1],None,'InvalidInput')];role=base[3][2]['Role']
                    iam.extend([('GetRole',{'RoleName':ROLE},{'Role':role},None),('DeleteRolePolicy',{'RoleName':ROLE,'PolicyName':POLICY},None,'NoSuchEntity'),('DeleteRole',{'RoleName':ROLE},None,None),('GetRole',{'RoleName':ROLE},None,'NoSuchEntity'),('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},{'Url':URL},None),('DeleteOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,None),('GetOpenIDConnectProvider',{'OpenIDConnectProviderArn':OIDC},None,'NoSuchEntity')]);s3=[];spec=SPECS[0]
                name,_,needs,literals=spec
                with self.subTest(language=language,source_helper=operation),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=[]) as (worker,server):
                    server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                    with patch('osv.ceph_sts_web_runtime.uuid.uuid4',return_value=uuid.UUID(int=1)),self.assertRaisesRegex(KeyError,'Code'):execute(p,name,needs,literals)
                    verify_requests(self,server,iam,{i:'iam-tenant-real' for i in range(len(iam))});self.assertFalse(server.s3.fixtures);self.assertFalse(server.sts.requests)

    def test_unexpected_success_preserves_original_unassigned_error_variable_bug_and_cleanup(self):
        """Return success to original PUT-denial source628; expect its actual UnboundLocalError rather than a rewritten error oracle, while every created role/provider/bucket still gets actual SDK cleanup."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            iam,sts,s3,checks=plans(628,True)
            position=next(i for i,item in enumerate(checks) if item[0]=='PUT' and item[2]=='test-1.txt');s3[position]=ok(200)
            name,_,needs,literals=next(s for s in SPECS if s[1]==628)
            with self.subTest(language=language),patch.dict(os.environ,ENV),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,iam,sts_plan=sts) as (worker,server):
                server.s3.fixtures.extend(s3);d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',WORK))
                with patch('osv.ceph_sts_web_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=n) for n in range(1,10)]),self.assertRaises(UnboundLocalError):execute(p,name,needs,literals)
                verify_requests(self,server,iam,{i:'iam-tenant-real' for i in range(len(iam))});self.assertFalse(server.s3.fixtures);self.assertFalse(server.sts.fixtures)
