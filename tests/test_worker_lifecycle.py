"""Regression for real SDK subprocess exits with buffered writes awaiting pipe closure."""
import json
import subprocess
import sys
import unittest
from osv.worker import Worker

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
