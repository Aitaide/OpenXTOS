#!/usr/bin/env python3
"""Test TensorFlow/TFLite ONNX export parity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TF_EXPORT = ROOT / "tools" / "xtos-onnx-baseline" / "tf_export.py"


class TFOnnxParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None

    def test_missing_tf_is_explicitly_unverified(self) -> None:
        """When TensorFlow is unavailable, tool returns unverified."""
        env = os.environ.copy()
        env["XTOS_TF_DISABLE"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(TF_EXPORT), "--probe-only"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        doc = json.loads(result.stdout)
        self.assertEqual(doc["schema"], "xtos-tf-onnx-parity-v1")
        self.assertEqual(doc["status"], "unverified")
        self.assertEqual(doc["safe_action"], "no_action")
        self.assertIn(doc["reason"], ["tf_disabled", "tf_or_runtime_unavailable"])

    def test_exported_tflite_matches_onnx_runtime_output(self) -> None:
        """TensorFlow → TFLite → inference matches ONNX Runtime CPU output."""
        # Discover isolated dependency roots
        tf_root = os.environ.get("XTOS_TF_ROOT")
        onnx_root = os.environ.get("XTOS_ONNX_ROOT")

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        if tf_root:
            env["XTOS_TF_ROOT"] = tf_root
        if onnx_root:
            env["XTOS_ONNX_ROOT"] = onnx_root

        result = subprocess.run(
            [sys.executable, str(TF_EXPORT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # If dependencies unavailable, must return unverified
        if result.returncode == 0:
            doc = json.loads(result.stdout)
            if doc.get("status") == "unverified":
                self.skipTest(f"TensorFlow or runtime unavailable: {doc.get('reason')}")
                return

        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        doc = json.loads(result.stdout)
        self.assertEqual(doc["schema"], "xtos-tf-onnx-parity-v1")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["safe_action"], "action_allowed")

        # Verify TFLite and ONNX Runtime outputs match
        self.assertIn("tf_output", doc)
        self.assertIn("onnx_output", doc)
        self.assertIn("max_abs_error", doc)
        self.assertIn("mean_abs_error", doc)

        # Fixed input [2.0, 0.0, -1.0, 0.0] should produce deterministic output
        max_error = doc["max_abs_error"]
        self.assertLessEqual(
            max_error,
            1e-5,
            f"TFLite/ONNX max absolute error {max_error} exceeds threshold 1e-5",
        )

        # Predicted class must match
        tf_output = doc["tf_output"]
        onnx_output = doc["onnx_output"]
        tf_class = tf_output.index(max(tf_output))
        onnx_class = onnx_output.index(max(onnx_output))
        self.assertEqual(
            tf_class,
            onnx_class,
            f"TFLite predicted class {tf_class}, ONNX predicted {onnx_class}",
        )

        # Verify metadata
        self.assertIn("tensorflow_version", doc)
        self.assertIn("input_shape", doc)
        self.assertIn("output_shape", doc)
        self.assertEqual(doc["input_shape"], [1, 4])
        self.assertEqual(doc["output_shape"], [1, 3])
        self.assertIn("execution_provider", doc)
        self.assertEqual(doc["execution_provider"], "CPUExecutionProvider")
        self.assertIn("tflite_model_bytes", doc)
        self.assertGreater(doc["tflite_model_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
