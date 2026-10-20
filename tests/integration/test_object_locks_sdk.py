"""Actual SDK fixtures for source object-lock contracts and safe cleanup boundaries."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime,timezone
from osv.scenarios import Context
from osv.suites import Probe,run_cases,CaseNotRun
from osv.worker import Worker
from osv.ceph_object_locks import CASES,LockScope,retained,legal,retention_delete,metadata,multi_delete,multipart_hold,change_mode,invalid_bucket,DATE2030,DATE2030_3,DATE2140
from sdk_fixtures import fixture_worker

EMPTY=b'<ListVersionsResult><IsTruncated>false</IsTruncated></ListVersionsResult>'
VERSION=b'<ListVersionsResult><IsTruncated>false</IsTruncated><Version><Key>file1</Key><VersionId>real-version-2</VersionId><IsLatest>true</IsLatest></Version></ListVersionsResult>'
OK=(200,{},b'')
CREATED=[(404,{},b''),OK]
PROBE=[(200,{'x-amz-version-id':'probe-version'},b''),OK,(204,{},b'')]
CLEAN=[OK,(200,{},EMPTY),(204,{},b''),(404,{},b'')]

def error(status,code):return status,{},('<Error><Code>'+code+'</Code><Message>independent fixture</Message></Error>').encode()
def version(ident='real-version-2'):return 200,{'x-amz-version-id':ident},b''
def retention_xml(date):return 200,{},('<Retention><Mode>GOVERNANCE</Mode><RetainUntilDate>'+datetime.fromtimestamp(date/1000,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')+'</RetainUntilDate></Retention>').encode()
def ctx(worker,d):return Context(worker,'unused/',d,b'independent',d/'body',d/'output',{'object_lock_cleanup_authorized':True})

@unittest.skipUnless(os.environ.get('OSV_RUN_OBJECT_LOCK_TESTS')=='1','enable actual SDK object lock fixtures')
class ObjectLocksSdkTest(unittest.TestCase):
    """Use raw independently specified XML and real signatures, producer version IDs and cleanup requests."""
    @classmethod
    def setUpClass(cls):
        """Reuse local service and built SDK commands; expect no real cloud credentials or endpoints."""
        import test_s3_sdk
        test_s3_sdk.SdkIntegrationTest.setUpClass.__func__(cls)

    def test_retention_source_vectors_preserve_dates_errors_versions_and_bypass(self):
        """Exercise all nine original retention vectors through typed SDK models after real short cleanup proof; expect exact 2030/2140 dates, invalid modes, actual version ID and bypass headers."""
        for target in self.targets:
            for mode in ('put','invalid_mode','get','iso','version','override','increase','shorten','shorten_bypass'):
                sequence=CREATED+PROBE
                if mode=='override':sequence += [OK]
                if mode=='version':sequence += [version('real-version-1')]
                sequence += [version()]
                if mode=='invalid_mode':sequence += [error(400,'MalformedXML')]*2
                else:
                    sequence += [OK]
                    if mode in ('increase','shorten','shorten_bypass'):sequence += [error(403,'AccessDenied') if mode=='shorten' else OK]
                    if mode=='iso':sequence += [(200,{'x-amz-object-lock-retain-until-date':'2030-01-01T00:00:00.000Z'},b'')]
                    elif mode!='shorten':sequence += [retention_xml(DATE2140 if mode=='put' else DATE2030_3 if mode=='increase' else DATE2030)]
                    sequence += [(204,{},b'')]
                # The earlier version is intentionally retained by the source but must be purged.
                if mode=='version':sequence += [OK,(200,{},VERSION.replace(b'real-version-2',b'real-version-1')),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                elif mode=='invalid_mode':sequence += [OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                else:sequence += CLEAN
                with self.subTest(language=target['language'],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary);retained(Probe(ctx(worker,d)),mode)
                    self.assertFalse(server.fixtures);self.assertEqual(server.requests[4][2].get('x-amz-bypass-governance-retention'),'true')
                    self.assertIn('versionId=probe-version',server.requests[4][1]);self.assertIn(b'GOVERNANCE',server.requests[3][3])
                    if mode=='version':self.assertTrue(any('versionId=real-version-2' in path and 'retention' in path for _,path,_,_ in server.requests))
                    if mode=='shorten_bypass':self.assertTrue(any(headers.get('x-amz-bypass-governance-retention')=='true' and method=='PUT' for method,_,headers,_ in server.requests))

    def test_failed_governance_probe_waits_actual_expiry_and_deletes_without_bypass(self):
        """Deny the actual two-second bypass probe; expect real expiry wait, unprivileged version deletion and explicit not_run before any long retention is attempted."""
        for target in self.targets:
            sequence=CREATED+[version('probe-version'),OK,error(403,'AccessDenied'),(204,{},b'')]+CLEAN
            with self.subTest(language=target['language']),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                d=Path(temporary);started=time.monotonic()
                with self.assertRaises(CaseNotRun):retained(Probe(ctx(worker,d)),'put')
                self.assertGreaterEqual(time.monotonic()-started,1.9);self.assertFalse(server.fixtures)
                self.assertNotIn('x-amz-bypass-governance-retention',server.requests[5][2]);self.assertIn('versionId=probe-version',server.requests[5][1])
                self.assertFalse(any(b'2140' in body or b'2030' in body for _,_,_,body in server.requests))

    def test_legal_hold_vectors_clear_real_versions_even_on_invalid_status(self):
        """Exercise ON/OFF, exact invalid abc response, full get dictionaries and denied/allowed deletion; expect residual actual versions to receive OFF before bypass deletion in cleanup."""
        for target in self.targets:
            for mode in ('put','invalid_status','get','delete_on','delete_off'):
                sequence=CREATED+[version()]
                if mode=='invalid_status':sequence += [error(400,'MalformedXML')]
                else:
                    sequence += [OK]
                    if mode=='put':sequence += [OK]
                    elif mode=='get':sequence += [(200,{},b'<LegalHold><Status>ON</Status></LegalHold>'),OK,(200,{},b'<LegalHold><Status>OFF</Status></LegalHold>')]
                    elif mode=='delete_on':sequence += [error(403,'AccessDenied'),OK]
                    elif mode=='delete_off':sequence += [(204,{},b'')]
                sequence += CLEAN if mode=='delete_off' else [OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=target['language'],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary);legal(Probe(ctx(worker,d)),mode);self.assertFalse(server.fixtures)
                    if mode!='delete_off':
                        self.assertIn(b'OFF',server.requests[-5][3]);self.assertIn('versionId=real-version-2',server.requests[-4][1]);self.assertEqual(server.requests[-4][2].get('x-amz-bypass-governance-retention'),'true')

    def test_negative_write_unexpected_success_still_clears_hold_and_bypasses_actual_version(self):
        """Let a nominally unlocked bucket unexpectedly accept retention/hold after the real cleanup probe; expect original source assertion failure while actual returned/listed versions are safely cleared and deleted."""
        for target in self.targets:
            for is_legal in (False,True):
                sequence=CREATED+PROBE+CLEAN+CREATED+[version(),OK]+[OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=target['language'],legal=is_legal),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary)
                    with self.assertRaises(AssertionError):invalid_bucket(Probe(ctx(worker,d)),is_legal,True)
                    self.assertFalse(server.fixtures);self.assertIn(b'OFF',server.requests[-5][3]);self.assertIn('versionId=real-version-2',server.requests[-4][1]);self.assertEqual(server.requests[-4][2].get('x-amz-bypass-governance-retention'),'true')
                    self.assertNotIn('x-amz-bucket-object-lock-enabled',server.requests[10][2])

    def test_retention_deletes_multipart_and_marker_use_producer_ids_and_abort(self):
        """Use actual initiated UploadId, returned part ETag, completion VersionId and delete marker ID; expect original denial/bypass flow and cleanup-only NoSuchUpload tolerance."""
        for target in self.targets:
            for mode in ('plain','multipart','marker','multipart_hold'):
                sequence=CREATED+([] if mode=='multipart_hold' else PROBE)
                if mode in ('multipart','multipart_hold'):
                    sequence += [(200,{},b'<InitiateMultipartUploadResult><UploadId>real-upload-7</UploadId></InitiateMultipartUploadResult>'),(200,{'ETag':'"real-etag-9"'},b''),(200,{'x-amz-version-id':'real-version-2'},b'<CompleteMultipartUploadResult><Key>file1</Key><ETag>"complete"</ETag></CompleteMultipartUploadResult>'),error(404,'NoSuchUpload')]
                else:sequence += [version(),OK]
                if mode=='marker':sequence += [version('real-marker')]
                sequence += [error(403,'AccessDenied')]
                if mode=='marker':sequence += [(204,{},b''),error(403,'AccessDenied')]
                if mode=='multipart_hold':sequence += [OK,OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                else:sequence += [(204,{},b'')]+CLEAN
                with self.subTest(language=target['language'],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary)
                    if mode=='multipart_hold':multipart_hold(Probe(ctx(worker,d)))
                    else:retention_delete(Probe(ctx(worker,d)),mode)
                    self.assertFalse(server.fixtures)
                    if mode in ('multipart','multipart_hold'):
                        self.assertTrue(any(method=='DELETE' and 'uploadId=real-upload-7' in path for method,path,_,_ in server.requests));self.assertTrue(any(b'real-etag-9' in body for _,_,_,body in server.requests))
                    if mode=='marker':self.assertTrue(any(method=='DELETE' and 'versionId=real-marker' in path for method,path,_,_ in server.requests))

    def test_head_metadata_and_upload_lock_headers_preserve_original_md5_and_dates(self):
        """Feed real HEAD lock headers and actual version IDs to both metadata source vectors; expect all three fields, signed upload headers/abc MD5, OFF and bypass cleanup."""
        head={'x-amz-version-id':'real-version-2','x-amz-object-lock-mode':'GOVERNANCE','x-amz-object-lock-retain-until-date':'2030-01-01T00:00:00.000Z','x-amz-object-lock-legal-hold':'ON'}
        for target in self.targets:
            for upload in (False,True):
                sequence=CREATED+PROBE+[version()]+([] if upload else [OK,OK])+[(200,head,b''),OK,(204,{},b'')]+CLEAN
                with self.subTest(language=target['language'],upload=upload),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary);metadata(Probe(ctx(worker,d)),upload);self.assertFalse(server.fixtures)
                    self.assertEqual(server.requests[5][3],b'abc')
                    if upload:
                        self.assertEqual(server.requests[5][2].get('x-amz-object-lock-legal-hold'),'ON');self.assertEqual({name.lower():value for name,value in server.requests[5][2].items()}.get('content-md5'),'kAFQmDzST7DWlj99KOF/cg==')

    def test_multi_delete_preserves_original_mixed_deleted_and_error_rows(self):
        """Return independent Deleted/Errors XML containing actual put-produced IDs; expect both full row contracts, second bypass request and no current-key delete markers."""
        first=b'<DeleteResult><Deleted><Key>file2</Key><VersionId>real-version-3</VersionId></Deleted><Error><Key>file1</Key><VersionId>real-version-2</VersionId><Code>AccessDenied</Code></Error></DeleteResult>'
        second=b'<DeleteResult><Deleted><Key>file1</Key><VersionId>real-version-2</VersionId></Deleted></DeleteResult>'
        for target in self.targets:
            sequence=CREATED+PROBE+[version(),version('real-version-3'),OK,(200,{},first),(200,{},second)]+CLEAN
            with self.subTest(language=target['language']),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                d=Path(temporary);multi_delete(Probe(ctx(worker,d)));self.assertFalse(server.fixtures)
                self.assertIn(b'real-version-3',server.requests[8][3]);self.assertEqual(server.requests[9][2].get('x-amz-bypass-governance-retention'),'true')

    def test_original_short_compliance_transitions_wait_real_expiry_before_cleanup(self):
        """Execute all three source mode transitions retaining the original now+10-second deadline; expect successful bypass/precise denials and no version deletion until the real expiry."""
        for target in self.targets:
            for mode in ('with_bypass','without_bypass','from_compliance'):
                sequence=CREATED+([] if mode=='from_compliance' else PROBE)+[version(),OK if mode=='with_bypass' else error(403,'AccessDenied')]+[OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                with self.subTest(language=target['language'],mode=mode),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                    d=Path(temporary);started=time.monotonic();change_mode(Probe(ctx(worker,d)),mode)
                    self.assertGreaterEqual(time.monotonic()-started,9.9);self.assertFalse(server.fixtures);self.assertIn(b'OFF',server.requests[-5][3])

    def test_invalid_bucket_all_four_original_error_oracles_and_unlocked_cleanup(self):
        """Replay precise 400/InvalidRequest for unlocked read/write endpoints; expect all four source contracts, optional actual probe, and cleanup tolerance limited to legal-hold InvalidRequest on a proven unlocked bucket."""
        empty_list=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
        for target in self.targets:
            for write in (False,True):
                for is_legal in (False,True):
                    sequence=(CREATED+PROBE+CLEAN if write else [])+CREATED+[version(),error(400,'InvalidRequest')]
                    if write:sequence += [OK,(200,{},VERSION),error(400,'InvalidRequest'),(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
                    else:sequence += [OK,(204,{},b''),(200,{},empty_list),(204,{},b''),(404,{},b'')]
                    with self.subTest(language=target['language'],write=write,legal=is_legal),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                        d=Path(temporary);invalid_bucket(Probe(ctx(worker,d)),is_legal,write);self.assertFalse(server.fixtures)

    def test_abort_only_tolerates_exact_cleanup_nosuchupload_not_arbitrary_404(self):
        """Return an arbitrary 404 after actual multipart completion; expect cleanup failure rather than treating every 404 as a completed/absent upload, while the completed real version is still purged."""
        for target in self.targets:
            sequence=CREATED+[(200,{},b'<InitiateMultipartUploadResult><UploadId>real-upload-7</UploadId></InitiateMultipartUploadResult>'),(200,{'ETag':'"part"'},b''),(200,{'x-amz-version-id':'real-version-2'},b'<CompleteMultipartUploadResult><Key>file1</Key><ETag>"done"</ETag></CompleteMultipartUploadResult>'),error(404,'DifferentError')]+[OK,(200,{},VERSION),OK,(204,{},b''),(200,{},EMPTY),(204,{},b''),(404,{},b'')]
            with self.subTest(language=target['language']),tempfile.TemporaryDirectory() as temporary,fixture_worker(target['language'],sequence,'s3') as (worker,server):
                d=Path(temporary)
                with self.assertRaisesRegex(RuntimeError,'locked multipart cleanup failed'):multipart_hold(Probe(ctx(worker,d)))
                self.assertFalse(server.fixtures);self.assertIn('versionId=real-version-2',server.requests[-4][1])

    def test_default_cases_never_authorize_lock_writes_and_read_negative_errors_remain_visible(self):
        """Execute the complete 28-source suite without explicit cleanup authorization; expect 26 not_run and only the two original read-only invalid-bucket sources executed, with no leaked buckets."""
        for target in self.targets:
            with self.subTest(language=target['language']),tempfile.TemporaryDirectory() as temporary:
                d=Path(temporary);worker=Worker(target,30)
                try:
                    rows=run_cases(Context(worker,'unused/',d,b'independent',d/'body',d/'output',{}),CASES)
                    self.assertEqual(len(rows),28);self.assertEqual(sum(row['status']=='not_run' for row in rows),26)
                    self.assertEqual({row['case'] for row in rows if row['status']!='not_run'},{'ceph_object_lock_invalid_get_retention','ceph_object_lock_invalid_get_legal_hold'})
                    Path('/tmp/osv-object-lock-default-'+target['language']+'.json').write_text(__import__('json').dumps(rows,indent=2))
                    self.assertEqual([bucket['Name'] for bucket in self.client.list_buckets()['Buckets']],['osv-sdk-integration'])
                finally:worker.close()

class ObjectLockAuthorizationTest(unittest.TestCase):
    """Reject non-boolean cleanup authorization before any resource or SDK operation can occur."""
    def test_every_write_source_requires_exact_boolean_authorization_before_io(self):
        """Call all 26 write sources with absent/false/string/integer authorization and no worker; expect CaseNotRun before filesystem/body/SDK access, preventing truthy config values from authorizing long retention."""
        readonly={'ceph_object_lock_invalid_get_retention','ceph_object_lock_invalid_get_legal_hold'}
        for value in (None,False,'true',1):
            for case in CASES:
                if case.name in readonly:continue
                with self.subTest(case=case.name,value=value),tempfile.TemporaryDirectory() as temporary:
                    d=Path(temporary);workload={} if value is None else {'object_lock_cleanup_authorized':value}
                    with self.assertRaises(CaseNotRun):case.execute(Probe(Context(None,'unused/',d,b'',d/'body',d/'out',workload)))
                    self.assertEqual(list(d.iterdir()),[])
