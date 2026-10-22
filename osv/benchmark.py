"""Bounded write windows, local IO evidence, and synchronized SDK input files."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time

MIN_WRITE_SECONDS = 5.0
MAX_WRITE_BYTES = 16*1024**3


def write_seconds(ctx, phase):
    """Extend real SDK measurements automatically; local protocol self-tests are count-driven."""
    return MIN_WRITE_SECONDS if phase == "measure" and ctx.worker.info.get("api") != "local" else 0.0


def target_order(targets, seed, round_index):
    """Balance positions and pair precedence for even-sized cycles, reversing odd cycles."""
    import random
    base = list(targets)
    random.Random(seed).shuffle(base)
    count = len(base)
    if not count:
        return []
    indices = [0]+[(i+1)//2 if i % 2 else count-i//2 for i in range(1, count)]
    order = [base[(i+round_index) % count] for i in indices]
    return list(reversed(order)) if count % 2 and (round_index//count) % 2 else order


def snapshot(workers):
    """Read counters only; no cache dropping, global sync, or benchmark-state mutation."""
    result = {"at_ns": time.monotonic_ns()}
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in ("MemAvailable", "Dirty", "Writeback"):
                values[name+"_bytes"] = int(value.split()[0])*1024
        result["memory"] = values
        result["io_pressure"] = Path("/proc/pressure/io").read_text().strip()
        process_io = {}
        for pid in [os.getpid()]+[w.process.pid for w in workers]:
            try:
                fields = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/io").read_text().splitlines())
                process_io[str(pid)] = {key: int(fields[key]) for key in ("read_bytes", "write_bytes", "rchar", "wchar")}
            except (OSError, ValueError, KeyError):
                pass
        result["process_io"] = process_io
        result["load_average"] = list(os.getloadavg())
        devices = {}
        for line in Path("/proc/diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 14 and not fields[2].startswith(("loop", "ram", "zram")):
                devices[fields[2]] = {"read_sectors": int(fields[5]), "write_sectors": int(fields[9]),
                    "io_ms": int(fields[12]), "weighted_io_ms": int(fields[13])}
        result["block_devices"] = devices
    except (OSError, ValueError):
        result["unavailable"] = True
    return result


class WriteWindow:
    """Measure one continuous upload interval and retain host/client IO evidence alongside it."""
    def __init__(self, workers, minimum):
        self.workers, self.minimum = workers, minimum
        self.observations = []
        self.stop = threading.Event()

    def __enter__(self):
        self.observations.append(snapshot(self.workers))
        self.started_ns = time.monotonic_ns()
        self.deadline_ns = self.started_ns+int(self.minimum*1e9)
        def observe():
            while not self.stop.wait(.25):
                self.observations.append(snapshot(self.workers))
        self.thread = threading.Thread(target=observe, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exception):
        self.finished_ns = time.monotonic_ns()
        self.stop.set()
        self.thread.join()
        self.observations.append(snapshot(self.workers))

    def batch(self, **metadata):
        seconds = (self.finished_ns-self.started_ns)/1e9
        return {**metadata, "wall_seconds": seconds, "wall_start_ns": self.started_ns,
                "wall_end_ns": self.finished_ns, "minimum_seconds": self.minimum,
                "duration_satisfied": seconds >= self.minimum, "observations": self.observations}


def filesystem(path):
    """Identify the actual mount, including nested TMPDIR mounts, without guessing from its name."""
    resolved = str(Path(path).resolve())
    matches = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = left.split()[4].replace("\\040", " ")
            if resolved == mount or resolved.startswith(mount.rstrip("/")+"/"):
                matches.append((len(mount), right.split()[0]))
    except (OSError, ValueError, IndexError):
        return "unknown"
    return max(matches)[1] if matches else "unknown"


def sync_inputs(paths):
    """Finish writes to our own input files before starting an upload clock; never sync the host globally."""
    for path in set(map(Path, paths)):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())


@contextmanager
def performance_workspace(workload, targets, report):
    """Keep scratch files on the normal temporary filesystem; do not reserve datasets in RAM."""
    with tempfile.TemporaryDirectory(prefix="osv-performance-") as temporary:
        kind = filesystem(temporary)
        report["performance_workspace"] = {"filesystem": kind, "memory_backed": kind in ("tmpfs", "ramfs"),
            "input_sync": "fsync individual prepared files before upload measurement", "path": temporary}
        yield Path(temporary)
