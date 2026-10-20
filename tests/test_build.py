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
        """Compile/package the actual SDK worker at 2 and 2.6 GiB caps under the production pressure guard; expect genuine worker classes in each final jar."""
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary);project=directory/"java"
            shutil.copytree(root/"drivers/java",project,ignore=shutil.ignore_patterns("target"))
            # The outer production guard retains CPU/IO/physical-memory monitoring.
            # Only the Maven child receives the smaller boundary-test address limit.
            script=directory/"package.py"
            script.write_text("import os,resource,subprocess,sys\nfrom scripts.build import build_environment\nbudget=int(sys.argv[2])\ndef limits():resource.setrlimit(resource.RLIMIT_AS,(budget,budget))\nraise SystemExit(subprocess.call(['mvn','-B','-T','1','-f',sys.argv[1],'clean','package'],env=build_environment(os.environ,1,budget),preexec_fn=limits))\n")
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
