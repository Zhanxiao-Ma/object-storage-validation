"""Exercise the actual JVM under the address-space limits used by the build guard."""
import os
import resource
import shlex
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()
