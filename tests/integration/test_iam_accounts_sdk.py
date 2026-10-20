"""Native IAM Query XML contracts; fixture replies are scripted raw inputs, never an IAM implementation."""
from collections import deque
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit,quote
import uuid
from xml.sax.saxutils import escape
from sdk_fixtures import fixture_worker,FixtureHandler
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun,run_cases
from osv.ceph_iam_accounts import SPECS
from osv.ceph_iam_scope import IamScope,pages

PREFIX='osv'+uuid.UUID(int=1).hex[:18]
PATH='/'+PREFIX+'/'
TRUST={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'sts:AssumeRole','Principal':{'AWS':'*'}}]}


def xml(value):
    if isinstance(value,dict) and set(value)=={'__raw_xml__'}:return value['__raw_xml__']
    if isinstance(value,dict):return ''.join('<'+k+'>'+xml(v)+'</'+k+'>' for k,v in value.items())
    if isinstance(value,list):return ''.join('<member>'+xml(v)+'</member>' for v in value)
    if isinstance(value,bool):return str(value).lower()
    return escape(str(value))


def response(op,data=None,code=None):
    if code:
        body='<ErrorResponse><Error><Type>Sender</Type><Code>'+code+'</Code><Message>scripted native IAM failure</Message></Error><RequestId>iam-raw-error</RequestId></ErrorResponse>'
        return (409 if code in ('EntityAlreadyExists','DeleteConflict') else 404 if code=='NoSuchEntity' else 403 if code=='AccessDenied' else 400,{'Content-Type':'text/xml'},body.encode())
    body='<'+op+'Response xmlns="https://iam.amazonaws.com/doc/2010-05-08/"><'+op+'Result>'+xml(data or {})+'</'+op+'Result><ResponseMetadata><RequestId>iam-raw-result</RequestId></ResponseMetadata></'+op+'Response>'
    return 200,{'Content-Type':'text/xml'},body.encode()


def entity(kind,name,path=PATH,uid='immutable-1',date='2025-01-01T00:00:00Z',**extra):
    value={'Path':path,kind+'Name':name,kind+'Id':uid,'Arn':'arn:aws:iam::123456789012:'+kind.lower()+path+name,'CreateDate':date,**extra}
    if kind=='Role':value['AssumeRolePolicyDocument']=quote(json.dumps(TRUST),safe='')
    return value


@contextmanager
def iam_fixture(language,plan,handler=FixtureHandler,sts_plan=None):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler);server.requests=[];server.fixtures=deque(response(op,data,code) for op,values,data,code in plan)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    environment={'OSV_IAM_ENDPOINT':'http://127.0.0.1:'+str(server.server_port),'OSV_IAM_REGION':'us-east-1','OSV_IAM_ROOT_ACCESS_KEY':'iam-root-real','OSV_IAM_ROOT_SECRET_KEY':'iam-root-real-secret','OSV_IAM_ACCESS_KEY':'iam-tenant-real','OSV_IAM_SECRET_KEY':'iam-tenant-real-secret','OSV_IAM_ALT_ROOT_ACCESS_KEY':'iam-alt-root-real','OSV_IAM_ALT_ROOT_SECRET_KEY':'iam-alt-root-real-secret','OSV_ALT_ACCESS_KEY':'s3-alt-real','OSV_ALT_SECRET_KEY':'s3-alt-real-secret'}
    sts_server=None
    if sts_plan is not None:
        sts_server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler);sts_server.requests=[]
        sts_server.fixtures=deque((status,headers,payload.replace(b'https://iam.amazonaws.com/doc/2010-05-08/',b'https://sts.amazonaws.com/doc/2011-06-15/')) for status,headers,payload in (response(op,data,code) for op,values,data,code in sts_plan))
        sts_thread=threading.Thread(target=sts_server.serve_forever,daemon=True);sts_thread.start();server.sts=sts_server
        environment.update({'OSV_STS_ENDPOINT':'http://127.0.0.1:'+str(sts_server.server_port),'OSV_STS_REGION':'us-east-1'})
    try:
        with fixture_worker(language,[],'s3',environment,handler=handler) as (worker,unused):
            server.s3=unused
            yield worker,server
    finally:
        server.shutdown();server.server_close();thread.join()
        if sts_server is not None:sts_server.shutdown();sts_server.server_close();sts_thread.join()


