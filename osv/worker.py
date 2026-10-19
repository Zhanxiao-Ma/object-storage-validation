"""Persistent JSON-lines SDK workers, with bounded responses and deadlines."""
import json
import os
import queue
import subprocess
import threading
import time

from .scenarios import BASE_OPERATIONS


class Worker:
    """Run a real SDK process; protocol and startup costs stay outside SDK timing."""

    def __init__(self, target, timeout=60):
        self.timeout = timeout
        self.process = subprocess.Popen(
            target["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env={**os.environ, **target.get("env", {})},
        )
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.info = self.call({"op": "info"})
            if not self.info.get("ok"):
                raise RuntimeError("SDK worker initialization failed")
            if type(self.info.get("protocol_version", 1)) is not int or self.info.get("protocol_version", 1) != 1:
                raise ValueError("unsupported worker protocol_version")
            capabilities = self.info.get("capabilities", sorted(BASE_OPERATIONS))
            if not isinstance(capabilities, list) or not all(isinstance(op, str) and op for op in capabilities):
                raise ValueError("worker capabilities must be an array of operation names")
            self.capabilities = frozenset(capabilities)
            for key in ("language", "api", "sdk_version"):
                if key in target and self.info.get(key) != target[key]:
                    raise RuntimeError(f"worker {key} differs from the manifest: {self.info.get(key)!r} != {target[key]!r}")
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    raise RuntimeError("worker exited before sending a response")
                if len(line) > 1024 * 1024:
                    raise RuntimeError("worker response exceeds 1 MiB")
                self.responses.put(json.loads(line))
        except Exception as error:
            self.responses.put(error)

    def call(self, request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        try:
            result = self.responses.get(timeout=self.timeout)
        except queue.Empty:
            self.process.kill()
            raise TimeoutError("SDK worker exceeded request deadline") from None
        if isinstance(result, Exception):
            raise result
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("invalid SDK worker response")
        if request["op"] != "info":
            duration = result.get("elapsed_ns")
            if type(duration) is not int or duration < 0:
                raise ValueError("missing or invalid SDK elapsed_ns")
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.reader.join(timeout=1)


def invoke_external(spec, output_dir, redact):
    """Preserve upstream results as supplementary evidence, never as SDK passes."""
    started = time.monotonic()
    stdout_path = output_dir / (spec["id"] + ".stdout.txt")
    stderr_path = output_dir / (spec["id"] + ".stderr.txt")
    # Upstream tools are finite jobs, not JSONL workers. Keep them out of comparisons.
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            spec["command"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, **spec.get("env", {})},
        )
        def consume(source, destination):
            for line in source:
                destination.write(redact(line))
            source.close()
        threads = [threading.Thread(target=consume, args=pair) for pair in
                   ((process.stdout, stdout), (process.stderr, stderr))]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=spec.get("timeout_seconds", 600))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        for thread in threads:
            thread.join()
    return {"id": spec["id"], "tool": spec["tool"], "exit_code": process.returncode,
            "timed_out": timed_out, "wall_seconds": time.monotonic() - started,
            "evidence": "supplementary", "stdout": stdout_path.name, "stderr": stderr_path.name}
