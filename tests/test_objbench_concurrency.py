"""Prove per-operation concurrency with real storage requests and independent barriers."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from osv.objbench import iter_objbench, normalize
from osv.scenarios import Context
from osv.worker import Worker
from test_harness import config


class ObjbenchConcurrencyTest(unittest.TestCase):
    """Let genuine reference-worker writes feed genuine GET/LIST consumers; observe scheduling only."""

    def test_each_operation_uses_its_own_objects_without_cross_phase_throttling(self):
        """Purpose: run 1-small/4-big, 4-small/1-big and short datasets with four real clients; expect 4/4/2/1 participating clients at the chosen phase, exact readback/list counts, per-batch lane evidence and no remaining objects."""
        vectors = [(1, 4, 'block', 4, 1, 4), (4, 1, 'small', 4, 4, 1),
                   (1, 2, 'block', 2, 1, 2), (1, 1, 'block', 1, 1, 1)]
        for small, big, chosen, expected, small_lanes, big_lanes in vectors:
            with self.subTest(small=small, big=big), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                directory = Path(temporary)
                target = config(directory)['targets'][0]
                workers = [Worker(target, 5) for _ in range(4)]
                for worker in workers:
                    stack.callback(worker.close)
                workload = {'seed': 1729, 'range_size': 3, 'rounds': 1,
                    'objbench': normalize({'small_objects': small, 'small_object_size': 5,
                                          'block_size': 8, 'big_object_size': big*8})}
                ctx = Context(workers[0], 'per-phase/', directory, b'raw', directory/'unused',
                              directory/'output', workload)
                report = {'samples': [], 'batches': [], 'execution_errors': []}
                barrier = threading.Barrier(expected, timeout=2)
                observed = set()
                lock = threading.Lock()
                original = Worker.call
                def call(worker, request):
                    if request['op'] == 'put' and '/'+chosen+'/' in request.get('key', ''):
                        with lock:
                            observed.add(worker.process.pid)
                        # Require independent production lanes to arrive; do not copy storage behavior.
                        barrier.wait()
                    return original(worker, request)
                with patch.object(Worker, 'call', call):
                    list(iter_objbench(ctx, workers, report, 'local'))
                self.assertEqual(len(observed), expected)
                self.assertEqual(report['execution_errors'], [])
                self.assertTrue(all(row['ok'] for row in report['samples']))
                batches = {row['op']: row for row in report['batches']}
                for operation in ('smallput', 'smallget'):
                    self.assertEqual(batches[operation]['effective_lanes'], small_lanes)
                for operation in ('put', 'get', 'range'):
                    self.assertEqual(batches[operation]['effective_lanes'], big_lanes)
                listings = [r for r in report['samples'] if r['op'] == 'list']
                self.assertEqual(len(listings), 4)
                self.assertTrue(all(r['objects'] == small+big for r in listings))
                self.assertEqual(report['benchmark_datasets'][0]['available_lanes'], 4)
                self.assertFalse([p for p in (directory/'objects').rglob('*') if p.is_file()])


if __name__ == '__main__':
    unittest.main()
