"""Predetermined raw Ceph XML/log documents exercise explicit official-SDK protocol-adapter source consumers."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from xml.etree import ElementTree as ET
from unittest.mock import patch
from osv.ceph_logging_remaining_extensions import execute,CLEANUP_SPECS,PARTIAL_CONF_SPECS
from osv.scenarios import Context
from osv.suites import Probe,CaseNotRun
from sdk_fixtures import fixture_worker
from test_iam_accounts_sdk import iam_fixture,verify_requests
from test_logging_workloads_sdk import OK,DELETED,ABSENT,EMPTY,listing,log_line
from test_logging_remaining_sdk import cleanup,name

WORK={'identities':{'main':{'user_id':'independent-extension-user'}},'ceph_logging_wire_extensions':True}
PROBE=(404,{},b'<Error><Code>NoSuchBucket</Code><Message>Actual generated absent probe</Message></Error>')

def flushed(key):return (200,{},('<PostBucketLoggingResult><FlushedLoggingObject>'+key+'</FlushedLoggingObject></PostBucketLoggingResult>').encode())

def journal(bucket,event,key):return 'owner '+bucket+' [20/Oct/2026:00:00:00 +0000] '+event+' '+key+' 3 opaque-version opaque-etag'

def configuration(kind,missing_rule=False):
    suffix='' if missing_rule else '<FilterRule><Name>suffix</Name><Value>.txt</Value></FilterRule>'
    return ('<BucketLoggingStatus><LoggingEnabled><TargetBucket>'+name(5)+'</TargetBucket><TargetPrefix>log/</TargetPrefix><LoggingType>'+kind+'</LoggingType><ObjectRollTime>5</ObjectRollTime><RecordsBatchSize>0</RecordsBatchSize><TargetObjectKeyFormat><SimplePrefix/></TargetObjectKeyFormat><Filter><S3Key><FilterRule><Name>prefix</Name><Value>test/</Value></FilterRule>'+suffix+'</S3Key></Filter></LoggingEnabled></BucketLoggingStatus>').encode()

def filter_vector(kind,bad=None):
    keys=['myobject'+str(i) for i in range(5)]+['test/myobject'+str(i)+'.txt' for i in range(5)]
    out=[PROBE,ABSENT,OK,ABSENT,OK,DELETED,OK,(200,{},configuration(kind,bad=='configuration'))]
    if bad!='configuration':
        selected=keys if kind=='Standard' else keys[5:]
        line=log_line if kind=='Standard' else journal
        body='\n'.join(line(name(4),'REST.PUT.OBJECT',key) for key in selected).encode()
        out += [OK]*10+[PROBE,flushed('log/incorrect' if bad=='flushed_key' else 'log/filter'),(200,{},listing(['log/filter']))]
        if bad!='flushed_key':out += [(200,{},body)]
    out += [OK]*2+[DELETED]*2+cleanup([],[] if bad=='configuration' else ['log/filter'])+cleanup([] if bad=='configuration' else keys)
    return out

def flush_vector(kind,shared,counts):
    """Specify complete independent five-bucket rounds, original operation counts and returned file membership, without implementing log delivery."""
    out=[];counter=2;owned=[]
    for count in counts:
        # Original extension probe consumes two generated names before target/sources.
        target=name(counter+2);buckets=[name(counter+3+i) for i in range(5)];counter+=8
        out += [PROBE]+[ABSENT,OK]*6+[DELETED]+[OK]*5+[OK]*(5*count*2)+[(200,{},EMPTY)]
        keys=['log/shared'] if shared else [bucket+'/records' for bucket in buckets]
        out += [flushed(key) for key in keys]+[(200,{},listing(keys))]
        for index,key in enumerate(keys):
            sources=buckets if shared else [buckets[index]];line=log_line if kind=='Standard' else journal
            body='\n'.join(line(bucket,event,'myobject'+str(i)) for bucket in sources for event in ('REST.PUT.OBJECT','REST.DELETE.OBJECT') for i in range(count)).encode()
            out += [(200,{},body)]
        owned.append((target,buckets,keys,count))
    out += [OK]*(6*len(counts))+[DELETED]*(6*len(counts))
    for target,buckets,keys,count in reversed(owned):
        for bucket in reversed(buckets):out += cleanup(['myobject'+str(i) for i in range(count)])
        out += cleanup([],keys)
    return out

def cleanup_vector(action,kind,shared,bad=None):
    """Provide independent raw state-transition observations and full ten-key records; no fixture code performs storage or evaluates policies."""
    target=name(4);buckets=[name(i) for i in range(5,10)];count=1 if shared and kind=='Journal' and action not in ('target','updating') else 5
    keys=['log/record-'+str(i) if shared else buckets[i]+'/record' for i in range(count)]
    object_keys=['myobject'+str(i) for i in range(10)]
    out=[PROBE]+[ABSENT,OK]*6+[DELETED]+[OK]*5+[OK]*100+[(200,{},EMPTY)]
    for index,bucket in enumerate(buckets):
        if action=='deletion':out += [DELETED,ABSENT]
        elif action=='disabling':out += [flushed(keys[min(index,len(keys)-1)])]+([PROBE] if not shared or index==0 else [])
        elif action=='updating':out += [flushed(keys[index])]+[PROBE]
        elif action=='notupdating':out += [OK]
    if action=='target':
        object_keys=['after_deletion_myobject'+str(i) for i in range(10)]
        out += [DELETED,ABSENT,ABSENT,OK,DELETED]+[OK]*100+[(200,{},EMPTY)]+[flushed(key) for key in keys]
    if action=='notupdating':out += [(200,{},EMPTY)]
    else:
        out += [(200,{},listing(keys))]
        line=log_line if kind=='Standard' else journal
        for index,key in enumerate(keys):
            sources=buckets if count==1 else [buckets[index]]
            body='\n'.join(line(bucket,event,obj) for bucket in sources for event in ('REST.PUT.OBJECT','REST.DELETE.OBJECT') for obj in object_keys)+'\n'
            if bad=='records' and index==0:body=body.split('\n',1)[1]
            out += [(200,{},body.encode())]
    if action=='deletion':out += [OK,DELETED]+cleanup([],keys)
    else:
        out += [OK]*6+[DELETED]*6
        # The target is recreated last in the original target-cleanup branch.
        if action=='target':out += cleanup([],keys)
        for bucket in reversed(buckets):out += cleanup(['myobject'+str(i) for i in range(10)]+(object_keys if action=='target' else []))
        if action!='target':out += cleanup([],[] if action=='notupdating' else keys)
    return out

def partial_vector(action,kind,empty_records=False):
    target=name(4);buckets=[name(i) for i in range(5,8)];keys=['log/partial-'+str(i) for i in range(3 if action=='updating' or kind=='Standard' else 2)]
    out=[PROBE]+[ABSENT,OK]*4+[DELETED]+[OK]*3+[OK]*18+[(200,{},EMPTY)]
    for i in range(2):
        if action=='deletion':out += [DELETED,ABSENT]
        else:out += [flushed(keys[i])]+([PROBE] if action=='updating' or i==0 else [])
    out += [OK]*6+[PROBE,flushed(keys[-1]),(200,{},listing(keys))]
    line=log_line if kind=='Standard' else journal
    for i,key in enumerate(keys):
        selected=buckets[:2] if len(keys)==2 and i==0 else [buckets[-1]] if len(keys)==2 else [buckets[i]]
        body='' if empty_records else '\n'.join(line(bucket,event,'myobject'+str(j)) for bucket in selected for event in ('REST.PUT.OBJECT','REST.DELETE.OBJECT') for repeat in range(2 if bucket==buckets[-1] else 1) for j in range(3))+'\n'
        out += [(200,{},body.encode())]
    survivors=[buckets[-1]] if action=='deletion' else buckets
    out += [OK]*(len(survivors)+1)+[DELETED]*(len(survivors)+1)
    for bucket in reversed(survivors):out += cleanup(['myobject'+str(i) for i in range(3)])
    out += cleanup([],keys)
    return out


def conf_vector(kind,update):
    source=name(5);line=log_line if kind=='Standard' else journal
    first='log/empty' if update=='roll_time' else 'log/empty'
    second='log/records' if update=='roll_time' else 'newlog1/records'
    final='log/final' if update=='roll_time' else 'newlog2/final'
    out=[PROBE]+[ABSENT,OK]*2+[DELETED,OK]
    if update=='prefix':out += [DELETED]
    # The source accidentally checks the previous result's status after this
    # update. Independent 201 preserves that exact observable source oracle.
    out += [(201,{},b'')]+[OK]*100
    if update=='prefix':out += [DELETED]
    out += [flushed(second),PROBE,(200,{},listing([first,second])),(200,{},b''),DELETED,(200,{},('\n'.join(line(source,'REST.PUT.OBJECT','myobject'+str(i)) for i in range(100))+'\n').encode()),DELETED]
    out += [OK]*100+[PROBE,flushed(final),(200,{},listing([final])),(200,{},('\n'.join(line(source,'REST.PUT.OBJECT','anotherobject'+str(i)) for i in range(100))+'\n').encode())]
    out += [OK]*2+[DELETED]*2+cleanup(['myobject'+str(i) for i in range(100)]+['anotherobject'+str(i) for i in range(100)])+cleanup([],[final])
    return out

def event_vector(kind,head_count=None):
    keys=['myobject'+str(i) for i in range(5)];source=name(4)
    if head_count is None:head_count=5 if kind=='Standard' else 0
    line=log_line if kind=='Standard' else journal
    body='\n'.join([line(source,'REST.PUT.OBJECT',key) for key in keys]+[line(source,'REST.HEAD.OBJECT',keys[i]) for i in range(head_count)])
    return [PROBE]+[ABSENT,OK]*2+[DELETED,OK]+[OK]*10+[(200,{},listing(keys)),PROBE,flushed('log/events'),(200,{},listing(['log/events'])),(200,{},body.encode())]+[OK]*2+[DELETED]*2+cleanup([],['log/events'])+cleanup(keys)


def rolltime_vector():
    initial=['myobject'+str(i) for i in range(5)];later=['myobject'+str(i) for i in range(25)]+['myobject'];source=name(4)
    first='\n'.join(log_line(source,'REST.PUT.OBJECT',key) for key in initial)+'\n'
    bodies=['\n'.join(log_line(source,'REST.PUT.OBJECT',key) for key in later[start:start+13])+'\n' for start in (0,13)]
    out=[PROBE]+[ABSENT,OK]*2+[DELETED,OK]+[OK]*5+[(200,{},listing(initial)),OK,(200,{},EMPTY),OK,(200,{},listing(['log/first'])),(200,{},first.encode()),DELETED]
    out += [OK]*25+[(200,{},listing(later)),OK,(200,{},listing(['log/later-1','log/later-2']))]+[(200,{},body.encode()) for body in bodies]
    out += [OK]*2+[DELETED]*2+cleanup([],['log/later-1','log/later-2'])+cleanup(later)
    return out

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_REMAINING_EXTENSION_TESTS')=='1','enable actual SDK Ceph logging protocol-adapter fixtures')
class LoggingRemainingExtensionsSdkTest(unittest.TestCase):
    """Validate original extension source consumers using actual SDK signing/hooks and independently scripted service documents."""
    def languages(self):return os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(',')
    def run_source(self,language,suffix,fixtures,bad=False):
        with tempfile.TemporaryDirectory() as directory,fixture_worker(language,fixtures,'s3') as (worker,server):
            d=Path(directory);ctx=Context(worker,'unused/',d,b'independent',d/'body',d/'out',WORK)
            with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,100)]):
                if bad:
                    with self.assertRaises(AssertionError):execute(Probe(ctx),suffix)
                else:self.assertEqual(execute(Probe(ctx),suffix)['protocol_adapter'],'ceph_logging_wire_extensions')
            self.assertFalse(server.fixtures)
            logging_puts=[body for method,path,headers,body in server.requests if method=='PUT' and 'logging' in path and b'<LoggingEnabled>' in body]
            self.assertTrue(logging_puts)
            for method,path,headers,body in server.requests:
                if method=='PUT' and 'logging' in path and body:ET.fromstring(body)
            self.assertTrue(any(b'<LoggingType>' in body for body in logging_puts))
            if suffix.startswith('key_filter'):
                actual=next(body for body in logging_puts if b'<Filter>' in body)
                self.assertIn(b'<S3Key>',actual);self.assertEqual(actual.count(b'<FilterRule>'),2)
                self.assertIn(b'<RecordsBatchSize>0</RecordsBatchSize>',actual)
                self.assertIn(b'<SimplePrefix',actual)
            policies=[json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path]
            for policy in policies:
                for statement in policy['Statement']:self.assertEqual(statement['Condition']['StringEquals']['aws:SourceAccount'],'independent-extension-user')
            self.assertFalse(any(path.endswith('/dummy') for method,path,headers,body in server.requests))
            self.assertTrue(all(not body for method,path,headers,body in server.requests if method=='POST' and 'logging' in path))

    def test_both_original_filter_types_preserve_get_configuration_and_exact_records(self):
        """Send all original filter fields and ten real PUTs; expect Standard all-ten versus Journal five-filtered records, actual GET configuration equality and actual POST returned-key membership."""
        for language in self.languages():
            for kind in ('Standard','Journal'):
                with self.subTest(language=language,kind=kind):self.run_source(language,'key_filter_s' if kind=='Standard' else 'key_filter_j',filter_vector(kind))

    def test_filter_readback_and_flushed_key_mismatches_fail_with_owned_cleanup(self):
        """Omit one actual GET filter rule or return a different actual POST key; expect original equality/membership assertions to fail and all created buckets/log objects to be removed."""
        for language in self.languages():
            for bad in ('configuration','flushed_key'):
                with self.subTest(language=language,bad=bad):self.run_source(language,'key_filter_j',filter_vector('Journal',bad),True)

    def test_five_serial_flush_sources_keep_all_original_rounds_and_record_counts(self):
        """Run original empty, 10/100-key Journal/Standard separate/shared-prefix rounds across five sources; expect actual flush keys and every original PUT/DELETE record/count assertion, without reducing loads."""
        specs=[('flush_empty','Journal',False,[0]),('flush_j','Journal',False,[10,100,0]),('flush_s','Standard',False,[10,100]),('flush_j_single','Journal',True,[10,100]),('flush_s_single','Standard',True,[10,100])]
        for language in self.languages():
            for suffix,kind,shared,counts in specs:
                with self.subTest(language=language,source=suffix):self.run_source(language,suffix,flush_vector(kind,shared,counts))

    def test_extension_opt_in_is_required_before_any_sdk_request(self):
        """Omit explicit extension authorization; expect precise not_run and zero HTTP, preventing a hook-enabled worker from silently substituting extensions for stock SDK validation."""
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':WORK['identities']})
                with self.assertRaises(CaseNotRun):execute(Probe(ctx),'key_filter_j')
                self.assertEqual(server.requests,[])

    def test_twenty_serial_cleanup_sources_preserve_owned_deletion_recreation_and_flush_rules(self):
        """Run all twenty original five-source cleanup variants with ten PUT+DELETE pairs each; expect exact deletion/disable/update/no-update/recreate rules, true UUID ownership and all original record assertions."""
        for language in self.languages():
            for suffix,line,action,kind,shared,concurrent in CLEANUP_SPECS:
                with self.subTest(language=language,source=suffix):self.run_source(language,suffix,cleanup_vector(action,kind,shared))

    def test_cleanup_missing_actual_record_fails_before_all_owned_buckets_are_removed(self):
        """Remove one independent downloaded post-update record; expect the original exact count assertion to fail after real SDK GET and preserve complete owned-resource cleanup."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'cleanup_updating_j',cleanup_vector('updating','Journal',False,'records'),True)

    def test_six_serial_partial_cleanup_sources_keep_unasserted_record_bug(self):
        """Run all original three-source partial transitions and repeat final-source writes; expect exact file counts and membership while retaining original ignored final record booleans."""
        for language in self.languages():
            for suffix,line,action,kind,concurrent in PARTIAL_CONF_SPECS[:6]:
                with self.subTest(language=language,source=suffix):self.run_source(language,suffix,partial_vector(action,kind))

    def test_original_partial_source_does_not_assert_missing_record_results(self):
        """Supply genuinely empty downloaded partial logs; expect the unchanged upstream source to accept its unasserted False verification results, documenting rather than silently strengthening that oracle."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'part_cleanup_updating_j',partial_vector('updating','Journal',True))

    def test_four_serial_configuration_updates_preserve_both_hundred_put_batches(self):
        """Run all original rolltime/prefix Journal/Standard changes with 100+100 actual PUTs; expect actual flush membership, continued logging, and preservation of the original previous-response status check."""
        for language in self.languages():
            for suffix,line,kind,update,concurrent in PARTIAL_CONF_SPECS[6:]:
                with self.subTest(language=language,source=suffix):self.run_source(language,suffix,conf_vector(kind,update))

    def test_two_event_sources_preserve_journal_false_equality_instead_of_zero_head_rule(self):
        """Run Standard five PUT+HEAD records and Journal five PUT plus one HEAD; expect original Journal HEAD-count equality False to accept one HEAD, without inventing a zero-record rule."""
        for language in self.languages():
            for kind in ('Standard','Journal'):
                with self.subTest(language=language,kind=kind):self.run_source(language,'event_type_s' if kind=='Standard' else 'event_type_j',event_vector(kind,1 if kind=='Journal' else 5))

    def test_journal_exactly_five_head_records_fail_original_false_equality_assertion(self):
        """Return five actual Journal HEAD records; expect original verification ==False assertion to fail and all owned buckets to be cleaned."""
        for language in self.languages():
            with self.subTest(language=language):self.run_source(language,'event_type_j',event_vector('Journal',5),True)

    def test_rolltime_source_preserves_every_original_sixty_one_second_wait(self):
        """Run original 10-second rollover and 25 paced writes with every 5/5/11/25x1/10/5-second wait intact; expect observed 0→1→2 files and original 5 then26 matching records."""
        import time
        for language in self.languages():
            with self.subTest(language=language):
                started=time.monotonic();self.run_source(language,'roll_time',rolltime_vector());self.assertGreaterEqual(time.monotonic()-started,61)

    def test_journal_account_source_runs_all_three_actual_root_account_vectors(self):
        """Run all three original Journal main/root combinations through explicit logging adapter and actual IAM ARN producers; expect actual POST returned keys, all five records and six owned-bucket cleanup."""
        plan=[('GetUser',{}, {'User':{'Arn':'arn:aws:iam::'+account+':root'}},None) for account in ('111111111111','222222222222','333333333333')]
        fixtures=[PROBE]
        for source in (name(4),name(8),name(12)):
            fixtures += [ABSENT,OK,ABSENT,OK,DELETED,OK]+[OK]*5+[(200,{},listing(['myobject'+str(i) for i in range(5)])),PROBE,flushed('log/account'),(200,{},listing(['log/account'])),(200,{},'\n'.join(journal(source,'REST.PUT.OBJECT','myobject'+str(i)) for i in range(5)).encode())]
        fixtures += [OK]*6+[DELETED]*6
        for _ in range(3):fixtures += cleanup([],['log/account'])+cleanup(['myobject'+str(i) for i in range(5)])
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,iam_fixture(language,plan) as (worker,server):
                server.s3.fixtures.extend(fixtures);d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',WORK)
                with patch('osv.ceph_logging_remaining_runtime.uuid.uuid4',side_effect=[uuid.UUID(int=i) for i in range(1,40)]):self.assertEqual(execute(Probe(ctx),'account_j')['protocol_adapter'],'ceph_logging_wire_extensions')
                self.assertFalse(server.s3.fixtures);verify_requests(self,server,plan)
                policies=[json.loads(body) for method,path,headers,body in server.s3.requests if method=='PUT' and 'policy' in path]
                self.assertEqual([p['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'] for p in policies],['independent-extension-user','222222222222','333333333333'])

    def test_successful_delete_releases_claim_before_head_can_observe_replacement(self):
        """Create a genuinely owned bucket, delete it successfully then observe an independently existing replacement; expect refusal and zero cleanup HTTP against the replacement on context exit."""
        from osv.ceph_logging_remaining_extension_runtime import ExtensionRuntime
        for language in self.languages():
            with self.subTest(language=language),tempfile.TemporaryDirectory() as directory,fixture_worker(language,[ABSENT,OK,DELETED,OK],'s3') as (worker,server):
                d=Path(directory);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',WORK)
                with ExtensionRuntime(Probe(ctx),'extension') as runtime:
                    bucket=runtime.new_bucket();runtime.main.create_bucket(Bucket=bucket)
                    with self.assertRaisesRegex(RuntimeError,'deletion not confirmed'):runtime.main.delete_bucket(Bucket=bucket)
                    self.assertEqual(runtime.scopes,[]);self.assertEqual(runtime.main.claims,{})
                self.assertFalse(server.fixtures);self.assertEqual([row[0] for row in server.requests],['HEAD','PUT','DELETE','HEAD'])

class LoggingCapturedDocumentTest(unittest.TestCase):
    """Pin extension response presence and scalar boundaries independently from SDK model defaults or original request values."""
    def test_actual_xml_zero_empty_prefix_rules_and_entities_remain_present(self):
        """Decode independent response bytes containing zero integers, empty SimplePrefix/text and XML entities; expect exact present fields and original HTTP metadata without request-derived defaults."""
        import base64
        from osv.ceph_logging_remaining_extension_runtime import captured
        body=b'<BucketLoggingStatus><LoggingEnabled><TargetPrefix/><ObjectRollTime>0</ObjectRollTime><RecordsBatchSize>0</RecordsBatchSize><TargetObjectKeyFormat><SimplePrefix/></TargetObjectKeyFormat><Filter><S3Key><FilterRule><Name>prefix</Name><Value>a&amp;b</Value></FilterRule><FilterRule><Name>suffix</Name><Value>.txt</Value></FilterRule></S3Key></Filter></LoggingEnabled><FlushedLoggingObject>actual&amp;key</FlushedLoggingObject></BucketLoggingStatus>'
        actual=captured({'ok':True,'status_code':201,'http_headers':{'actual':'header'},'response_body_b64':base64.b64encode(body).decode()})
        self.assertEqual(actual,{'LoggingEnabled':{'TargetPrefix':'','ObjectRollTime':0,'RecordsBatchSize':0,'TargetObjectKeyFormat':{'SimplePrefix':{}},'Filter':{'Key':{'FilterRules':[{'Name':'prefix','Value':'a&b'},{'Name':'suffix','Value':'.txt'}]}}},'FlushedLoggingObject':'actual&key','ResponseMetadata':{'HTTPStatusCode':201,'HTTPHeaders':{'actual':'header'}}})

    def test_absent_empty_and_invalid_capture_never_create_expected_extension_fields(self):
        """Provide empty/absent/over-limit/malformed actual captures; expect empty response fields only for a genuine empty body and explicit errors otherwise, never fabricated FlushedLoggingObject or LoggingEnabled."""
        import base64
        from osv.ceph_logging_remaining_extension_runtime import captured
        base={'ok':True,'status_code':204}
        self.assertEqual(captured({**base,'response_body_b64':''}),{'ResponseMetadata':{'HTTPStatusCode':204,'HTTPHeaders':{}}})
        for values in ({},{'response_body_b64':'','response_body_capture_error':'actual response exceeded 512KiB'},{'response_body_b64':'!invalid'}):
            with self.subTest(values=values),self.assertRaises(Exception):captured({**base,**values})
        for text in ('<broken>','<BucketLoggingStatus><LoggingEnabled><ObjectRollTime>1.5</ObjectRollTime></LoggingEnabled></BucketLoggingStatus>'):
            with self.subTest(text=text),self.assertRaises(Exception):captured({**base,'response_body_b64':base64.b64encode(text.encode()).decode()})
