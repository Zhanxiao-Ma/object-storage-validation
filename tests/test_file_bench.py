"""Exercise the file-operation runner through real storage, transport and reporting."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from osv.cli import load_config,run
from osv.file_bench import normalize,inventory
from osv.worker import Worker
from osv.report import comparisons
from test_harness import config


class FileOperationsTest(unittest.TestCase):
    """Verify independent operations using genuine producer output and independent oracles."""
    def execute(self,directory,change=None):
        data=config(directory);data['workload'].update(mode='performance',requests=2,rounds=1,warmup_requests=0)
        if change:change(data)
        manifest=directory/'config.json';manifest.write_text(json.dumps(data))
        return run(load_config(manifest),directory/'results')

    def test_all_operations_and_concurrencies(self):
        """Purpose: run every atomic operation; expect all parameter cases, valid bytes and no residual objects."""
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);report=self.execute(directory)
            self.assertEqual(report['execution_errors'],[])
            self.assertEqual({c['action'] for c in report['performance_inventory']},
                             {'head','get','range','put','multipart','list','copy','delete','delete_many'})
            self.assertEqual({c['concurrency'] for c in report['performance_inventory']},{1,2})
            self.assertEqual({c['name'] for c in report['performance_inventory']},{r['op'] for r in report['summary']})
            self.assertTrue(all(s['ok'] for s in report['samples']))
            self.assertEqual(len(report['summary']),22)
            self.assertFalse(list((directory/'objects').rglob('*.bin')))
            self.assertTrue(all(b['verification_outside_timing'] for b in report['batches']))

    def test_range_head_middle_tail_and_short_objects(self):
        """Purpose: consume exact ranges of real stored bytes; expect head/middle/tail positions and no extra read bytes."""
        seen=[];original=Worker.call
        def observe(worker,q):
            if q['op']=='get' and 'offset' in q and '/performance/' in q['key']:
                seen.append((q['offset'],q['length']))
            return original(worker,q)
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',observe):
            report=self.execute(Path(d))
            self.assertEqual(set(seen),{(0,6),(5,6),(11,6)})
            self.assertTrue(all(s['bytes']==6 for s in report['samples'] if s['op'].startswith('range_')))

    def test_same_length_corruption_invalidates_read_write_copy_and_multipart(self):
        """Purpose: corrupt real downloaded bytes without changing length; expect failed samples and zero valid throughput for every data operation."""
        original=Worker.call
        def corrupt(worker,q):
            result=original(worker,q)
            if q['op']=='get' and '/performance/' in q.get('key','') and result.get('ok'):
                path=Path(q['output_path']);data=bytearray(path.read_bytes());data[0]^=1;path.write_bytes(data)
            return result
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',corrupt):
            report=self.execute(Path(d))
            for action in ('get','range','put','copy','multipart'):
                rows=[s for s in report['samples'] if s['op'].startswith(action+'_')]
                self.assertTrue(rows,action);self.assertTrue(all(not s['ok'] and s['bytes']==0 for s in rows),action)

    def test_success_response_cannot_hide_undeleted_objects(self):
        """Purpose: suppress actual batch deletion while returning success; expect remaining objects to invalidate every batch sample."""
        original=Worker.call
        def omit(worker,q):
            if q['op']=='delete_files':return {'ok':True,'elapsed_ns':1,'errors':[]}
            return original(worker,q)
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',omit):
            report=self.execute(Path(d));rows=[s for s in report['samples'] if s['op'].startswith('delete_many_')]
            self.assertTrue(rows);self.assertTrue(all(not s['ok'] and s.get('objects',0)==0 for s in rows))
            self.assertFalse(list((Path(d)/'objects').rglob('*.bin')))

    def test_batch_errors_and_duplicate_list_items_are_failures(self):
        """Purpose: alter real SDK response metadata; expect explicit batch errors and duplicate LIST entries to fail verification."""
        original=Worker.call
        def fault(worker,q):
            result=original(worker,q)
            if q['op']=='delete_files':result['errors']=[{'key':q['objects'][0]['key'],'code':'AccessDenied'}]
            if q['op']=='list' and q.get('prefix','').endswith('listing/') and result.get('keys'):
                result['keys'].append(result['keys'][0])
            return result
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',fault):
            report=self.execute(Path(d))
            rows=[s for s in report['samples'] if s['op'].startswith(('list_','delete_many_'))]
            self.assertTrue(rows);self.assertTrue(all(not s['ok'] for s in rows))

    def test_missing_capability_is_reported_and_other_operations_continue(self):
        """Purpose: remove multipart support from the real worker handshake; expect named not-run cases and successful independent reads."""
        original=Worker.call
        def restrict(worker,q):
            result=original(worker,q)
            if q['op']=='info':result['capabilities'].remove('multipart_upload')
            return result
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',restrict):
            report=self.execute(Path(d))
            self.assertEqual(len(report['performance_not_run']),2)
            self.assertTrue(all('multipart_upload' in r['reason'] for r in report['performance_not_run']))
            self.assertTrue(any(s['ok'] and s['op'].startswith('get_') for s in report['samples']))

    def test_prepare_verify_cleanup_are_outside_windows(self):
        """Purpose: delay real setup/cleanup requests; expect elapsed benchmark windows to exclude those delays."""
        original=Worker.call;delayed=[0.0]
        def delay(worker,q):
            # All setup PUTs of a HEAD case are outside its measurement window.
            if '/head_' in q.get('key','') and q['op'] in ('put','delete'):
                time.sleep(.02);delayed[0]+=.02
            return original(worker,q)
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',delay):
            report=self.execute(Path(d))
            heads=[b for b in report['batches'] if b['op'].startswith('head_')]
            self.assertGreaterEqual(delayed[0],.12)
            self.assertLess(sum(b['wall_seconds'] for b in heads),delayed[0]/2)

    def test_warmup_excluded_and_targets_interleaved(self):
        """Purpose: run two genuine workers for two rounds; expect identical case ordering, reversed target order, and warmups excluded."""
        def change(data):
            other=json.loads(json.dumps(data['targets'][0]));other['id']='second';data['targets'].append(other)
            data['workload'].update(rounds=2,warmup_requests=1)
        with tempfile.TemporaryDirectory() as d:
            report=self.execute(Path(d),change)
            self.assertTrue(any(s['phase']=='warmup' for s in report['samples']))
            self.assertTrue(all(r['attempts']==4 for r in report['summary']))
            batches=[b for b in report['batches'] if b['phase']=='measure']
            first=[b['target'] for b in batches if b['op']=='head_17_c1' and b['round']==0]
            second=[b['target'] for b in batches if b['op']=='head_17_c1' and b['round']==1]
            self.assertEqual(first,second[::-1])

    def test_read_sources_prepared_once_per_lane(self):
        """Purpose: perform repeated genuine reads/copies; expect one source per lane, exact results, and complete cleanup."""
        original=Worker.call;prepared=[]
        def observe(worker,q):
            if q['op']=='put' and '/source/' in q.get('key',''):
                prepared.append(q['key'])
            return original(worker,q)
        def change(data):data['workload'].update(requests=7)
        with tempfile.TemporaryDirectory() as d,patch.object(Worker,'call',observe):
            report=self.execute(Path(d),change)
            self.assertEqual(report['execution_errors'],[])
            self.assertTrue(all(s['ok'] for s in report['samples']))
            # HEAD, GET, COPY and three Range positions, each with 1+2 sources.
            self.assertEqual(len(prepared),18)
            self.assertEqual(len(set(prepared)),18)
            self.assertFalse(list((Path(d)/'objects').rglob('*.bin')))

    def test_same_protocol_sdk_comparison(self):
        """Purpose: compare two S3-speaking SDK identities; expect a valid budget result rather than a native-protocol rejection."""
        cfg={'targets':[{'id':i,'provider':'minio','language':'java','api':'s3','sdk':i} for i in ('aws','minio')],
             'comparisons':[dict(s3='aws',native='minio',min_samples=1,min_throughput_ratio=.9,max_p95_ratio=1.1,same_storage_and_network_confirmed=True)]}
        rows=[dict(target=i,op='head',successes=2,failures=0,verified_ops_s=10,p95_ms=2) for i in ('aws','minio')]
        self.assertEqual(comparisons(cfg,[],rows)[0]['status'],'within_budget')

    def test_multipart_short_tail_and_failure_abort(self):
        """Purpose: upload real prepared parts and fail a later read; expect exact concatenated bytes and no abandoned upload directory."""
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);data=config(directory);worker=Worker(data['targets'][0])
            try:
                first=directory/'first';last=directory/'last';first.write_bytes(bytes(range(128)));last.write_bytes(b'short-tail')
                result=worker.call({'op':'multipart_upload','key':'parts','part_paths':[str(first),str(last)]})
                self.assertTrue(result['ok'],result)
                out=directory/'read';self.assertTrue(worker.call({'op':'get','key':'parts','output_path':str(out)})['ok'])
                self.assertEqual(out.read_bytes(),bytes(range(128))+b'short-tail')
                failed=worker.call({'op':'multipart_upload','key':'failed','part_paths':[str(first),str(directory/'absent')]})
                self.assertFalse(failed['ok'])
                self.assertFalse(worker.call({'op':'head','key':'failed'})['ok'])
                self.assertFalse(list((directory/'objects'/'.uploads').glob('*')))
            finally:worker.close()

    def test_cross_page_listing_and_cross_batch_delete(self):
        """Purpose: exercise real 1001-object datasets; expect complete lists and verified deletion across the request boundary."""
        def change(data):
            data['workload'].update(requests=1,concurrency=1)
            data['workload']['files'].update(list_counts=[1001],delete_counts=[1001])
        with tempfile.TemporaryDirectory() as d:
            report=self.execute(Path(d),change)
            rows=[s for s in report['samples'] if s['op'].startswith(('list_','delete_many_'))]
            self.assertEqual(len(rows),2)
            self.assertTrue(all(s['ok'] and s['objects']==1001 for s in rows))

    def test_invalid_matrix_rejected(self):
        """Purpose: reject malformed/unbounded sizes before any storage IO; expect explicit validation failures."""
        for value in ({'sizes':[]},{'sizes':[True]},{'sizes':[[1]]},{'sizes':[1,1]}, {'part_sizes':[1]},
                      {'range_object_size':1,'range_sizes':[2]}, {'list_counts':[10001]}, {'unknown':1}):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize(value)
