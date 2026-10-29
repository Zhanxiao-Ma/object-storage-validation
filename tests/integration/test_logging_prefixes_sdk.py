"""Actual SDK five-source prefix contracts and preservation of the pinned single-prefix oracle defect."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from osv.scenarios import Context
from osv.suites import Probe,run_cases
from osv.ceph_logging_prefixes import CASES,prefixes,RelatedBucketScope,matches
from sdk_fixtures import fixture_worker
from test_logging_workloads_sdk import log_line,listing

OK=(200,{},b'');DEL=(204,{},b'');NO=(404,{},b'');EMPTY=b'<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>'
NAMES=[uuid.UUID(int=1),uuid.UUID(int=2)]
SOURCES=['osv-'+NAMES[1].hex+str(index) for index in range(5)]

def prefix_vector(single,extra=False,last_only=False):
    sequence=[NO,OK]*6+[DEL]+[OK]*5+[OK]*25+[OK]*(1 if single else 5)
    logs=['log/shared'] if single else [source+'/record' for source in SOURCES]
    if extra and not single:logs.append(SOURCES[0]+'/another-record')
    sequence += [(200,{},listing(logs))]
    if single:
        body='\n'.join(log_line(source,'REST.PUT.OBJECT','myobject'+str(number)+str(index)) for number,source in enumerate(SOURCES,1) for index in range(5) if not last_only or number==5).encode()
        sequence += [(200,{},body)]+[(200,{},listing(['myobject'+str(number)+str(index) for index in range(5)])) for number in range(1,6)]
    else:
        for key in logs:
            source=next(source for source in SOURCES if key.startswith(source));body='\n'.join(log_line(source,'REST.PUT.OBJECT','myobject'+str(index)) for index in range(5)).encode()
            sequence += [(200,{},body),(200,{},listing(['myobject'+str(index) for index in range(5)]))]
    sequence += [OK]*6+[DEL]*6
    for number in reversed(range(5)):
        known=5+(1 if not single or number==4 else 0);sequence += [OK]+[DEL]*known+[(200,{},EMPTY),DEL,NO]
    sequence += [OK,(200,{},listing(logs))]+[DEL]*len(logs)+[DEL,NO]
    return sequence

@unittest.skipUnless(os.environ.get('OSV_RUN_LOGGING_PREFIX_TESTS')=='1','enable actual SDK logging prefix fixtures')
class LoggingPrefixesSdkTest(unittest.TestCase):
    """Use real SDK producers and independent raw logs/listings for all five related source buckets."""
    def execute(self,language,single,extra=False,last_only=False):
        with tempfile.TemporaryDirectory() as temporary,fixture_worker(language,prefix_vector(single,extra,last_only),'s3') as (worker,server):
            d=Path(temporary);ctx=Context(worker,'unused/',d,b'',d/'body',d/'out',{'identities':{'main':{'user_id':'source-account','canonical_id':'different-owner'}}})
            started=time.monotonic()
            with patch('osv.ceph_logging_prefixes.uuid.uuid4',side_effect=NAMES):prefixes(Probe(ctx),single)
            self.assertGreaterEqual(time.monotonic()-started,5.4 if single else 27.4);self.assertFalse(server.fixtures)
            policy=next(json.loads(body) for method,path,headers,body in server.requests if method=='PUT' and 'policy' in path)
            self.assertEqual(len(policy['Statement']),5)
            for source,statement in zip(SOURCES,policy['Statement']):
                self.assertEqual(statement['Condition']['ArnLike']['aws:SourceArn'],'arn:aws:s3:::'+source);self.assertEqual(statement['Condition']['StringEquals']['aws:SourceAccount'],'source-account');self.assertTrue(statement['Resource'].endswith('/log/' if single else '/'+source+'/'))
            dummies=[path for method,path,headers,body in server.requests if method=='PUT' and body==b'dummy'];self.assertEqual(len(dummies),1 if single else 5)
            if single:self.assertIn(SOURCES[-1],dummies[0])
            self.assertEqual(sum(method=='DELETE' and '/myobject' in path for method,path,headers,body in server.requests),25)

    def test_multiple_prefixes_accept_original_at_least_five_logs_and_all_source_records(self):
        """Run five actual sibling source buckets with six independently supplied logs; expect five real flush waits, >=5 acceptance and original per-prefix record matching without strengthening count to equality."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language):self.execute(language,False,extra=True)

    def test_single_prefix_preserves_original_final_source_only_assertion(self):
        """Provide a shared actual log containing only the final source's five records; expect the pinned overwrite-found loop to pass, preserving its known missing-first-four-source assertion defect and original final-source-only flush."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language):self.execute(language,True,last_only=True)

    def test_defaults_require_account_metadata_before_creating_related_buckets(self):
        """Run both source cases with actual SDK capabilities and absent user_id; expect explicit not_run and zero HTTP, rather than pretending logging or namespace success."""
        for language in os.environ.get('OSV_S3_TEST_LANGUAGES','java,cpp').split(','):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary,fixture_worker(language,[],'s3') as (worker,server):
                d=Path(temporary);rows=run_cases(Context(worker,'unused/',d,b'',d/'body',d/'out',{}),CASES);self.assertEqual(len(rows),2);self.assertEqual({row['status'] for row in rows},{'not_run'});self.assertEqual(server.requests,[])

class RelatedNamespaceBoundaryTest(unittest.TestCase):
    """Validate internal ownership namespace inputs without substituting SDK/storage behavior."""
    def test_only_internal_uuid_base_and_original_five_indices_are_accepted(self):
        """Pass valid internal UUID bases and invalid external bases/out-of-range/bool indices; expect exactly five sibling names or rejection before any SDK request."""
        from types import SimpleNamespace
        p=SimpleNamespace(ctx=SimpleNamespace(object_namespace=False));base='osv-'+uuid.UUID(int=2).hex
        self.assertEqual([RelatedBucketScope(p,base,index).name for index in range(5)],SOURCES)
        for bad_base,index in [('production',0),(base+'0',0),(base,-1),(base,5),(base,True)]:
            with self.subTest(base=bad_base,index=index),self.assertRaises(ValueError):RelatedBucketScope(p,bad_base,index)