def verify_requests(test,server,plan,identities=None):
    test.assertFalse(server.fixtures);test.assertEqual(len(server.requests),len(plan))
    for index,(request,(op,values,data,code)) in enumerate(zip(server.requests,plan)):
        method,path,headers,body=request;query=parse_qs(body.decode() if method=='POST' else urlsplit(path).query,keep_blank_values=True)
        test.assertEqual(query['Action'],[op]);test.assertEqual(query['Version'],['2010-05-08'])
        for key,value in values.items():test.assertEqual(query[key],[str(value)])
        auth={k.lower():v for k,v in headers.items()}['authorization'];test.assertIn('Credential='+(identities or {}).get(index,'iam-root-real')+'/',auth);test.assertIn('/us-east-1/iam/aws4_request',auth)


def account_plan(kind,mode):
    plan=[];owned=[]
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    def create(name,path=PATH,uid='immutable-1',date='2025-01-01T00:00:00Z',**extra):
        item=entity(kind,name,path,uid,date,**extra);values={kind+'Name':name,'Path':path}
        if kind=='Role':values['AssumeRolePolicyDocument']=json.dumps(TRUST)
        values.update(extra);add('Create'+kind,values,{kind:item});owned.append(item);return item
    def get(name,item):add('Get'+kind,{kind+'Name':name},{kind:item})
    def delete(name):add('Delete'+kind,{kind+'Name':name})
    def no(op,name):add(op,{kind+'Name':name},code='NoSuchEntity')
    def listing(prefix,items,page=False):
        if not page:add('List'+kind+'s',{'PathPrefix':prefix},{kind+'s':items,'IsTruncated':False});return
        for index,item in enumerate(items):
            values={'PathPrefix':prefix,'MaxItems':1}
            if index:values['Marker']='cursor-'+str(index)
            data={kind+'s':[item],'IsTruncated':index<len(items)-1}
            if index<len(items)-1:data['Marker']='cursor-'+str(index+1)
            add('List'+kind+'s',values,data)
    a=PREFIX+('U1' if kind=='User' else 'R1')
    if mode=='create':
        extra={} if kind=='User' else {'Description':'my role description','MaxSessionDuration':43200};first=create(a,**extra)
        if kind=='Role':get(a,first)
        values={kind+'Name':a,'Path':PATH+'foo/'}
        if kind=='Role':values['AssumeRolePolicyDocument']=json.dumps(TRUST)
        add('Create'+kind,values,code='EntityAlreadyExists');create(PREFIX+('U2' if kind=='User' else 'R2'),PATH+'foo/',uid='immutable-2')
    elif mode=='case_insensitive_name':
        first=create(a);lower=PREFIX+('u1' if kind=='User' else 'r1');values={kind+'Name':lower}
        if kind=='Role':values['AssumeRolePolicyDocument']=json.dumps(TRUST)
        add('Create'+kind,values,code='EntityAlreadyExists');get(lower,first);delete(lower);no('Get'+kind,lower);owned=[];no('Get'+kind,a)
    elif mode=='delete':
        a=PREFIX+'U1';no('Delete'+kind,a);first=create(a);delete(a);second=create(a,uid='immutable-2',date='2025-01-01T00:00:01Z');owned=[second]
        # The scope retains both immutable IDs, and queries both actual ownership records.
        get(a,second);delete(a);no('Get'+kind,a);return plan
    elif mode in ('list','list_path_prefix'):
        add('List'+kind+'s',{'PathPrefix':PATH},{kind+'s':[],'IsTruncated':False})
        suffixes=('a','b','c','d') if mode=='list_path_prefix' else ('aa','Ab','ac','Ad')
        ns=[PREFIX+x for x in suffixes];items=[]
        if mode=='list':
            for index,(name,suffix) in enumerate(zip(reversed(ns),('w/','x/','y/','z/'))):items.append(create(name,PATH+suffix,uid='id-'+str(index)))
            ordered=list(reversed(items));listing(PATH,ordered);listing(PATH,ordered,True)
        else:
            for index,(name,suffix) in enumerate(zip(ns,('','','a/','a/x/'))):items.append(create(name,PATH+suffix,uid='id-'+str(index)))
            for prefix,rows in (('',items),('a',items[2:]),('a/x',items[3:])):listing(PATH+prefix,rows);listing(PATH+prefix,rows,True)
            listing(PATH+'a/x/d',[])
    elif mode=='update' and kind=='Role':
        a=PREFIX+'a';no('UpdateRole',a);first=create(a);get(a,first);add('UpdateRole',{'RoleName':a,'Description':'my role description','MaxSessionDuration':43200});get(a,{**first,'Description':'my role description','MaxSessionDuration':43200})
    elif mode in ('update_name','update_path'):
        a,b,z=[PREFIX+x for x in ('a','b','z')]
        if mode=='update_name':add('UpdateUser',{'UserName':a,'NewUserName':z},code='NoSuchEntity')
        first=create(a);second=create(b,PATH+'m/',uid='immutable-2');listing(PATH,[first,second]);get(a,first)
        values={'UserName':a,'NewPath':PATH+'z/'} if mode=='update_path' else {'UserName':a,'NewUserName':z};add('UpdateUser',values)
        changed=entity('User',a if mode=='update_path' else z,PATH+'z/' if mode=='update_path' else PATH)
        if mode=='update_name':no('GetUser',a)
        get(changed['UserName'],changed);listing(PATH,[changed,second] if mode=='update_path' else [second,changed]);owned=[changed,second]
    for item in reversed(owned):get(item[kind+'Name'],item);delete(item[kind+'Name'])
    return plan


