import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
TOOL = Path(__file__).with_name("baseline.py")
BUILD_ROOT = ROOT / ".build"
ONNX_ROOT = Path(
    os.environ.get("XTOS_ONNX_ROOT", str(ROOT / ".build" / "onnx-root"))
)


def runtime_environment():
    environment = os.environ.copy()
    root = Path(environment.get("XTOS_ONNX_ROOT", str(ONNX_ROOT)))
    python_candidates = [
        root / "usr" / "lib" / "python3" / "dist-packages",
        root,
    ]
    library_candidates = [
        root / "usr" / "lib" / "x86_64-linux-gnu",
        root / "onnxruntime" / "capi",
        root,
    ]
    python_root = next(
        (candidate for candidate in python_candidates if (candidate / "onnxruntime").is_dir()),
        None,
    )
    library_root = next(
        (
            candidate
            for candidate in library_candidates
            if candidate.is_dir() and any(candidate.glob("libonnxruntime*.so*"))
        ),
        None,
    )
    if python_root is not None:
        environment["PYTHONPATH"] = ":".join(
            part for part in (str(python_root), environment.get("PYTHONPATH", "")) if part
        )
    if library_root is not None:
        environment["LD_LIBRARY_PATH"] = ":".join(
            part for part in (str(library_root), environment.get("LD_LIBRARY_PATH", "")) if part
        )
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def runtime_available():
    probe = subprocess.run(
        [sys.executable, "-c", "import onnxruntime, numpy"],
        env=runtime_environment(),
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


class OnnxBaselineTests(unittest.TestCase):
    def run_tool(self, *arguments, disable_runtime=False):
        environment = runtime_environment()
        if disable_runtime:
            environment["XTOS_ONNX_DISABLE"] = "1"
        return subprocess.run(
            [sys.executable, str(TOOL), *arguments],
            env=environment,
            capture_output=True,
            text=True,
        )

    def parse_output(self, result):
        self.assertTrue(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def test_fixed_model_fixture_hash_is_stable(self):
        fixture = TOOL.with_name("fixtures") / "fixed_classifier.onnx"
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "6e6226cf772934a3f7a398ecb55ffd290503497d91803ff499a7aa28bf90a113",
        )

    def test_missing_runtime_is_explicitly_unverified(self):
        result = self.run_tool("--probe-only", disable_runtime=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "unverified")
        self.assertEqual(document["safe_action"], "no_action")
        self.assertEqual(document["reason"], "runtime_disabled")

    @unittest.skipUnless(runtime_available(), "ONNX Runtime fixture is unavailable")
    def test_fixed_model_matches_reference_and_records_latency(self):
        result = self.run_tool("--iterations", "5", "--deadline-ms", "1000")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["safe_action"], "action_allowed")
        self.assertEqual(document["execution_provider"], "CPUExecutionProvider")
        self.assertEqual(document["input_shape"], [1, 4])
        self.assertEqual(document["threads"], {"intra_op": 1, "inter_op": 1})
        self.assertEqual(len(document["model_sha256"]), 64)
        self.assertTrue(document["cpu_model"])
        self.assertLessEqual(document["max_abs_error"], 1e-5)
        self.assertEqual(document["latency_ns"]["samples"], 5)
        for percentile in ("p50", "p95", "p99", "p99_9", "max"):
            self.assertIn(percentile, document["latency_ns"])

    @unittest.skipUnless(runtime_available(), "ONNX Runtime fixture is unavailable")
    def test_input_quality_returns_safe_no_action(self):
        result = self.run_tool("--quality", "0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "degraded_input_quality")
        self.assertEqual(document["safe_action"], "no_action")

    @unittest.skipUnless(runtime_available(), "ONNX Runtime fixture is unavailable")
    def test_low_confidence_returns_safe_no_action(self):
        result = self.run_tool("--confidence-threshold", "0.999999")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "low_confidence")
        self.assertEqual(document["safe_action"], "no_action")
        self.assertLess(document["confidence"], document["confidence_threshold"])

    @unittest.skipUnless(runtime_available(), "ONNX Runtime fixture is unavailable")
    def test_deadline_miss_returns_safe_no_action(self):
        result = self.run_tool("--iterations", "1", "--deadline-ms", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "timeout")
        self.assertEqual(document["safe_action"], "no_action")
        self.assertGreaterEqual(document["timed_out_count"], 1)

    @unittest.skipUnless(runtime_available(), "ONNX Runtime fixture is unavailable")
    def test_model_load_failure_returns_safe_no_action(self):
        missing_model = str(BUILD_ROOT / "does-not-exist.onnx")
        result = self.run_tool("--model-path", missing_model)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "model_load_failed")
        self.assertEqual(document["safe_action"], "no_action")


if __name__ == "__main__":
    unittest.main()
