"""Run the pure TypeScript template tests offline in three viewer time zones."""
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ManagedAppTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node is missing; template tests require native TypeScript stripping")
        probe = subprocess.run(
            [cls.node, "--experimental-strip-types", "-e", "0"],
            capture_output=True, text=True, timeout=15,
        )
        if probe.returncode:
            error = probe.stderr.strip()
            if "--experimental-strip-types" in error and any(
                text in error.lower() for text in ("bad option", "unknown option", "unrecognized option")
            ):
                raise unittest.SkipTest(f"Node is too old for native TypeScript stripping: {error}")
            raise RuntimeError(f"Node type-stripping probe failed:\n{probe.stdout}{probe.stderr}")

    def check_timezone(self, timezone):
        result = subprocess.run(
            [self.node, "--experimental-strip-types", "--test", "tests/managed_app_templates.test.mjs"],
            cwd=ROOT, env={**os.environ, "TZ": timezone},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"TZ={timezone}\n{result.stdout}{result.stderr}")
        # a run that executes nothing also exits 0: require the tests to have run, none skipped
        summary = dict(re.findall(r"^(?:ℹ|#) (tests|pass|skipped|todo) (\d+)$", result.stdout, re.M))
        self.assertGreaterEqual(int(summary.get("pass", 0)), 30, f"TZ={timezone}: {summary}")
        self.assertEqual((summary.get("skipped"), summary.get("todo")), ("0", "0"), f"TZ={timezone}: {summary}")

    def test_america_new_york(self):
        self.check_timezone("America/New_York")

    def test_asia_tokyo(self):
        self.check_timezone("Asia/Tokyo")

    def test_utc(self):
        self.check_timezone("UTC")


if __name__ == "__main__":
    unittest.main()
