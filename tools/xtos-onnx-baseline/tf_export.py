#!/usr/bin/env python3
"""Export a fixed TensorFlow model to TFLite and compare with ONNX Runtime."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".build" / "benchmarks" / "tf_classifier.tflite"


def configure_dependency_environment() -> None:
    """Add isolated TensorFlow/ONNX roots to import and loader paths."""
    python_paths = []
    library_paths = []
    roots = []
    for variable in ("XTOS_TF_ROOT", "XTOS_ONNX_ROOT"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    for root in roots:
        python_candidates = [
            root / "usr/lib/python3/dist-packages",
            root / "usr/lib/python3/site-packages",
            root / "lib/python3/dist-packages",
            root / "lib/python3/site-packages",
            root,
        ]
        for pattern in ("usr/lib/python*/dist-packages", "usr/lib/python*/site-packages",
                        "lib/python*/dist-packages", "lib/python*/site-packages"):
            python_candidates.extend(root.glob(pattern))
        for candidate in python_candidates:
            if candidate.is_dir():
                python_paths.append(str(candidate))
        for candidate in (
            root / "usr/lib/x86_64-linux-gnu",
            root / "usr/lib",
            root / "lib/x86_64-linux-gnu",
            root / "lib",
            root / "onnxruntime/capi",
        ):
            if candidate.is_dir():
                library_paths.append(str(candidate))
    if python_paths:
        existing = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = ":".join(python_paths + ([existing] if existing else []))
        for path in reversed(python_paths):
            if path not in sys.path:
                sys.path.insert(0, path)
    if library_paths:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(library_paths + ([existing] if existing else []))


def json_output(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def unavailable(reason: str) -> int:
    json_output(
        {
            "schema": "xtos-tf-onnx-parity-v1",
            "status": "unverified",
            "safe_action": "no_action",
            "reason": reason,
        }
    )
    return 0


def load_dependencies() -> tuple[Any, Any, Any] | None:
    """Load TensorFlow, NumPy, and ONNX Runtime."""
    if os.environ.get("XTOS_TF_DISABLE") == "1":
        return None
    configure_dependency_environment()

    try:
        import numpy as np
        import onnxruntime as ort
        import tensorflow as tf
    except Exception:
        return None
    
    if "CPUExecutionProvider" not in ort.get_available_providers():
        return None
    
    return np, ort, tf


def create_fixed_model(tf: Any) -> Any:
    """Create a fixed 3-class classifier: Linear(4->3) -> Softmax."""
    model = tf.keras.Sequential([
        tf.keras.layers.Dense(
            3,
            activation=None,
            use_bias=True,
            kernel_initializer=tf.keras.initializers.Constant([
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0],
            ]),
            bias_initializer=tf.keras.initializers.Zeros(),
            input_shape=(4,),
        ),
        tf.keras.layers.Softmax(),
    ])
    model.build(input_shape=(None, 4))
    return model


def convert_to_tflite(model: Any, tf: Any, output_path: Path) -> int:
    """Convert TensorFlow model to TFLite format."""
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = []  # No quantization
    # Keras prints its SavedModel summary; preserve it on stderr, not in JSON.
    with redirect_stdout(sys.stderr):
        tflite_model = converter.convert()
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(tflite_model)
    
    return len(tflite_model)


def run_tflite_inference(tflite_path: Path, input_data: Any, tf: Any) -> Any:
    """Run TFLite inference with fixed input."""
    interpreter = tf.lite.Interpreter(model_path=str(tflite_path))
    interpreter.allocate_tensors()
    
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()
    
    interpreter.set_tensor(input_details[0]["index"], input_data)
    interpreter.invoke()
    
    return interpreter.get_tensor(output_details[0]["index"])


def run_onnx_runtime(fixed_classifier_onnx: Path, input_data: Any, ort: Any) -> Any:
    """Run ONNX Runtime inference with fixed model."""
    sess_options = ort.SessionOptions()
    sess_options.intra_op_num_threads = 1
    sess_options.inter_op_num_threads = 1
    sess_options.log_severity_level = 3
    
    session = ort.InferenceSession(
        str(fixed_classifier_onnx),
        sess_options=sess_options,
        providers=["CPUExecutionProvider"],
    )
    
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: input_data})
    return outputs[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--probe-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dependencies = load_dependencies()
    
    if dependencies is None:
        return unavailable(
            "tf_disabled"
            if os.environ.get("XTOS_TF_DISABLE") == "1"
            else "tf_or_runtime_unavailable"
        )
    
    np, ort, tf = dependencies
    
    # Set single-threaded execution
    tf.config.threading.set_intra_op_parallelism_threads(1)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    
    if args.probe_only:
        json_output(
            {
                "schema": "xtos-tf-onnx-parity-v1",
                "status": "ready",
                "safe_action": "no_action",
                "tensorflow_version": tf.__version__,
                "onnxruntime_version": ort.__version__,
                "execution_provider": "CPUExecutionProvider",
            }
        )
        return 0
    
    # Create fixed model
    model = create_fixed_model(tf)
    
    # Fixed input: [2.0, 0.0, -1.0, 0.0]
    input_data = np.array([[2.0, 0.0, -1.0, 0.0]], dtype=np.float32)
    
    # Get TensorFlow eager output
    tf_output = model.predict(input_data, verbose=0)[0].tolist()
    
    # Convert to TFLite
    tflite_bytes = convert_to_tflite(model, tf, args.output)
    
    # Run TFLite inference
    tflite_output = run_tflite_inference(args.output, input_data, tf)[0].tolist()
    
    # Run ONNX Runtime with fixed classifier
    fixed_classifier_onnx = ROOT / "tools" / "xtos-onnx-baseline" / "fixtures" / "fixed_classifier.onnx"
    if not fixed_classifier_onnx.exists():
        return unavailable("fixed_classifier_onnx_missing")
    
    onnx_output = run_onnx_runtime(fixed_classifier_onnx, input_data, ort)[0].tolist()
    
    # Calculate errors between TFLite and ONNX
    abs_errors = [abs(a - b) for a, b in zip(tflite_output, onnx_output)]
    max_abs_error = max(abs_errors)
    mean_abs_error = sum(abs_errors) / len(abs_errors)
    
    # Predicted classes
    tf_class = tf_output.index(max(tf_output))
    tflite_class = tflite_output.index(max(tflite_output))
    onnx_class = onnx_output.index(max(onnx_output))
    
    # Check if predictions match
    predictions_match = (tflite_class == onnx_class)
    
    # Determine status and action
    status = "ok" if (max_abs_error <= 1e-5 and predictions_match) else "error"
    safe_action = "action_allowed" if status == "ok" else "no_action"
    
    json_output(
        {
            "schema": "xtos-tf-onnx-parity-v1",
            "status": status,
            "safe_action": safe_action,
            "tensorflow_version": tf.__version__,
            "tf_output": tf_output,
            "tflite_output": tflite_output,
            "onnx_output": onnx_output,
            "max_abs_error": max_abs_error,
            "mean_abs_error": mean_abs_error,
            "tf_predicted_class": tf_class,
            "tflite_predicted_class": tflite_class,
            "onnx_predicted_class": onnx_class,
            "predictions_match": predictions_match,
            "input_shape": [1, 4],
            "output_shape": [1, 3],
            "tflite_model_bytes": tflite_bytes,
            "execution_provider": "CPUExecutionProvider",
            "threads": {"intra_op": 1, "inter_op": 1},
            "cpu_model": platform.processor() or "unknown",
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip() or "unknown",
        }
    )
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
