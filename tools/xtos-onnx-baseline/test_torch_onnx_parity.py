import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
TOOL = Path(__file__).with_name("torch_export.py")


def runtime_environment():
    environment = os.environ.copy()
    roots = []
    for variable in ("XTOS_TORCH_ROOT", "XTOS_ONNX_ROOT", "XTOS_PROTOBUF_ROOT"):
        value = environment.get(variable)
        if value:
            roots.append(Path(value))
    python_paths = []
    library_paths = []
    for root in roots:
        for candidate in (
            root / "usr" / "lib" / "python3" / "dist-packages",
            root / "usr" / "lib" / "python3" / "site-packages",
            root,
        ):
            if (
                (candidate / "torch").is_dir()
                or (candidate / "onnxruntime").is_dir()
                or (candidate / "google").is_dir()
            ):
                python_paths.append(str(candidate))
        for candidate in (
            root / "usr" / "lib" / "x86_64-linux-gnu",
            root / "usr" / "lib",
            root / "onnxruntime" / "capi",
            root,
        ):
            if candidate.is_dir() and any(candidate.glob("*.so*")):
                library_paths.append(str(candidate))
    if python_paths:
        environment["PYTHONPATH"] = ":".join(
            python_paths + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [])
        )
    if library_paths:
        environment["LD_LIBRARY_PATH"] = ":".join(
            library_paths + ([environment["LD_LIBRARY_PATH"]] if environment.get("LD_LIBRARY_PATH") else [])
        )
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def runtime_available():
    probe = subprocess.run(
        [sys.executable, "-c", "import numpy, onnx, onnxruntime, torch"],
        env=runtime_environment(),
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


class TorchOnnxParityTests(unittest.TestCase):
    def run_tool(self, *arguments, disable_torch=False):
        environment = runtime_environment()
        if disable_torch:
            environment["XTOS_TORCH_DISABLE"] = "1"
        return subprocess.run(
            [sys.executable, str(TOOL), *arguments],
            env=environment,
            capture_output=True,
            text=True,
        )

    def parse_output(self, result):
        self.assertTrue(result.stdout, result.stderr)
        return json.loads(result.stdout)

    def test_missing_torch_is_explicitly_unverified(self):
        result = self.run_tool("--probe-only", disable_torch=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "unverified")
        self.assertEqual(document["safe_action"], "no_action")
        self.assertEqual(document["reason"], "torch_disabled")

    @unittest.skipUnless(runtime_available(), "PyTorch and ONNX Runtime fixtures are unavailable")
    def test_exported_onnx_matches_torch_eager_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "torch_classifier.onnx"
            result = self.run_tool("--output", str(output_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        document = self.parse_output(result)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["safe_action"], "action_allowed")
        self.assertEqual(document["execution_provider"], "CPUExecutionProvider")
        self.assertEqual(document["input_shape"], [1, 4])
        self.assertEqual(document["output_shape"], [1, 3])
        self.assertEqual(document["threads"], {"intra_op": 1, "inter_op": 1})
        self.assertLessEqual(document["max_abs_error"], 1e-5)
        self.assertLessEqual(document["mean_abs_error"], 1e-6)
        self.assertEqual(document["torch_predicted_index"], document["onnx_predicted_index"])
        self.assertEqual(len(document["model_sha256"]), 64)
        self.assertTrue(document["torch_version"])
        self.assertTrue(document["onnxruntime_version"])


if __name__ == "__main__":
    unittest.main()
