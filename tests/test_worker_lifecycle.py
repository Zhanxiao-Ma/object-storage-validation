"""Regression for real SDK subprocess exits with buffered writes awaiting pipe closure."""
import json
import subprocess
import sys
import unittest
from osv.worker import Worker, WorkerUnavailable, WorkerTimeout

class WorkerLifecycleTest(unittest.TestCase):
    """Keep a failed child from breaking cleanup and hiding the original worker failure."""
    def test_dead_child_does_not_abort_pipe_cleanup(self):
        """Let a real child exit after handshake, then write; expect failure plus complete/idempotent cleanup despite buffered broken stdin."""
        for code in (0,7):
            with self.subTest(code=code):
                program="import sys,json;sys.stdin.readline();print(json.dumps({\"ok\":True,\"language\":\"python\",\"api\":\"local\",\"sdk_version\":\"1\"}),flush=True);sys.exit("+str(code)+")"
                worker=Worker({"command":[sys.executable,"-c",program]},5)
                self.assertEqual(worker.process.wait(timeout=5),code)
                with self.assertRaises((BrokenPipeError,RuntimeError)):worker.call({"op":"head","key":"dead"})
                worker.close();worker.close()
                self.assertTrue(all(stream.closed for stream in (worker.process.stdin,worker.process.stdout,worker.process.stderr)))
                self.assertFalse(worker.reader.is_alive());self.assertFalse(worker.stderr_reader.is_alive())

    def test_exit_code_preserved_on_repeated_calls(self):
        """Purpose: terminate a real child with exit 7; expect every later call to retain that cause without broken-pipe noise."""
        program='import sys,json;sys.stdin.readline();print(json.dumps({"ok":True}),flush=True);sys.exit(7)'
        worker=Worker({'command':[sys.executable,'-c',program]},2)
        try:
            worker.process.wait(timeout=2)
            for _ in range(3):
                with self.assertRaisesRegex(WorkerUnavailable,'exit_code=7'):worker.call({'op':'head'})
        finally:worker.close()

    def test_timeout_permanently_invalidates_child(self):
        """Purpose: stall a real subprocess after handshake; expect a deadline failure and no reuse of a stale response stream."""
        program='import sys,json,time;sys.stdin.readline();print(json.dumps({"ok":True}),flush=True);sys.stdin.readline();time.sleep(20)'
        worker=Worker({'command':[sys.executable,'-c',program]},.05)
        try:
            with self.assertRaises(WorkerTimeout):worker.call({'op':'head'})
            with self.assertRaisesRegex(WorkerUnavailable,'deadline'):worker.call({'op':'head'})
            self.assertIsNotNone(worker.process.poll())
        finally:worker.close()
