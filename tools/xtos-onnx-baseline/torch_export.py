#!/usr/bin/env python3
"""Export a fixed PyTorch model and compare eager output with ONNX Runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".build" / "benchmarks" / "torch_classifier.onnx"


def json_output(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def unavailable(reason: str) -> int:
    json_output(
        {
            "schema": "xtos-torch-onnx-parity-v1",
            "status": "unverified",
            "safe_action": "no_action",
            "reason": reason,
        }
    )
    return 0


def load_dependencies() -> tuple[Any, Any, Any] | None:
    if os.environ.get("XTOS_TORCH_DISABLE") == "1":
        return None
    try:
        import numpy as np
        import onnx
        import onnxruntime as ort
        import torch
    except Exception:
        return None
    if "CPUExecutionProvider" not in ort.get_available_providers():
        return None
    return np, ort, torch


class FixedClassifier:
    def __init__(self, torch: Any) -> None:
        self.torch = torch
        self.module = torch.nn.Sequential(
            torch.nn.Linear(4, 3, bias=True),
            torch.nn.Softmax(dim=1),
        )
        with torch.no_grad():
            layer = self.module[0]
            layer.weight.copy_(torch.tensor(
                [[1.0, 0.0, 0.0, 0.0],
                 [0.0, 1.0, 0.0, 0.0],
                 [0.0, 0.0, 1.0, 0.0]],
                dtype=torch.float32,
            ))
            layer.bias.zero_()
        self.module.eval()


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
            "torch_disabled"
            if os.environ.get("XTOS_TORCH_DISABLE") == "1"
            else "torch_or_runtime_unavailable"
        )
    np, ort, torch = dependencies
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    if args.probe_only:
        json_output(
            {
                "schema": "xtos-torch-onnx-parity-v1",
                "status": "ready",
                "safe_action": "no_action",
                "torch_version": torch.__version__,
                "onnxruntime_version": ort.__version__,
                "execution_provider": "CPUExecutionProvider",
            }
        )
        return 0

    model = FixedClassifier(torch)
    input_data = torch.tensor([[2.0, 0.0, -1.0, 0.0]], dtype=torch.float32)
    with torch.no_grad():
        torch_output = model.module(input_data).cpu().numpy()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model.module,
        input_data,
        str(args.output),
        input_names=["input"],
        output_names=["probabilities"],
        opset_version=13,
        dynamo=False,
    )
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        str(args.output),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    onnx_output = np.asarray(
        session.run(["probabilities"], {"input": input_data.numpy()})[0],
        dtype=np.float32,
    )
    absolute_error = np.abs(torch_output - onnx_output)
    document = {
        "schema": "xtos-torch-onnx-parity-v1",
        "status": "ok",
        "safe_action": "action_allowed",
        "torch_version": torch.__version__,
        "onnxruntime_version": ort.__version__,
        "execution_provider": "CPUExecutionProvider",
        "input_shape": list(input_data.shape),
        "output_shape": list(torch_output.shape),
        "threads": {"intra_op": 1, "inter_op": 1},
        "cpu_model": platform.processor() or platform.machine(),
        "model_path": str(args.output),
        "model_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "torch_output": torch_output.tolist(),
        "onnx_output": onnx_output.tolist(),
        "max_abs_error": float(np.max(absolute_error)),
        "mean_abs_error": float(np.mean(absolute_error)),
        "torch_predicted_index": int(np.argmax(torch_output[0])),
        "onnx_predicted_index": int(np.argmax(onnx_output[0])),
    }
    if document["max_abs_error"] > 1e-5 or document["mean_abs_error"] > 1e-6:
        document["status"] = "output_mismatch"
        document["safe_action"] = "no_action"
    json_output(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
