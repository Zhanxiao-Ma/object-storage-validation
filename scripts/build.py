#!/usr/bin/env python3
"""Build SDK workers with CPU, address-space and I/O limits plus memory-pressure stops."""
import argparse
from collections import deque
import os
from pathlib import Path
import resource
import signal
import subprocess
import time

GIB = 1024 ** 3


def available():
    return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:"))) * 1024


def swap_pages():
    stats = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return (int(stats["pswpin"]) + int(stats["pswpout"])) * os.sysconf("SC_PAGE_SIZE")


def pressure():
    line = next(line for line in Path("/proc/pressure/memory").read_text().splitlines() if line.startswith("full "))
    return float(dict(item.split("=") for item in line.split()[1:])["avg10"])


def group_rss(group):
    total = 0
    for entry in Path("/proc").glob("[0-9]*/stat"):
        try:
            # comm can contain spaces or parentheses; fields after its final ')' are stable.
            fields = entry.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group:
                total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
    return total


def build_environment(environ, jobs, budget):
    """Fit JVM reservations inside the address-space budget, including non-heap regions."""
    environment = dict(environ)
    heap_mib = min(2048, budget // (3 * 1024**2))
    environment["MAVEN_OPTS"] = (
        f"-Xms64m -Xmx{heap_mib}m -XX:+UseSerialGC -XX:CompressedClassSpaceSize=128m "
        f"-XX:ReservedCodeCacheSize=128m -XX:MaxMetaspaceSize=256m -XX:ActiveProcessorCount={jobs}")
    environment["CMAKE_BUILD_PARALLEL_LEVEL"] = str(jobs)
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("java", "cpp", "command"))
    parser.add_argument("--prefix", help="CMAKE_PREFIX_PATH for installed AWS SDK and nlohmann_json")
    parser.add_argument("--oss-source", help="optional official OSS C++ SDK source checkout")
    parser.add_argument("--tos-source", help="optional official TOS C++ SDK source checkout")
    args, remainder = parser.parse_known_args()
    if args.target != "command" and remainder:
        parser.error("unrecognized arguments: " + " ".join(remainder))
    memory = available()
    jobs = min(max(1, (os.cpu_count() or 1)-2), 8, (memory-12*GIB)//(2*GIB))
    if memory < 12*GIB or jobs < 1:
        parser.exit(2, f"Insufficient memory: MemAvailable={memory/GIB:.2f} GiB; build not started.\n")
    root = Path(__file__).resolve().parents[1]
    budget = memory - 12*GIB
    print(f"Build jobs={jobs}, address-space cap={budget/GIB:.1f} GiB, nice=10, ionice=2/7", flush=True)
    environment = build_environment(os.environ, jobs, budget)
    if args.target == "java":
        commands = [["mvn", "-B", "-T", str(jobs), "-f", str(root/"drivers/java/pom.xml"), "package"]]
    elif args.target == "cpp":
        configure = ["cmake", "-S", str(root/"drivers/cpp"), "-B", str(root/"drivers/cpp/build"), "-DCMAKE_BUILD_TYPE=Release"]
        if args.prefix:
            configure.append("-DCMAKE_PREFIX_PATH="+args.prefix)
        if args.oss_source:
            configure.append("-DOSV_OSS_SOURCE="+args.oss_source)
        if args.tos_source:
            configure.append("-DOSV_TOS_SOURCE="+args.tos_source)
        commands = [configure, ["cmake", "--build", str(root/"drivers/cpp/build"), "--parallel", str(jobs)]]
    else:
        argv = remainder[1:] if remainder and remainder[0] == "--" else remainder
        if not argv:
            parser.error("command mode requires an executable")
        commands = [[word.replace("{jobs}", str(jobs)) for word in argv]]
    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (budget, budget))
        cpus = sorted(os.sched_getaffinity(0))[:jobs]
        os.sched_setaffinity(0, cpus)
    for command in commands:
        child = subprocess.Popen(["nice", "-n", "10", "ionice", "-c2", "-n7", *command],
                                 env=environment, start_new_session=True, preexec_fn=limits)
        history = deque(maxlen=11)
        try:
            while child.poll() is None:
                history.append((time.monotonic(), swap_pages()))
                swapping = len(history) == 11 and ((history[-1][1]-history[0][1]) /
                           (history[-1][0]-history[0][0])) > 64*1024**2
                if available() < 8*GIB or pressure() > 5 or swapping or group_rss(child.pid) > budget:
                    raise RuntimeError("Build stopped: memory pressure threshold exceeded")
                time.sleep(1)
            if child.returncode:
                raise SystemExit(child.returncode)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()


if __name__ == "__main__":
    main()
