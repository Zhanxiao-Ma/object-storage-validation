"""Exercise independent-key and hotspot writes through actual reference storage."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from osv.cli import iter_operations, cleanup_objects
from osv.scenarios import Context, build_registry
from osv.worker import Worker
from test_harness import config


class OverwriteEvidenceTest(unittest.TestCase):
    """Check real producer output against the real GET consumer and report semantics."""

    @contextmanager
    def setup_run(self, count=7, concurrency=2):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            directory = Path(temporary)
            data = config(directory)
            workload = {**data['workload'], 'operations': ['put'], 'seed': 1729}
            target = data['targets'][0]
            workers = [Worker(target) for _ in range(concurrency)]
            for worker in workers:
                stack.callback(worker.close)
            payload = bytes(range(251))+b'known-independent-tail'
            body = directory/'input.bin'
            body.write_bytes(payload)
            ctx = Context(workers[0], 'overwrite-check/', directory, payload, body,
                          directory/'output.bin', workload)
            report = {'samples': [], 'batches': []}
            def execute():
                return list(iter_operations(ctx, workers, report, 'local', build_registry({}),
                                            workload, 'measure', 0, count))
            try:
                yield ctx, report, execute
            finally:
                cleanup_objects(workers[0], ctx.prefix)
                self.assertFalse([p for p in (directory/'objects').rglob('*') if p.is_file()])

    def test_shared_and_independent_keys_have_true_final_readback_and_wall_timing(self):
        """Purpose: execute real hot and per-lane writes at 1/2/4 lanes and uneven counts; expect distinct per-lane keys, shared hotspot keys, actual byte reads outside upload timing, and no phantom unused-lane keys."""
        for count, concurrency in ((1, 4), (7, 2), (3, 1)):
            with self.subTest(count=count, concurrency=concurrency), self.setup_run(count, concurrency) as (ctx, report, execute):
                original = Worker.call
                reads = []
                def call(worker, request):
                    result = original(worker, request)
                    if request['op'] == 'get':
                        self.assertEqual(Path(request['output_path']).read_bytes(), ctx.payload)
                        reads.append((request['key'], time.monotonic_ns()))
                    return result
                with patch.object(Worker, 'call', call):
                    self.assertEqual(execute(), ['repeat_put', 'parallel_put'])
                for operation, mode, key_count in (('repeat_put', 'shared', 1), ('parallel_put', 'per_lane', min(count, concurrency))):
                    rows = [r for r in report['samples'] if r['op'] == operation]
                    batch = next(b for b in report['batches'] if b['op'] == operation)
                    self.assertEqual(len(rows), count)
                    self.assertEqual(len({r['key'] for r in rows}), key_count)
                    self.assertEqual({r['key_mode'] for r in rows}, {mode})
                    self.assertTrue(all(r['ok'] and r['bytes'] == len(ctx.payload) for r in rows))
                    self.assertTrue(all(r['request_end_ns']-r['request_start_ns'] == r['rpc_wall_ns'] and r['rpc_wall_ns'] >= r['elapsed_ns'] for r in rows))
                    observed = [(key, at) for key, at in reads if key in {r['key'] for r in rows}]
                    self.assertEqual(len(observed), key_count)
                    self.assertTrue(all(at > batch['wall_end_ns'] for _, at in observed))
                    self.assertEqual(batch['active_keys'], key_count)
                    self.assertEqual(batch['verification']['scope'], 'final_state_only')
                    self.assertFalse(batch['verification']['failures'])
                listed = ctx.worker.call({'op': 'list', 'prefix': ctx.prefix})
                self.assertTrue(listed['ok'])
                self.assertEqual(listed['keys'], [ctx.prefix+'perf'])

    def test_corrupt_final_lane_invalidates_only_its_acknowledged_writes(self):
        """Purpose: change bytes after real GET for exactly one independent lane, including equal-length corruption; expect that lane's upload rows to fail with zero bytes while other lane and hot writes remain valid."""
        for equal_length in (True, False):
            with self.subTest(equal_length=equal_length), self.setup_run() as (ctx, report, execute):
                original = Worker.call
                def call(worker, request):
                    result = original(worker, request)
                    if request['op'] == 'get' and request['key'].endswith('perf-lane-1'):
                        Path(request['output_path']).write_bytes(b'X'*len(ctx.payload) if equal_length else b'bad')
                    return result
                with patch.object(Worker, 'call', call):
                    execute()
                failed = [r for r in report['samples'] if not r['ok']]
                self.assertEqual(len(failed), 3)
                self.assertTrue(all(r['op'] == 'parallel_put' and r['lane'] == 1 and r['bytes'] == 0 for r in failed))
                self.assertTrue(all(r['ok'] for r in report['samples'] if r not in failed))
                self.assertEqual(len(report['batches'][1]['verification']['failures']), 1)

    def test_corrupt_hot_final_state_invalidates_all_shared_key_writes(self):
        """Purpose: corrupt the actual final shared-key GET; expect all seven hot writes to fail and independent writes to retain correct data and bytes."""
        with self.setup_run() as (ctx, report, execute):
            original = Worker.call
            def call(worker, request):
                result = original(worker, request)
                if request['op'] == 'get' and request['key'].endswith('/perf'):
                    Path(request['output_path']).write_bytes(b'corrupt')
                return result
            with patch.object(Worker, 'call', call):
                execute()
            hot = [r for r in report['samples'] if r['op'] == 'repeat_put']
            self.assertTrue(all(not r['ok'] and r['bytes'] == 0 for r in hot))
            self.assertTrue(all(r['ok'] for r in report['samples'] if r['op'] == 'parallel_put'))

    def test_failed_get_cannot_accept_a_stale_correct_output_file(self):
        """Purpose: make the actual independent-key GET miss while a correct old output exists; expect SDK failure to invalidate the key's writes instead of accepting stale bytes."""
        with self.setup_run() as (ctx, report, execute):
            original = Worker.call
            def call(worker, request):
                if request['op'] == 'get' and request['key'].endswith('perf-lane-1'):
                    Path(request['output_path']).write_bytes(ctx.payload)
                    request = {**request, 'key': ctx.prefix+'never-written'}
                return original(worker, request)
            with patch.object(Worker, 'call', call):
                execute()
            failed = [r for r in report['samples'] if not r['ok']]
            self.assertEqual(len(failed), 3)
            self.assertTrue(all(r['bytes'] == 0 for r in failed))
            self.assertFalse(list(ctx.directory.glob('overwrite-*.verify')))

    def test_failed_put_remains_failed_even_if_a_later_final_state_is_valid(self):
        """Purpose: send one real PUT a nonexistent input path then complete later overwrites; expect the failed request to stay failed, successful writes to pass final GET, and exact attempted count."""
        with self.setup_run() as (ctx, report, execute):
            original = Worker.call
            damaged = False
            def call(worker, request):
                nonlocal damaged
                if request['op'] == 'put' and request['key'].endswith('perf-lane-0') and not damaged:
                    damaged = True
                    request = {**request, 'body_path': str(ctx.directory/'absent-input')}
                return original(worker, request)
            with patch.object(Worker, 'call', call):
                execute()
            rows = [r for r in report['samples'] if r['op'] == 'parallel_put']
            self.assertEqual(len(rows), 7)
            self.assertEqual(sum(not r['ok'] for r in rows), 1)
            self.assertTrue(all(r['bytes'] == 0 for r in rows if not r['ok']))
            self.assertFalse(report['batches'][1]['verification']['failures'])


if __name__ == '__main__':
    unittest.main()
