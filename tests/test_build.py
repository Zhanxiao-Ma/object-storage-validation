"""Exercise the actual JVM under the address-space limits used by the build guard."""
import os
import resource
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from scripts.build import build_environment, GIB


@unittest.skipUnless(shutil.which("java"), "Java runtime required for the real JVM reservation check")
class BuildEnvironmentTest(unittest.TestCase):
    """Prevent fixed JVM reservations from exceeding a small but valid build budget."""

    @unittest.skipUnless(os.environ.get('OSV_RUN_BUILD_TESTS')=='1' and shutil.which('mvn'),'enable guarded real Maven freshness regression explicitly')
    def test_actual_guarded_java_build_consumes_changed_source_with_old_timestamp(self):
        """Purpose: build real SDK sources, change a class while preserving an older source timestamp, and rebuild; expect the final actual jar to contain the changed producer output rather than stale class bytes."""
        import zipfile
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            project=Path(temporary)
            shutil.copytree(root/'drivers/java',project/'drivers/java',ignore=shutil.ignore_patterns('target'))
            (project/'scripts').mkdir();shutil.copy2(root/'scripts/build.py',project/'scripts/build.py')
            source=project/'drivers/java/src/main/java/io/github/storagevalidation/BuildFreshnessProbe.java'
            for marker in ('freshness-before','freshness-after'):
                source.write_text('package io.github.storagevalidation;\n/** Detect stale generated-source bytecode by exposing a distinct literal in the real compiled class. */\nfinal class BuildFreshnessProbe { static String value(){return "'+marker+'";} }\n')
                if marker.endswith('after'):os.utime(source,(946684800,946684800))
                result=subprocess.run([sys.executable,str(project/'scripts/build.py'),'java'],cwd=project,capture_output=True,text=True,timeout=180)
                self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-8000:])
                with zipfile.ZipFile(project/'drivers/java/target/sdk-worker-0.1.0.jar') as jar:
                    actual=jar.read('io/github/storagevalidation/BuildFreshnessProbe.class')
                self.assertIn(marker.encode(),actual)

    def test_real_jvm_starts_with_minimum_and_ci_budgets(self):
        """Launch a real JVM using production options at 2 and 2.6 GiB caps; expect successful startup in both cases."""
        for budget in (2*GIB, int(2.6*GIB)):
            with self.subTest(budget=budget):
                environment = build_environment(os.environ, 1, budget)
                def limits():
                    resource.setrlimit(resource.RLIMIT_AS, (budget, budget))
                result = subprocess.run(["java", *shlex.split(environment["MAVEN_OPTS"]), "-version"],
                    capture_output=True, text=True, timeout=15, env=environment, preexec_fn=limits)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                self.assertIn("version", result.stderr)
                self.assertEqual(environment["CMAKE_BUILD_PARALLEL_LEVEL"], "1")

    @unittest.skipUnless(os.environ.get("OSV_RUN_BUILD_TESTS")=="1" and shutil.which("mvn"),"enable actual resource-guarded Maven packaging explicitly")
    def test_actual_project_packages_with_minimum_address_space(self):
        """Package real SDK code at 2/2.6 GiB, capped by any tighter inherited guard; expect worker classes without increasing the parent's hard limit."""
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);project=directory/"java"
            shutil.copytree(root/"drivers/java",project,ignore=shutil.ignore_patterns("target"))
            # The outer production guard retains CPU/IO/physical-memory monitoring.
            # Only the Maven child receives the smaller boundary-test address limit.
            script=directory/"package.py"
            script.write_text("import os,resource,subprocess,sys\nfrom scripts.build import build_environment\nbudget=int(sys.argv[2])\nhard=resource.getrlimit(resource.RLIMIT_AS)[1]\nif hard!=resource.RLIM_INFINITY:budget=min(budget,hard)\nprint('effective_budget_bytes',budget,flush=True)\ndef limits():resource.setrlimit(resource.RLIMIT_AS,(budget,budget))\nraise SystemExit(subprocess.call(['mvn','-B','-T','1','-f',sys.argv[1],'clean','package'],env=build_environment(os.environ,1,budget),preexec_fn=limits))\n")
            for budget in (2*GIB,int(2.6*GIB)):
                with self.subTest(budget=budget):
                    command=[sys.executable,str(root/"scripts/build.py"),"command","--",sys.executable,str(script),str(project/"pom.xml"),str(budget)]
                    result=subprocess.run(command,cwd=root,env={**os.environ,"PYTHONPATH":str(root)},capture_output=True,text=True,timeout=240)
                    self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-8000:])
                    import zipfile
                    with zipfile.ZipFile(project/"target/sdk-worker-0.1.0.jar") as jar:
                        self.assertIn("io/github/storagevalidation/WorkerMain.class",jar.namelist())


if __name__ == "__main__":
    unittest.main()
