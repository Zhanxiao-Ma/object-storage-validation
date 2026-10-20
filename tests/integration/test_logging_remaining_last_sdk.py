"""Independent namespace and governance XML vectors exercise the final two logging source bodies through actual SDKs."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from osv.ceph_logging_remaining_last import execute
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from sdk_fixtures import fixture_worker
from test_logging_remaining_extensions_sdk import WORK,PROBE,flushed,journal,name
from test_logging_workloads_sdk import OK,ABSENT,DELETED,EMPTY,listing
from test_logging_remaining_sdk import cleanup

TENANT='independent-tenant';TENANT_USER='independent-tenant-user'
LAST_WORK={**WORK,'tenant_name':TENANT,'identities':{**WORK['identities'],'tenant':{'user_id':TENANT_USER}},'object_lock_cleanup_authorized':True}
VEMPTY=(200,{},b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>')
VERSION='actual+object/version=&';PROOF_VERSION='actual+short/proof=&'
VERSIONS=(200,{},('<ListVersionsResult><IsTruncated>false</IsTruncated><Version><Key>myobject</Key><VersionId>'+escape(VERSION)+'</VersionId><IsLatest>true</IsLatest></Version></ListVersionsResult>').encode())


def tenant_vectors():
    fixtures=[];actors=[];scopes=[]
    def add(result=OK,actor='main'):fixtures.append(result);actors.append(actor)
    def probe():add(PROBE)
    probe()
    vectors=[(4,5,'main','tenant','qualified',True),(8,8,'main','tenant','qualified',True),(11,12,'main','tenant','bare',False),(13,14,'tenant','tenant','qualified',True),(17,18,'tenant','tenant','bare',True),(21,22,'tenant','main','default',True),(25,26,'tenant','main','bare',False)]
    for src,dst,owner,logger,address,valid in vectors:
        for n,actor,log in [(src,owner,False),(dst,logger,True)]:
            add(ABSENT,actor);add(OK,actor);scopes.append((n,actor,log,valid))
        add(DELETED,logger)
        if not valid:
            add((404,{},b'<Error><Code>NoSuchKey</Code><Message>Independent wrong namespace target</Message></Error>'),owner);continue
        add(OK,owner)
        for _ in range(5):add(OK,owner)
        add((200,{},listing(['myobject'+str(i) for i in range(5)])),owner);probe();add(flushed('log/tenant'),owner);add((200,{},listing(['log/tenant'])),logger);add((200,{},'\n'.join(journal(name(src),'REST.PUT.OBJECT','myobject'+str(i)) for i in range(5)).encode()),logger)
    for n,actor,log,valid in scopes:add(OK,actor)
    for n,actor,log,valid in scopes:add(DELETED,actor)
    for n,actor,log,valid in reversed(scopes):
        for response in cleanup([] if log or not valid else ['myobject'+str(i) for i in range(5)],['log/tenant'] if log and valid else []):add(response,actor)
    return fixtures,actors,vectors


def meta_vector(wrong_version=False,proof_failure=False):
    source=name(4)
    out=[PROBE,ABSENT,OK,(200,{'x-amz-version-id':PROOF_VERSION},b'')]
    if proof_failure:
        out += [(403,{},b'<Error><Code>AccessDenied</Code></Error>'),DELETED,OK,DELETED,OK,VEMPTY,DELETED,ABSENT]
        return out
    out += [OK,DELETED,ABSENT,OK,DELETED,OK,(200,{'x-amz-version-id':VERSION},b''),OK,OK,DELETED,OK,OK,PROBE,flushed('log/metadata'),(200,{},listing(['log/metadata']))]
    events=['REST.PUT.OBJECT','REST.PUT.ACL','REST.PUT.LEGAL_HOLD','REST.PUT.RETENTION','REST.PUT.OBJECT_TAGGING','REST.DELETE.OBJECT_TAGGING']
    body='\n'.join('owner '+source+' [20/Oct/2026:00:00:00 +0000] '+event+' myobject 3 '+('unrelated-version' if wrong_version and i==0 else VERSION)+' etag' for i,event in enumerate(events))
    out += [(200,{},body.encode())]
    if not wrong_version:out += [OK,DELETED]
    out += [OK]*2+[DELETED]*2+cleanup([],['log/metadata'])+[OK]
    if wrong_version:out += [VERSIONS,OK,DELETED]
    out += [VEMPTY,DELETED,ABSENT]
    return out

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_REMAINING_LAST_TESTS')=='1','enable actual SDK final logging source fixtures')
class LoggingRemainingLastSdkTest(unittest.TestCase):
    """Verify complete original namespace/version producers and safe cleanup consumers with raw independent replies, without emulating tenant permissions or lock semantics."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def run_source(self,language,mode,fixtures,work=LAST_WORK,failure=None):
        with tempfile.TemporaryDirectory() as directory,fixture_worker(language,fixtures,'s3',{'OSV_TENANT_ACCESS_KEY':'tenant-real-key','OSV_TENANT_SECRET_KEY':'tenant-real-secret'}) as (worker,server):
            d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',work)
            with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,100)]):
                if failure:
                    with self.assertRaises(failure):execute(Probe(ctx),mode)
                else:self.assertEqual(execute(Probe(ctx),mode)['protocol_adapter'],'ceph_logging_wire_extensions')
            self.assertFalse(server.fixtures)
            for method,path,headers,body in server.requests:
                if method=='PUT' and 'logging' in path and body:ET.fromstring(body)
            return list(server.requests)

    def test_complete_journal_tenant_source_preserves_all_seven_namespace_vectors(self):
        """Run all seven original namespace vectors, including same bare names under separate real credentials and both NoSuchKey negatives; expect five complete Journal log readbacks, exact policy/source accounts and owner-specific cleanup."""
        for language in self.languages():
            with self.subTest(language=language):
                fixtures,actors,vectors=tenant_vectors();rows=self.run_source(language,'tenant',fixtures)
                self.assertEqual(len(rows),len(actors))
                for row,actor in zip(rows,actors):self.assertIn('Credential='+('tenant-real-key' if actor=='tenant' else 'local-test')+'/',{k.lower():v for k,v in row[2].items()}['authorization'])
                targets=[]
                for method,path,headers,body in rows:
                    if method=='PUT' and 'logging' in path and body:
                        node=ET.fromstring(body)
                        for target in node.iter():
                            if target.tag.rsplit('}',1)[-1]=='TargetBucket' and target.text and target.text not in [name(i) for i in (3,7,10,16,20,24)]:targets.append(target.text)
                self.assertEqual(targets,[TENANT+':'+name(5),TENANT+':'+name(8),name(12),TENANT+':'+name(14),name(18),':'+name(22),name(26)])
                policies=[json.loads(body) for method,path,headers,body in rows if method=='PUT' and 'policy' in path]
                self.assertEqual([p['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'] for p in policies],[WORK['identities']['main']['user_id']]*3+[TENANT_USER]*4)

    def test_original_locked_metadata_version_drives_all_apis_and_six_log_events(self):
        """Feed actual locked PutObject VersionId to original ACL/tag/delete and retrieved Journal version assertions; expect actual same-key short proof before60-day retention, exact six events, hold OFF and bypass delete."""
        for language in self.languages():
            with self.subTest(language=language):
                before=time.time();rows=self.run_source(language,'metadata',meta_vector());after=time.time()
                retains=[]
                for method,path,headers,body in rows:
                    query=parse_qs(urlsplit(path).query,keep_blank_values=True);low={k.lower():v for k,v in headers.items()}
                    if method=='PUT' and 'retention' in query:
                        root=ET.fromstring(body);date=next(n.text for n in root.iter() if n.tag.rsplit('}',1)[-1]=='RetainUntilDate');retains.append(date)
                    if method=='DELETE' and 'tagging' not in query and query.get('versionId') in ([VERSION],[PROOF_VERSION]):self.assertEqual(low['x-amz-bypass-governance-retention'],'true')
                    if 'acl' in query or 'tagging' in query:self.assertEqual(query.get('versionId'),[VERSION])
                    if query.get('versionId')==[VERSION]:self.assertIn(method,('PUT','DELETE'))
                from datetime import datetime
                self.assertEqual(len(retains),2);short,long=[datetime.fromisoformat(t.replace('Z','+00:00')).timestamp() for t in retains]
                self.assertTrue(before+1<=short<=after+3);self.assertTrue(before+60*86400-2<=long<=after+60*86400+2)

    def test_unrelated_actual_log_version_fails_and_lock_cleanup_uses_listed_version(self):
        """Return an unrelated downloaded Journal VersionID; expect original assertion failure before its normal cleanup, then actual version listing→hold OFF→bypass deletion still removes the real retained source version."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'metadata',meta_vector(wrong_version=True),failure=AssertionError)

    def test_failed_same_key_governance_proof_expires_and_not_runs_before_long_retention(self):
        """Deny actual short probe retention; expect original key cleaned after its real two-second expiry, explicit not_run, no legal hold or60-day retention and safe locked-bucket deletion."""
        for language in self.languages():
            with self.subTest(language=language):
                rows=self.run_source(language,'metadata',meta_vector(proof_failure=True),failure=CaseNotRun)
                self.assertFalse(any('legal-hold' in path for method,path,headers,body in rows))
                deletes=[{k.lower():v for k,v in headers.items()} for method,path,headers,body in rows if method=='DELETE' and 'versionId=' in path]
                self.assertEqual(len(deletes),1);self.assertNotIn('x-amz-bypass-governance-retention',deletes[0])

    def test_missing_tenant_and_cleanup_authorization_are_not_run_before_any_request(self):
        """Omit actual tenant identity metadata or explicit governance cleanup authorization; expect precise not_run before probes or bucket creation, never a gated false success."""
        for language in self.languages():
            for mode,work in [('tenant',WORK),('metadata',WORK)]:
                with self.subTest(language=language,mode=mode):self.assertEqual(self.run_source(language,mode,[],work,failure=CaseNotRun),[])
