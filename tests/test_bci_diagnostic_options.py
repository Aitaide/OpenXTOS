"""Diagnostic command options tested before transport CLI integration."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "tools/xtos-cyton"


class DiagnosticOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.binary = Path(cls.tmp.name) / "options"
        result = subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                 "-pedantic", str(HERE / "test_diagnostic_options.c"),
                                 "-o", str(cls.binary)], capture_output=True, text=True)
        if result.returncode:
            cls.tmp.cleanup()
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_transport_timeout_bound(self):
        result = subprocess.run([str(self.binary), "transport_timeout_bound"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "transport_timeout_bound")

    def test_window_bounds(self):
        result = subprocess.run([str(self.binary), "window_bounds"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "window_bounds")

    def test_invalid(self):
        result = subprocess.run([str(self.binary), "invalid"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "invalid")

    def test_valid(self):
        result = subprocess.run([str(self.binary), "valid"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "valid")


if __name__ == "__main__":
    unittest.main()