def key_plan(mode):
    plan=[];name=PREFIX+'a';fake='abcdefghijklmnopqrstu';item=entity('User',name);created=[]
    def add(op,values=None,data=None,code=None):plan.append((op,values or {},data,code))
    add('GetUser',data={'User':{'Arn':'arn:aws:iam::123456789012:root'}})
    if mode=='create':add('CreateAccessKey',{'UserName':name},code='NoSuchEntity')
    else:
        add('GetUser',{'UserName':'nosuchuser'},code='NoSuchEntity');values={'UserName':'nosuchuser'}
        if mode!='list':values['AccessKeyId']=fake
        if mode=='update':values['Status']='Active'
        add({'update':'UpdateAccessKey','delete':'DeleteAccessKey','list':'ListAccessKeys'}[mode],values,code='NoSuchEntity')
    add('CreateUser',{'UserName':name,'Path':PATH},{'User':item})
    def listing(keys,page=False):
        if not page or not keys:
            add('ListAccessKeys',{'UserName':name,**({'MaxItems':1} if page else {})},{'AccessKeyMetadata':keys,'IsTruncated':False});return
        for index,key in enumerate(keys):
            values={'UserName':name,'MaxItems':1}
            if index:values['Marker']='next-key'
            add('ListAccessKeys',values,{'AccessKeyMetadata':[key],'IsTruncated':index<len(keys)-1,**({'Marker':'next-key'} if index<len(keys)-1 else {})})
    def key(index):
        value={'UserName':name,'AccessKeyId':'AKIAFRESHKEY'+str(index),'SecretAccessKey':'actual-raw-native-secret-'+str(index),'Status':'Active','CreateDate':'2025-01-01T00:00:00Z'};created.append(value)
        add('CreateAccessKey',{'UserName':name},{'AccessKey':value});return value
    if mode=='list':
        for page in (False,True):listing([],page)
        first=key(1)
        for page in (False,True):listing([first],page)
        second=key(2)
        # Access key source intentionally sorts both final two-key results.
        for page in (False,True):listing([second,first],page)
    else:
        if mode=='delete':add('DeleteAccessKey',{'UserName':name,'AccessKeyId':fake},code='NoSuchEntity')
        first=key(1);values={'UserName':name,'AccessKeyId':first['AccessKeyId']}
        if mode=='update':
            add('UpdateAccessKey',{'UserName':name,'AccessKeyId':fake,'Status':'Active'},code='NoSuchEntity')
            for status in ('Active','Inactive'):add('UpdateAccessKey',{**values,'Status':status})
            listing([{**first,'Status':'Inactive'}])
        elif mode=='delete':
            add('DeleteAccessKey',values);add('DeleteAccessKey',values,code='NoSuchEntity');listing([])
    for value in reversed(created):
        add('GetUser',{'UserName':name},{'User':item});add('DeleteAccessKey',{'UserName':name,'AccessKeyId':value['AccessKeyId']},code='NoSuchEntity' if mode=='delete' else None)
    add('GetUser',{'UserName':name},{'User':item});add('DeleteUser',{'UserName':name})
    return plan


