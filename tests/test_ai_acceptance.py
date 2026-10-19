#!/usr/bin/env python3
"""The AI acceptance entry must reject missing/skipped tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/test_ai.py"
PATTERNS = ("test_onnx_baseline.py", "test_torch_onnx_parity.py", "test_tf_onnx_parity.py")


class AiAcceptanceTests(unittest.TestCase):
    def run_fixture(self, body, omit_last=False):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            directory = Path(tmp)
            for index, name in enumerate(PATTERNS):
                if omit_last and index == 2:
                    continue
                (directory / name).write_text(
                    "import unittest\nclass Fixture(unittest.TestCase):\n"
                    "    def test_behavior(self):\n        " + body + "\n",
                    encoding="utf-8",
                )
            report = directory / "results.json"
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--tests-dir", str(directory),
                 "--report", str(report)],
                capture_output=True, text=True, timeout=30,
            )
            document = json.loads(report.read_text()) if report.exists() else None
            return result, document

    def test_passing_all_suites_is_accepted(self):
        result, document = self.run_fixture("self.assertEqual(1, 1)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(document)
        self.assertEqual(document["status"], "pass")
        self.assertEqual(document["tests_run"], 3)
        self.assertEqual(document["skipped"], [])

    def test_skips_are_nonzero_even_when_unittest_is_successful(self):
        result, document = self.run_fixture("self.skipTest('dependency missing')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(document, result.stderr)
        self.assertEqual(document["status"], "fail")
        self.assertEqual(len(document["skipped"]), 3)

    def test_failed_tests_are_nonzero(self):
        result, document = self.run_fixture("self.fail('output mismatch')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(document, result.stderr)
        self.assertEqual(document["status"], "fail")
        self.assertEqual(len(document["failures"]), 3)

    def test_dependency_roots_are_consumed_by_tensorflow_exporter(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temporary:
            root = Path(temporary)
            tf_root = root / "tf-root"
            onnx_root = root / "onnx-root"
            tf_python = tf_root / "lib/python3.11/site-packages"
            onnx_python = onnx_root / "usr/lib/python3/dist-packages"
            tf_python.joinpath("tensorflow").mkdir(parents=True)
            onnx_python.joinpath("onnxruntime").mkdir(parents=True)
            onnx_lib = onnx_root / "usr/lib/x86_64-linux-gnu"
            onnx_lib.mkdir(parents=True)
            (onnx_lib / "libonnxruntime.so").write_bytes(b"fixture")
            probe = (
                "import importlib.util, os; "
                f"spec=importlib.util.spec_from_file_location('tf_export', {str(ROOT / 'tools/xtos-onnx-baseline/tf_export.py')!r}); "
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "module.configure_dependency_environment(); "
                "print(os.environ.get('PYTHONPATH','')); print(os.environ.get('LD_LIBRARY_PATH',''))"
            )
            environment = os.environ.copy()
            environment.update({"XTOS_TF_ROOT": str(tf_root), "XTOS_ONNX_ROOT": str(onnx_root)})
            result = subprocess.run([sys.executable, "-c", probe], env=environment,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(tf_python), result.stdout)
        self.assertIn(str(onnx_python), result.stdout)
        self.assertIn(str(onnx_lib), result.stdout)

    def test_tensorflow_benchmark_rejects_unverified_dependency_result(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".build") as temporary:
            environment = os.environ.copy()
            environment["XTOS_TF_DISABLE"] = "1"
            environment["BUILD_ROOT"] = str(Path(temporary).relative_to(ROOT))
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/benchmark_tf_onnx.sh")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unverified", result.stderr.lower())

    def test_missing_suite_is_nonzero(self):
        result, document = self.run_fixture("self.assertTrue(True)", omit_last=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(document, result.stderr)
        self.assertEqual(document["status"], "fail")
        self.assertIn("test_tf_onnx_parity.py", document["missing_suites"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
