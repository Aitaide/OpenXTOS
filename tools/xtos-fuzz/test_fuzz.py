#!/usr/bin/env python3
"""Unit tests for the XTOS fuzz tooling (tools/xtos-fuzz).

Covers:
  * gen_corpus.py: determinism, corpus layout, mutation loop never raises
  * harness binaries: build + run over a generated corpus, any crash fails
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


class CorpusTests(unittest.TestCase):
    def test_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_a = Path(tmp) / "a"
            out_b = Path(tmp) / "b"
            for out in (out_a, out_b):
                result = subprocess.run(
                    [sys.executable, str(HERE / "gen_corpus.py"),
                     "--out", str(out), "--rounds", "30", "--seed", "99"],
                    capture_output=True, text=True, check=True)
                self.assertIn("generated", result.stdout)
            names_a = sorted(p.name for p in (out_a / "csv").iterdir())
            names_b = sorted(p.name for p in (out_b / "csv").iterdir())
            self.assertEqual(names_a, names_b)
            for name_a, name_b in zip(names_a, names_b):
                self.assertEqual((out_a / "csv" / name_a).read_bytes(),
                                 (out_b / "csv" / name_b).read_bytes(),
                                 f"corpus file {name_a} not deterministic")

    def test_check_mode_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "corpus"
            subprocess.run(
                [sys.executable, str(HERE / "gen_corpus.py"),
                 "--out", str(out), "--rounds", "5", "--seed", "5"],
                capture_output=True, text=True, check=True)
            result = subprocess.run(
                [sys.executable, str(HERE / "gen_corpus.py"),
                 "--out", str(out), "--check"],
                capture_output=True, text=True, check=True)
            self.assertIn("corpus_files", result.stdout)
            self.assertIn("csv=", result.stdout)
            self.assertIn("packet=", result.stdout)


class HarnessBuildTests(unittest.TestCase):
    """Build harnesses with sanitizers and run a small corpus through them.

    Requires gcc with ASan/UBSan (standard on the dev hosts and CI images).
    """

    def _make(self, target):
        return subprocess.run(
            ["make", "-C", str(HERE), target],
            capture_output=True, text=True)

    def test_check_target_runs_corpus_without_crash(self):
        result = self._make("check")
        self.assertEqual(result.returncode, 0,
                         f"fuzz check failed:\n{result.stdout}\n{result.stderr}")
        self.assertIn("fuzz_check=pass", result.stdout)


if __name__ == "__main__":
    unittest.main()
