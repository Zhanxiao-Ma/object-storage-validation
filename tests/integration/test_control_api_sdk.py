"""Independent Query-protocol responses exercised through genuine IAM/STS SDK clients."""
from collections import deque
from contextlib import contextmanager, ExitStack
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from urllib.parse import parse_qs, quote
from osv.worker import Worker
from osv.secrets import secret_scope
from osv.cli import redactor
from sdk_fixtures import ROOT, FixtureHandler


def response(action, content):
    return (200, {'Content-Type':'text/xml'}, ('<'+action+'Response><'+action+'Result>'+content+'</'+action+'Result><ResponseMetadata><RequestId>independent-query-request</RequestId></ResponseMetadata></'+action+'Response>').encode())


@contextmanager
def control_worker(language, iam=(), sts=(), overrides=None):
    """Supply independent wire documents and separate ports, leaving serialization/signing to actual SDKs."""
    servers={};threads=[]
    for service,documents in [('s3',()),('iam',iam),('sts',sts)]:
        server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler);server.requests=[];server.fixtures=deque(documents)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers[service]=server;threads.append(thread)
    env={'OSV_ACCESS_KEY':'s3-unused-key','OSV_SECRET_KEY':'s3-unused-secret','OSV_SESSION_TOKEN':'','OSV_BUCKET':'unused',
        'OSV_ENDPOINT':f'http://127.0.0.1:{servers["s3"].server_port}','OSV_REGION':'us-east-1','OSV_PATH_STYLE':'true',
        'OSV_RETRIES':'0','OSV_CONNECTIONS':'16','AWS_EC2_METADATA_DISABLED':'true',
        'HTTP_PROXY':'','HTTPS_PROXY':'','http_proxy':'','https_proxy':'','ALL_PROXY':'','all_proxy':'','NO_PROXY':'127.0.0.1','no_proxy':'127.0.0.1'}
    for service in ['iam','sts']:
        env['OSV_'+service.upper()+'_ENDPOINT']=f'http://127.0.0.1:{servers[service].server_port}'
        env['OSV_'+service.upper()+'_REGION']='us-east-1'
    for identity in ['IAM','IAM_ROOT','IAM_ALT_ROOT']:
        env['OSV_'+identity+'_ACCESS_KEY']='independent-'+identity.lower()+'-key'
        env['OSV_'+identity+'_SECRET_KEY']='independent-'+identity.lower()+'-secret'
        env['OSV_'+identity+'_SESSION_TOKEN']=''
    env.update(overrides or {})
    command=['java','-Xmx256m','-jar',str(ROOT/'drivers/java/target/sdk-worker-0.1.0.jar'),'s3'] if language=='java' else [str(ROOT/'drivers/cpp/build/osv-cpp-s3')]
    worker=None
    try:
        worker=Worker({'command':command,'language':language,'api':'s3','env':env},15)
        yield worker,servers
    finally:
        if worker is not None:worker.close()
        for server in servers.values():server.shutdown();server.server_close()
        for thread in threads:thread.join()


