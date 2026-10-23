import json
import subprocess
import sys
import unittest
from pathlib import Path


TOOL = Path(__file__).with_name("replay.py")
FIXTURES = Path(__file__).with_name("fixtures")


class ReplayTests(unittest.TestCase):
    def run_check(self, fixture_name):
        return subprocess.run(
            [sys.executable, str(TOOL), "--input", str(FIXTURES / fixture_name), "--check"],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_valid_fixture_outputs_summary(self):
        result = self.run_check("fixture.csv")
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["interval_count"], 2)
        self.assertEqual(summary["min_interval_ns"], 4_000_000)
        self.assertEqual(summary["max_interval_ns"], 4_000_000)

    def test_non_monotonic_fixture_fails(self):
        result = self.run_check("bad_non_monotonic.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("strictly increasing", result.stderr)

    def test_wrong_column_count_fixture_fails(self):
        result = self.run_check("bad_columns.csv")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 3 columns", result.stderr)


if __name__ == "__main__":
    unittest.main()
