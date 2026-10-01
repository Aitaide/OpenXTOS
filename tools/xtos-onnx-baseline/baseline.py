#!/usr/bin/env python3
"""Deterministic ONNX Runtime CPU soft-real-time baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = Path(__file__).with_name("fixtures") / "fixed_classifier.onnx"
ONNX_ROOT = Path(
    os.environ.get("XTOS_ONNX_ROOT", str(ROOT / ".build" / "onnx-root"))
)


def configure_bundled_runtime() -> None:
    """Use the optional locally extracted runtime without changing the host."""
    python_candidates = [
        ONNX_ROOT / "usr" / "lib" / "python3" / "dist-packages",
        ONNX_ROOT,
    ]
    library_candidates = [
        ONNX_ROOT / "usr" / "lib" / "x86_64-linux-gnu",
        ONNX_ROOT / "onnxruntime" / "capi",
        ONNX_ROOT,
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
        sys.path.insert(0, str(python_root))
    if library_root is not None:
        old_path = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = ":".join(
            part for part in (str(library_root), old_path) if part
        )


def json_output(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def unavailable(reason: str, error: str | None = None) -> int:
    document: dict[str, Any] = {
        "schema": "xtos-onnx-cpu-baseline-v1",
        "status": "unverified",
        "safe_action": "no_action",
        "reason": reason,
    }
    if error:
        document["error_type"] = error
    json_output(document)
    return 0


def load_runtime() -> tuple[Any, Any] | None:
    if os.environ.get("XTOS_ONNX_DISABLE") == "1":
        return None
    configure_bundled_runtime()
    try:
        import numpy as np
        import onnxruntime as ort
    except Exception as exc:  # Optional dependency: operationally unverified.
        return None
    if "CPUExecutionProvider" not in ort.get_available_providers():
        return None
    return np, ort


def percentile_ns(samples: list[int], quantile: float) -> int:
    ordered = sorted(samples)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return int(ordered[index])


def latency_summary(samples: list[int]) -> dict[str, int]:
    return {
        "samples": len(samples),
        "p50": percentile_ns(samples, 0.50),
        "p95": percentile_ns(samples, 0.95),
        "p99": percentile_ns(samples, 0.99),
        "p99_9": percentile_ns(samples, 0.999),
        "max": max(samples),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--deadline-ms", type=float, default=50.0)
    parser.add_argument("--quality", type=float, default=1.0)
    parser.add_argument("--confidence-threshold", type=float, default=0.5)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if not math.isfinite(args.deadline_ms) or args.deadline_ms < 0.0:
        parser.error("--deadline-ms must be finite and non-negative")
    if not math.isfinite(args.quality) or not 0.0 <= args.quality <= 1.0:
        parser.error("--quality must be between 0 and 1")
    if not math.isfinite(args.confidence_threshold) or not 0.0 <= args.confidence_threshold <= 1.0:
        parser.error("--confidence-threshold must be between 0 and 1")
    return args


def main() -> int:
    args = parse_args()
    runtime = load_runtime()
    if runtime is None:
        return unavailable(
            "runtime_disabled"
            if os.environ.get("XTOS_ONNX_DISABLE") == "1"
            else "runtime_unavailable"
        )
    np, ort = runtime
    available_providers = ort.get_available_providers()
    if args.probe_only:
        json_output(
            {
                "schema": "xtos-onnx-cpu-baseline-v1",
                "status": "ready",
                "safe_action": "no_action",
                "onnxruntime_version": ort.__version__,
                "execution_provider": "CPUExecutionProvider",
                "available_providers": available_providers,
            }
        )
        return 0

    base = {
        "schema": "xtos-onnx-cpu-baseline-v1",
        "model_path": str(args.model_path),
        "execution_provider": "CPUExecutionProvider",
        "onnxruntime_version": ort.__version__,
        "input_shape": [1, 4],
        "threads": {"intra_op": 1, "inter_op": 1},
        "cpu_model": platform.processor() or platform.machine(),
        "model_sha256": hashlib.sha256(args.model_path.read_bytes()).hexdigest()
        if args.model_path.is_file()
        else "",
        "iterations_requested": args.iterations,
        "deadline_ms": args.deadline_ms,
        "quality": args.quality,
        "confidence_threshold": args.confidence_threshold,
    }
    if args.quality < 0.8:
        base.update({"status": "degraded_input_quality", "safe_action": "no_action"})
        json_output(base)
        return 0

    try:
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = 1
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(
            str(args.model_path),
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        base.update(
            {
                "status": "model_load_failed",
                "safe_action": "no_action",
                "error_type": type(exc).__name__,
            }
        )
        json_output(base)
        return 0

    input_name = session.get_inputs()[0].name
    output_name = session.get_outputs()[0].name
    input_data = np.asarray([[2.0, 0.0, -1.0, 0.0]], dtype=np.float32)
    expected = np.asarray(
        [[0.8437947034835815, 0.11419519782066345, 0.04201006868481636]],
        dtype=np.float32,
    )
    latencies: list[int] = []
    timed_out_count = 0
    output = None
    for _ in range(args.iterations):
        started = time.perf_counter_ns()
        output = session.run([output_name], {input_name: input_data})[0]
        elapsed = time.perf_counter_ns() - started
        latencies.append(elapsed)
        if elapsed / 1_000_000.0 > args.deadline_ms:
            timed_out_count += 1

    actual = np.asarray(output, dtype=np.float32)
    max_abs_error = float(np.max(np.abs(actual - expected)))
    probabilities = actual[0]
    predicted_index = int(np.argmax(probabilities))
    confidence = float(probabilities[predicted_index])
    base.update(
        {
            "status": "ok",
            "safe_action": "action_allowed",
            "input_name": input_name,
            "output_name": output_name,
            "output": actual.tolist(),
            "expected_output": expected.tolist(),
            "max_abs_error": max_abs_error,
            "predicted_index": predicted_index,
            "confidence": confidence,
            "timed_out_count": timed_out_count,
            "latency_ns": latency_summary(latencies),
        }
    )
    if timed_out_count:
        base["status"] = "timeout"
        base["safe_action"] = "no_action"
    elif confidence < args.confidence_threshold:
        base["status"] = "low_confidence"
        base["safe_action"] = "no_action"
    json_output(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
