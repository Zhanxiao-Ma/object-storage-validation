"""Exercise new performance workloads through actual local producer/consumer storage."""
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import unittest

from osv.cli import load_config, run
from osv.io_bench import normalize
from osv.worker import Worker
from test_harness import config, ROOT


class IoBenchTest(unittest.TestCase):
    """Assert real bytes, request plans, cleanup and measurements rather than a copied workload."""

    def manifest(self, directory, model, options, **overrides):
        data = config(directory)
        data['workload'].update(mode='performance',  requests=8,
            rounds=1, concurrency=2, warmup_requests=0, **{model: options})
        data['workload'].update(overrides)
        path = directory/'manifest.json'
        path.write_text(json.dumps(data))
        return path

    def proxy(self, directory, path, mutation):
        """Alter only an actual request/returned file; storage behavior still runs in the production worker."""
        script = directory/'wire-proxy.py'
        script.write_text('import json,subprocess,sys\nfrom pathlib import Path\n'
            +f'child=subprocess.Popen([sys.executable,{str(ROOT/"drivers/reference.py")!r}],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)\n'
            +'try:\n for line in sys.stdin:\n  q=json.loads(line)\n'
            +('  if q.get("op")=="mp_complete": q["parts"][0]["etag"]="independent-wrong-etag"\n' if mutation=='etag' else '')
            +'  child.stdin.write(json.dumps(q)+"\\n");child.stdin.flush();response=child.stdout.readline()\n'
            +('  if q.get("op")=="get":\n   p=Path(q["output_path"]);b=p.read_bytes();p.write_bytes(bytes([b[0]^255])+b[1:])\n' if mutation=='bytes' else '')
            +'  print(response.strip(),flush=True)\nfinally: child.terminate();child.wait()\n')
        data = json.loads(path.read_text());data['targets'][0]['command']=[sys.executable,str(script)]
        path.write_text(json.dumps(data))

    def test_real_reads_cover_sizes_tail_windows_and_same_object_concurrency(self):
        """Run actual small/Range reads with two clients; expect full hashes, ordered windows including a short tail, reproducible random offsets, a common hot key, excluded warmup and no leaked objects."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self.manifest(directory, 'read', {'small_object_sizes':[3,9], 'objects':2,
                'object_size':17, 'range_sizes':[6,17], 'patterns':['sequential','random','hot'], 'warmup_rounds':1})
            report = run(load_config(path), directory/'results')
            self.assertEqual(report['cases'], [])
            self.assertEqual(report['execution_errors'], [])
            self.assertEqual(len([r for r in report['summary'] if r['op'].startswith('read_')]), 8)
            self.assertTrue(all(row['ok'] for row in report['samples']))
            self.assertEqual(sum(row['attempts'] for row in report['summary'] if row['op'].startswith('read_')), 64)
            self.assertEqual(sum(row['phase']=='warmup' for row in report['samples']), 64)
            rows = [r for r in report['samples'] if r['phase']=='measure' and r['op']=='read_sequential_6']
            self.assertEqual([r['offset'] for r in rows], [0,0,6,6,12,12,0,0])
            self.assertEqual([r['bytes'] for r in rows], [6,6,6,6,5,5,6,6])
            hot = [r for r in report['samples'] if r['phase']=='measure' and r['op']=='read_hot_6']
            self.assertEqual(len({r['key'] for r in hot}), 1)
            self.assertTrue(all(r['bytes']==6 for r in hot))
            for phase in ('warmup','measure'):
                random_rows = [r for r in report['samples'] if r['phase']==phase and r['op']=='read_random_6']
                self.assertTrue(all(0 <= r['offset'] <= 11 for r in random_rows))
            self.assertEqual([r['offset'] for r in report['samples'] if r['phase']=='warmup' and r['op']=='read_random_6'],
                             [r['offset'] for r in report['samples'] if r['phase']=='measure' and r['op']=='read_random_6'])
            self.assertEqual([p for p in (directory/'objects').rglob('*') if p.is_file()], [])
            self.assertIn('读取与分片负载配置', (directory/'results/report.md').read_text())

    def test_corrupt_real_read_bytes_never_enter_verified_throughput(self):
        """Corrupt actual returned bytes after worker IO; expect every affected request failed, zero verified bytes, bounded diagnostics for 64 KiB corruption, preserved failure counts and complete cleanup."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self.manifest(directory, 'read', {'small_object_sizes':[65536], 'objects':1,
                'object_size':65537, 'range_sizes':[4096], 'patterns':['hot']})
            self.proxy(directory,path,'bytes')
            report = run(load_config(path),directory/'results')
            self.assertTrue(report['samples'])
            self.assertTrue(all(not r['ok'] and r['bytes']==0 for r in report['samples'] if r['op'].startswith('read_')))
            self.assertTrue(all(len(r['error']) < 512 for r in report['samples'] if r['op'].startswith('read_')))
            self.assertTrue(all(r['successes']==0 and r['failures']==8 for r in report['summary'] if r['op'].startswith('read_')))
            self.assertEqual([p for p in (directory/'objects').rglob('*') if p.is_file()], [])

    def test_real_multipart_verifies_short_last_part_and_separate_timing(self):
        """Upload two real objects with full and 17-byte last parts; expect final independent hashes, exact part/total counts, SDK-versus-upload timing separation, excluded warmup and no objects/uploads left."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp);size=5*1024**2+17
            path = self.manifest(directory,'multipart',{'object_sizes':[size], 'part_sizes':[5*1024**2], 'objects':2,'warmup_rounds':1})
            report = run(load_config(path),directory/'results')
            self.assertEqual(report['execution_errors'], [])
            self.assertEqual(report['cases'], [])
            self.assertTrue(all(r['ok'] for r in report['samples']))
            measured = [r for r in report['samples'] if r['phase']=='measure' and r['op'].startswith('multipart_')]
            self.assertEqual(len(measured), 10)
            parts = [r for r in measured if r['op'].startswith('multipart_part_')]
            self.assertEqual(sorted(r['bytes'] for r in parts), [17,17,5*1024**2,5*1024**2])
            totals = [r for r in measured if r['op'].startswith('multipart_total_')]
            self.assertEqual(len(totals),2)
            self.assertTrue(all(r['bytes']==size and r['timing_scope']=='upload_wall' for r in totals))
            total_summary = next(r for r in report['summary'] if r['op'].startswith('multipart_total_'))
            self.assertEqual(total_summary['attempts'],2)
            self.assertEqual(total_summary['latency_scope'],'upload_wall')
            self.assertIsNone(total_summary['sdk_mean_ms'])
            self.assertEqual([p for p in (directory/'objects').rglob('*') if p.is_file()], [])

    def test_failed_real_completion_aborts_uploads_and_rejects_provisional_parts(self):
        """Send a wrong ETag into actual multipart completion; expect failed object/part verification, attempted abort, zero total verified bytes and no unfinished upload files."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = self.manifest(directory,'multipart',{'object_sizes':[5*1024**2+1], 'part_sizes':[5*1024**2], 'objects':2})
            self.proxy(directory,path,'etag')
            report = run(load_config(path),directory/'results')
            totals = [r for r in report['samples'] if r['op'].startswith('multipart_total_')]
            self.assertEqual(len(totals),2)
            self.assertTrue(all(not r['ok'] and r['bytes']==0 for r in totals))
            self.assertTrue(all(not r['ok'] and r['bytes']==0 for r in report['samples'] if r['op'].startswith('multipart_part_')))
            self.assertEqual([p for p in (directory/'objects').rglob('*') if p.is_file()], [])

    def test_multipart_readback_corruption_rejects_successful_sdk_upload(self):
        """Corrupt real GET bytes only after successful multipart completion; expect the whole object and provisional parts to fail verification with zero verified bytes, while actual successful SDK create/complete responses and cleanup remain truthful."""
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            path=self.manifest(directory,'multipart',{'object_sizes':[5*1024**2+17], 'part_sizes':[5*1024**2], 'objects':1})
            self.proxy(directory,path,'bytes')
            report=run(load_config(path),directory/'results')
            totals=[r for r in report['samples'] if r['op'].startswith('multipart_total_')]
            self.assertEqual(len(totals),1)
            self.assertFalse(totals[0]['ok'])
            self.assertEqual(totals[0]['bytes'],0)
            self.assertTrue(all(not r['ok'] and r['bytes']==0 for r in report['samples'] if r['op'].startswith('multipart_part_')))
            self.assertTrue(all(r['ok'] for r in report['samples'] if r['op'].startswith(('multipart_create_','multipart_complete_'))))
            self.assertEqual([p for p in (directory/'objects').rglob('*') if p.is_file()],[])

    def test_multipart_upload_window_ends_before_any_readback_or_cleanup(self):
        """Observe real worker calls with one client and two objects; expect both SDK completes before GET/delete, and the reported upload clock window to end between the last complete response and first GET, keeping verification outside write throughput."""
        original=Worker.call
        events=[]
        def observe(worker, request):
            begin=time.monotonic_ns()
            result=original(worker,request)

            if '/multipart/' in request.get('key','') or request['op'].startswith('mp_'):
                events.append((request['op'],begin,time.monotonic_ns()))
            return result
        with tempfile.TemporaryDirectory() as temp, patch.object(Worker,'call',observe):
            directory=Path(temp)
            path=self.manifest(directory,'multipart',{'object_sizes':[5*1024**2+17], 'part_sizes':[5*1024**2], 'objects':2},concurrency=1)
            report=run(load_config(path),directory/'results')
            self.assertTrue(all(row['ok'] for row in report['samples']))
            complete=[end for op,begin,end in events if op=='mp_complete']
            reads=[begin for op,begin,end in events if op=='get']
            deletes=[begin for op,begin,end in events if op=='delete']
            self.assertEqual(len(complete),2)
            self.assertEqual(len(reads),2)
            batch=next(row for row in report['batches'] if row['op'].startswith('multipart_total_'))
            self.assertLessEqual(max(complete),batch['wall_end_ns'])
            self.assertLessEqual(batch['wall_end_ns'],min(reads))
            self.assertLess(max(complete),min(deletes))
            self.assertAlmostEqual(batch['wall_seconds'],(batch['wall_end_ns']-batch['wall_start_ns'])/1e9)

    def test_configuration_boundaries_reject_invalid_matrices_before_io(self):
        """Feed independent invalid sizes/types/counts and excessive matrices to real validation; expect rejection before process startup, with exact boundaries and unchanged old defaults accepted."""
        for model, values in [('read', {'range_sizes':[]}), ('read',{'range_sizes':[1,1]}),
            ('read', {'small_object_sizes':[True]}), ('read',{'objects':0}), ('read',{'object_size':16,'range_sizes':[17]}),
            ('read',{'patterns':['unknown']}), ('read',{'patterns':['hot','hot']}), ('read',{'warmup_rounds':11}),
            ('read',{'object_size':256*1024**2,'objects':9}), ('multipart',{'part_sizes':[5*1024**2-1]}),
            ('multipart',{'object_sizes':[0]}), ('multipart',{'object_sizes':[256*1024**2],'objects':9}),
            ('multipart',{'unknown':1})]:
            with self.subTest(model=model,values=values), self.assertRaises(ValueError):
                normalize(model,values)
        self.assertEqual(normalize('multipart',{'part_sizes':[5*1024**2]})['part_sizes'],[5*1024**2])
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            path=self.manifest(directory,'read',{'range_sizes':[0]})
            with self.assertRaises(ValueError):load_config(path)