@unittest.skipUnless(os.environ.get('OSV_RUN_CONTROL_API_TESTS')=='1','enable real IAM/STS official SDK wire checks')
class ControlApiSdkTest(unittest.TestCase):
    """Test actual model conversion, signer routing, presence and credential consumers without implementing Query services."""
    def languages(self):
        return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')

    def test_query_serializer_dates_presence_and_encoded_or_literal_policy_documents(self):
        """Feed independent IAM XML with encoded/plain policies, literal plus/percent and omitted fields; expect genuine Query serialization, exact parsed policy, epoch-millisecond dates with actual SDK precision, and absence preserved."""
        policy={'Version':'2012-10-17','Statement':[],'literal':'a+b%2Bc'};raw=json.dumps(policy,separators=(',',':'))
        docs=[response('GetUserPolicy','<UserName>u</UserName><PolicyName>p</PolicyName><PolicyDocument>'+quote(raw,safe='')+'</PolicyDocument>'),
            response('GetUserPolicy','<PolicyDocument>'+raw+'</PolicyDocument>'),
            response('GetRole','<Role><Path>/</Path><RoleName>r</RoleName><RoleId>id</RoleId><Arn>arn:aws:iam::123:role/r</Arn><CreateDate>2024-01-02T03:04:05.123Z</CreateDate><AssumeRolePolicyDocument>'+quote(raw,safe='')+'</AssumeRolePolicyDocument></Role>'),
            response('ListUserPolicies','<PolicyNames/><IsTruncated>false</IsTruncated>'),response('GetUser','')]
        for language in self.languages():
            with self.subTest(language=language),control_worker(language,docs) as (worker,servers):
                first=worker.call({'op':'api.iam.GetUserPolicy','identity':'iam_root','input':{'UserName':'u','PolicyName':'p'}})
                self.assertTrue(first['ok'],first);self.assertEqual(first['data'],{'UserName':'u','PolicyName':'p','PolicyDocument':policy});self.assertEqual(json.dumps(first['data']['PolicyDocument']),json.dumps(policy));self.assertEqual(first['status_code'],200)
                second=worker.call({'op':'api.iam.GetUserPolicy','input':{'UserName':'u','PolicyName':'p'}})
                self.assertTrue(second['ok'],second);self.assertEqual(second['data'],{'PolicyDocument':policy});self.assertEqual(json.dumps(second['data']['PolicyDocument']),json.dumps(policy))
                role=worker.call({'op':'api.iam.GetRole','input':{'RoleName':'r'}})
                self.assertTrue(role['ok'],role);self.assertEqual(role['data']['Role']['AssumeRolePolicyDocument'],policy);self.assertEqual(json.dumps(role['data']['Role']['AssumeRolePolicyDocument']),json.dumps(policy))
                # Official C++ IAM DateTime ISO_8601 parsing retains seconds for this
                # fractional wire timestamp; Java retains all three millisecond digits.
                # Preserve typed SDK output instead of reconstructing precision from XML.
                expected_date=1704164645123 if language=='java' else 1704164645000
                self.assertEqual(role['data']['Role']['CreateDate'],expected_date);self.assertNotIn('MaxSessionDuration',role['data']['Role']);self.assertNotIn('Description',role['data']['Role'])
                listed=worker.call({'op':'api.iam.ListUserPolicies','input':{'UserName':'u','MaxItems':1}})
                self.assertTrue(listed['ok'],listed);self.assertEqual(listed['data'],{'PolicyNames':[],'IsTruncated':False})
                absent=worker.call({'op':'api.iam.GetUser','input':{}});self.assertTrue(absent['ok'],absent);self.assertEqual(absent['data'],{})
                requests=servers['iam'].requests;self.assertEqual(len(requests),5);self.assertFalse(servers['iam'].fixtures)
                self.assertTrue(all(method=='POST' and path=='/' for method,path,_,_ in requests))
                query=parse_qs(requests[0][3].decode());self.assertEqual(query['Action'],['GetUserPolicy']);self.assertEqual(query['Version'],['2010-05-08']);self.assertEqual(query['UserName'],['u'])
                last_list=parse_qs(requests[3][3].decode());self.assertEqual(last_list['MaxItems'],['1'])
                headers={k.lower():v for k,v in requests[0][2].items()};self.assertIn('Credential=independent-iam_root-key/',headers['authorization']);self.assertIn('/us-east-1/iam/aws4_request',headers['authorization'])
                self.assertEqual(servers['s3'].requests,[]);self.assertEqual(servers['sts'].requests,[])

    def test_missing_control_prerequisites_and_invalid_input_never_fall_back_to_s3(self):
        """Remove service endpoint/region or identity credentials and supply malformed SDK fields; expect absent truthful identity capabilities and zero HTTP rather than main S3 credential fallback."""
        variants=[({'OSV_IAM_ENDPOINT':''},'api.iam.GetUser','identity_iam',{}),
            ({'OSV_IAM_REGION':''},'api.iam.GetUser','identity_iam',{}),
            ({'OSV_IAM_ACCESS_KEY':''},'api.iam.GetUser','identity_iam',{}),
            ({'OSV_IAM_SECRET_KEY':''},'api.iam.GetUser','identity_iam',{}),
            ({'OSV_STS_ENDPOINT':''},'api.sts.GetCallerIdentity','identity_sts_iam',{}),
            ({'OSV_STS_REGION':''},'api.sts.GetCallerIdentity','identity_sts_iam',{})]
        for language in self.languages():
            for env,operation,cap,input_value in variants:
                with self.subTest(language=language,missing=tuple(env)),control_worker(language,overrides=env) as (worker,servers):
                    self.assertNotIn(cap,worker.capabilities);result=worker.call({'op':operation,'input':input_value});self.assertFalse(result['ok'])
                    self.assertTrue(all(not server.requests for server in servers.values()))
            with control_worker(language) as (worker,servers):
                bad=[{'op':'api.iam.GetUser','identity':name,'input':{}} for name in ['main','anonymous','alt',False,1,'']]
                bad += [{'op':'api.iam.ListUsers','input':{'MaxItems':value}} for value in [True,1.5,2**63,'1',{},[]]]
                bad += [{'op':'api.iam.GetUser','input':{'NotAnOfficialField':1}},{'op':'api.iam.GetUser','input':[]},{'op':'api.iam.GetUser','input':{'UserName':False}},
                    {'op':'api.iam.GetUser','wire_headers':{},'input':{}}]
                for request in bad:
                    with self.subTest(language=language,request=request):self.assertFalse(worker.call(request)['ok'])
                self.assertTrue(all(not server.requests for server in servers.values()))

    def test_real_access_key_and_sts_session_outputs_feed_actual_next_sdk_signers(self):
        """Use actual CreateAccessKey and GetSessionToken parsed credentials as inputs to subsequent official STS clients; expect independent STS routing, actual signing keys/session token, preserved UTC expiry and complete dynamic redaction through a closure created before either credential producer."""
        key_response=response('CreateAccessKey','<AccessKey><UserName>fresh</UserName><AccessKeyId>fresh-real-key</AccessKeyId><SecretAccessKey>fresh-real-secret</SecretAccessKey><Status>Active</Status><CreateDate>2024-01-02T03:04:05Z</CreateDate></AccessKey>')
        session=response('GetSessionToken','<Credentials><AccessKeyId>temporary-real-key</AccessKeyId><SecretAccessKey>temporary-real-secret</SecretAccessKey><SessionToken>temporary-real-token</SessionToken><Expiration>2024-01-02T03:04:05Z</Expiration></Credentials>')
        caller=response('GetCallerIdentity','<UserId>actual-user</UserId><Account>123456789012</Account><Arn>arn:aws:iam::123456789012:user/fresh</Arn>')
        for language in self.languages():
            with self.subTest(language=language),secret_scope() as registry,ExitStack() as stack:
                scrub=redactor({})
                producer,first=stack.enter_context(control_worker(language,[key_response]));result=producer.call({'op':'api.iam.CreateAccessKey','identity':'iam_root','input':{'UserName':'fresh'}})
                self.assertTrue(result['ok'],result);key_result=result;key=result['data']['AccessKey'];self.assertIs(producer.secret_registry,registry)
                # Assert before any consumer environment can re-register these values.
                for issued in [key['AccessKeyId'],key['SecretAccessKey']]:self.assertNotIn(issued,scrub(json.dumps(key_result)))
                consumer,second=stack.enter_context(control_worker(language,sts=[session],overrides={'OSV_IAM_ACCESS_KEY':key['AccessKeyId'],'OSV_IAM_SECRET_KEY':key['SecretAccessKey']}))
                result=consumer.call({'op':'api.sts.GetSessionToken','input':{'DurationSeconds':900}});self.assertTrue(result['ok'],result);session_result=result;temporary=result['data']['Credentials'];self.assertIs(consumer.secret_registry,registry)
                for issued in [temporary['AccessKeyId'],temporary['SecretAccessKey'],temporary['SessionToken']]:self.assertNotIn(issued,scrub(json.dumps(session_result)))
                final,third=stack.enter_context(control_worker(language,sts=[caller,caller],overrides={'OSV_IAM_ACCESS_KEY':temporary['AccessKeyId'],'OSV_IAM_SECRET_KEY':temporary['SecretAccessKey'],'OSV_IAM_SESSION_TOKEN':temporary['SessionToken']}))
                result=final.call({'op':'api.sts.GetCallerIdentity','input':{}});self.assertTrue(result['ok'],result);self.assertEqual(result['data']['Account'],'123456789012')
                auth={k.lower():v for k,v in second['sts'].requests[0][2].items()};self.assertIn('Credential='+key['AccessKeyId']+'/',auth['authorization']);self.assertIn('/us-east-1/sts/aws4_request',auth['authorization'])
                auth={k.lower():v for k,v in third['sts'].requests[0][2].items()};self.assertIn('Credential='+temporary['AccessKeyId']+'/',auth['authorization']);self.assertEqual(auth['x-amz-security-token'],temporary['SessionToken'])
                self.assertEqual(temporary['Expiration'],1704164645000)
                self.assertIs(final.secret_registry,registry)
                fork=final.fork();stack.callback(fork.close);self.assertIs(fork.secret_registry,registry)
                fork_result=fork.call({'op':'api.sts.GetCallerIdentity','input':{}});self.assertTrue(fork_result['ok'],fork_result)
                actual_diagnostics=json.dumps([key_result,session_result,result,fork_result,second['sts'].requests,third['sts'].requests],default=lambda value:value.decode())
                cleaned=scrub(actual_diagnostics)
                for issued in [key['AccessKeyId'],key['SecretAccessKey'],temporary['AccessKeyId'],temporary['SecretAccessKey'],temporary['SessionToken']]:
                    self.assertIn(issued,actual_diagnostics);self.assertNotIn(issued,cleaned)
                self.assertIn('[REDACTED]',cleaned)
                for servers in [first,second,third]:self.assertEqual(servers['s3'].requests,[])

    def test_official_control_service_errors_preserve_status_code_and_error_name(self):
        """Feed actual IAM/STS Query error documents; expect official SDK exceptions retain 404 NoSuchEntity and 400 InvalidInput, including observed response headers."""
        iam_error=(404,{'Content-Type':'text/xml','x-amzn-requestid':'iam-error-request'},b'<ErrorResponse><Error><Type>Sender</Type><Code>NoSuchEntity</Code><Message>specified entity absent</Message></Error><RequestId>iam-error-request</RequestId></ErrorResponse>')
        sts_error=(400,{'Content-Type':'text/xml','x-amzn-requestid':'sts-error-request'},b'<ErrorResponse><Error><Type>Sender</Type><Code>InvalidInput</Code><Message>independent invalid input</Message></Error><RequestId>sts-error-request</RequestId></ErrorResponse>')
        for language in self.languages():
            with self.subTest(language=language),control_worker(language,[iam_error],[sts_error]) as (worker,servers):
                for request,status,code in [({'op':'api.iam.GetUser','input':{'UserName':'absent'}},404,'NoSuchEntity'),({'op':'api.sts.GetSessionToken','input':{'DurationSeconds':-1}},400,'InvalidInput')]:
                    result=worker.call(request);self.assertFalse(result['ok']);self.assertEqual(result.get('status_code'),status,result);self.assertEqual(result.get('error_code'),code,result)
                self.assertEqual(len(servers['iam'].requests),1);self.assertEqual(len(servers['sts'].requests),1);self.assertEqual(servers['s3'].requests,[])

    def test_all_fifty_five_operations_bind_real_sdk_request_types_and_query_actions(self):
        """Invoke every audited public operation using actual empty SDK models and independent service-rejection XML; expect 55 real Query requests, including all acronym spellings, rather than reflection or fallback failures."""
        iam='PutUserPolicy GetUserPolicy ListUserPolicies DeleteUserPolicy CreateUser GetUser UpdateUser DeleteUser ListUsers CreateAccessKey UpdateAccessKey DeleteAccessKey ListAccessKeys AttachUserPolicy DetachUserPolicy ListAttachedUserPolicies CreateGroup GetGroup UpdateGroup DeleteGroup ListGroups AddUserToGroup RemoveUserFromGroup ListGroupsForUser PutGroupPolicy GetGroupPolicy ListGroupPolicies DeleteGroupPolicy AttachGroupPolicy DetachGroupPolicy ListAttachedGroupPolicies CreateRole GetRole UpdateRole DeleteRole ListRoles PutRolePolicy GetRolePolicy ListRolePolicies DeleteRolePolicy AttachRolePolicy DetachRolePolicy ListAttachedRolePolicies CreateOpenIDConnectProvider GetOpenIDConnectProvider ListOpenIDConnectProviders DeleteOpenIDConnectProvider AddClientIDToOpenIDConnectProvider RemoveClientIDFromOpenIDConnectProvider UpdateOpenIDConnectProviderThumbprint GetAccountSummary'.split()
        sts='GetSessionToken AssumeRole AssumeRoleWithWebIdentity GetCallerIdentity'.split()
        error=(400,{'Content-Type':'text/xml'},b'<ErrorResponse><Error><Type>Sender</Type><Code>InvalidInput</Code><Message>independent request rejected</Message></Error><RequestId>binding-check</RequestId></ErrorResponse>')
        for language in self.languages():
            with self.subTest(language=language),control_worker(language,[error]*51,[error]*4) as (worker,servers):
                for service,operations in [('iam',iam),('sts',sts)]:
                    for operation in operations:
                        with self.subTest(language=language,operation=operation):
                            result=worker.call({'op':'api.'+service+'.'+operation,'input':{}})
                            self.assertFalse(result['ok']);self.assertEqual(result.get('status_code'),400,result);self.assertEqual(result.get('error_code'),'InvalidInput',result)
                    self.assertEqual(len(servers[service].requests),len(operations));self.assertFalse(servers[service].fixtures)
                    for operation,(method,path,headers,body) in zip(operations,servers[service].requests):
                        self.assertEqual(method,'POST');self.assertEqual(path,'/');self.assertEqual(parse_qs(body.decode())['Action'],[operation])
                self.assertEqual(servers['s3'].requests,[])

    def test_concurrent_first_use_shares_safe_identity_cache_and_reaches_real_query_transport(self):
        """Start sixteen first-use IAM requests through the same worker concurrency bridge; expect all actual SDK requests to finish under one identity, with policy dictionaries decoded in actual order even inside the batch result."""
        for language in self.languages():
            with self.subTest(language=language),control_worker(language,[response('GetRole','<Role><RoleName>r</RoleName><AssumeRolePolicyDocument>%7B%22Version%22%3A%222012-10-17%22%2C%22Statement%22%3A%5B%5D%7D</AssumeRolePolicyDocument></Role>')]*16) as (worker,servers):
                result=worker.call({'op':'concurrent_requests','requests':[{'op':'api.iam.GetRole','input':{'RoleName':'r'}} for _ in range(16)]})
                self.assertTrue(result['ok'],result);self.assertEqual(len(result['results']),16)
                for item in result['results']:
                    self.assertTrue(item['ok'],item);self.assertEqual(item['data'],{'Role':{'RoleName':'r','AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[]}}})
                    self.assertEqual(json.dumps(item['data']['Role']['AssumeRolePolicyDocument']),json.dumps({'Version':'2012-10-17','Statement':[]}))
                self.assertEqual(len(servers['iam'].requests),16);self.assertFalse(servers['iam'].fixtures);self.assertEqual(servers['s3'].requests,[])