@unittest.skipUnless(os.environ.get('OSV_RUN_IAM_ACCOUNT_TESTS')=='1','enable real IAM SDK Query contracts')
class IamAccountsSdkTest(unittest.TestCase):
    """Run native producer XML directly through the migrated scenarios and their actual scoped cleanup."""
    def test_thirteen_user_role_sources_preserve_original_vectors_and_actual_pagination(self):
        """Replay thirteen complete original user/role sources on both official SDKs; expect unmodified status/code/ID/date/order checks and actual Marker/MaxItems requests signed as IAM root."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for reference,execute in SPECS:
                if 'access_key' in reference:continue
                kind='User' if '_user_' in reference else 'Role';mode=reference.split('_'+kind.lower()+'_',1)[1];plan=account_plan(kind,mode)
                with self.subTest(language=language,reference=reference),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);probe=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):execute(probe)
                    verify_requests(self,server,plan)

    def test_all_thirty_three_default_sources_are_not_run_without_control_plane_identities(self):
        """Run all actual source entries on a real SDK worker with explicitly absent IAM transport/credentials; expect exactly 33 not_run results and no HTTP, avoiding fake cloud passes for missing prerequisites."""
        from osv.ceph_iam_accounts import CASES as account_cases
        from osv.ceph_iam_inline import CASES as inline_cases
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3',{'OSV_IAM_ENDPOINT':'','OSV_IAM_REGION':'','OSV_IAM_ACCESS_KEY':'','OSV_IAM_SECRET_KEY':'','OSV_IAM_ROOT_ACCESS_KEY':'','OSV_IAM_ROOT_SECRET_KEY':''}) as (worker,server):
                d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'out',{}),account_cases+inline_cases)
                self.assertEqual(len(rows),33);self.assertEqual({row['status'] for row in rows},{'not_run'});self.assertEqual(server.requests,[])

    def test_authorization_and_nonroot_identity_stop_mutations(self):
        """Use actual native GetUser XML with a nonroot ARN; expect missing explicit consent to perform no HTTP and nonroot credentials to stop before any mutation."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:user/not-root'}},None)]
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{})
                with self.assertRaises(CaseNotRun):
                    with IamScope(Probe(ctx)):pass
                self.assertEqual(server.requests,[]);ctx.workload['iam_mutation_authorized']=True
                with self.assertRaises(CaseNotRun):
                    with IamScope(Probe(ctx)):pass
                verify_requests(self,server,plan)

    def test_four_fresh_user_key_sources_preserve_real_ids_dates_and_pagination(self):
        """Feed real CreateAccessKey XML to all four fresh-user key sources and cleanup; expect Active/Inactive and unchanged epoch dates, exact negative names, real page cursors and deletion of only returned fresh IDs."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for reference,execute in SPECS:
                if 'access_key' not in reference:continue
                mode=reference.rsplit('_',1)[1];plan=key_plan(mode)
                with self.subTest(language=language,reference=reference),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);probe=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):execute(probe)
                    verify_requests(self,server,plan)

    def test_repeated_real_marker_fails_without_local_sort_or_infinite_loop(self):
        """Supply repeated actual SDK pagination markers; expect the production paginator to reject the boundary after two real requests rather than synthesize rows or silently stop."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            plan=[('ListUsers',{'PathPrefix':PATH},{'Users':[],'IsTruncated':True,'Marker':'same'},None),('ListUsers',{'PathPrefix':PATH,'Marker':'same'},{'Users':[],'IsTruncated':True,'Marker':'same'},None)]
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);probe=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{}))
                with self.assertRaisesRegex(AssertionError,'repeated Marker'):pages(probe,'ListUsers','Users',PathPrefix=PATH)
                verify_requests(self,server,plan)

    def test_key_cleanup_checks_actual_user_id_before_touching_a_replacement(self):
        """Return the same username with a different immutable ID during finally; expect the original deliberate failure and zero DeleteAccessKey/DeleteUser requests against the replacement."""
        name=PREFIX+'a';first=entity('User',name);replacement=entity('User',name,uid='somebody-elses-new-id')
        plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:root'}},None),('CreateUser',{'UserName':name,'Path':PATH},{'User':first},None),('CreateAccessKey',{'UserName':name},{'AccessKey':{'AccessKeyId':'ACTUALRETURNEDKEY','SecretAccessKey':'actual-dynamic-secret'}},None),('GetUser',{'UserName':name},{'User':replacement},None),('GetUser',{'UserName':name},{'User':replacement},None)]
        class StaticOwnershipXml(FixtureHandler):
            """Route static independent XML by actual query Action; never model resources, ownership checks or cleanup behavior."""
            def respond(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')));query=parse_qs(body.decode() if self.command=='POST' else urlsplit(self.path).query);op=query['Action'][0]
                self.server.requests.append((self.command,self.path,dict(self.headers),body))
                data=({'User':first} if op=='CreateUser' else {'AccessKey':{'AccessKeyId':'ACTUALRETURNEDKEY','SecretAccessKey':'actual-dynamic-secret'}} if op=='CreateAccessKey' else {'User':replacement} if op=='GetUser' and 'UserName' in query else {'User':{'Arn':'arn:aws:iam::123456789012:root'}} if op=='GetUser' else {})
                status,headers,payload=response(op,data);self.send_response(status)
                for k,v in headers.items():self.send_header(k,v)
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
            do_GET=do_POST=respond
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,[],handler=StaticOwnershipXml) as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                with self.assertRaisesRegex(AssertionError,'independent source failure'),patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                    with IamScope(p) as scope:
                        scope.create('User',name,PATH);scope.call('CreateAccessKey',UserName=name);raise AssertionError('independent source failure')
                verify_requests(self,server,plan)

    def test_mid_source_failure_still_deletes_returned_key_of_actual_owned_user(self):
        """Fail immediately after the real SDK returns a fresh key; expect actual returned ID to reach DeleteAccessKey only after a matching UID GetUser and user cleanup to complete without hiding the source failure."""
        name=PREFIX+'a';first=entity('User',name)
        plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:root'}},None),('CreateUser',{'UserName':name,'Path':PATH},{'User':first},None),('CreateAccessKey',{'UserName':name},{'AccessKey':{'AccessKeyId':'ACTUALRETURNEDKEY','SecretAccessKey':'actual-dynamic-secret'}},None),('GetUser',{'UserName':name},{'User':first},None),('DeleteAccessKey',{'UserName':name,'AccessKeyId':'ACTUALRETURNEDKEY'},None,None),('GetUser',{'UserName':name},{'User':first},None),('DeleteUser',{'UserName':name},None,None)]
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                with self.assertRaisesRegex(AssertionError,'independent source failure'),patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                    with IamScope(p) as scope:
                        scope.create('User',name,PATH);scope.call('CreateAccessKey',UserName=name);raise AssertionError('independent source failure')
                verify_requests(self,server,plan)

    def test_mismatched_or_missing_creation_identity_never_claims_an_unrelated_resource(self):
        """Return another user's name or an empty immutable ID in real CreateUser XML; expect ownership failure without any Get/Delete targeting that unowned response identity."""
        name=PREFIX+'a'
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            for item in (entity('User','existing-unrelated-user'),entity('User',name,uid='')):
                plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::123456789012:root'}},None),('CreateUser',{'UserName':name,'Path':PATH},{'User':item},None)]
                with self.subTest(language=language,returned=item['UserName'],uid=item['UserId']),tempfile.TemporaryDirectory() as temporary,iam_fixture(language,plan) as (worker,server):
                    d=Path(temporary);p=Probe(Context(worker,'unused/',d,b'',d/'body',d/'out',{'iam_mutation_authorized':True}))
                    with self.assertRaisesRegex(AssertionError,'cannot prove requested resource ownership'),patch('osv.ceph_iam_scope.uuid.uuid4',return_value=uuid.UUID(int=1)):
                        with IamScope(p) as scope:scope.create('User',name,PATH)
                    verify_requests(self,server,plan)
