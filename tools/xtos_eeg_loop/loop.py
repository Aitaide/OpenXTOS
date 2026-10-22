"""Fixture-only EEG preprocessing to inference smoke loop.

This module blocks action authorization for source_kind=fixture. It is not a
hardware acquisition adapter or an EEG classifier.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Callable, Any

EXPECTED_HEADER = "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt"
EXPECTED_INTERVAL_NS = 4_000_000
TOLERANCE = 0.01


class LoopError(ValueError):
    """The fixture contract or preprocessing quality gate was rejected."""


def _read_frames(path: Path):
    with Path(path).open("r", encoding="utf-8", newline="") as stream:
        if stream.readline().rstrip("\r\n") != EXPECTED_HEADER:
            raise LoopError("invalid EEG header: expected channels=2, 250Hz, nanovolt")
        rows = []
        for line, row in enumerate(csv.reader(stream), start=2):
            if len(row) != 3:
                raise LoopError(f"line {line}: expected 3 columns")
            try:
                timestamp = int(row[0])
                values = [float(row[1]), float(row[2])]
            except (TypeError, ValueError) as exc:
                raise LoopError(f"line {line}: invalid numeric frame") from exc
            if not 0 < timestamp <= (1 << 64) - 1 or not all(math.isfinite(value) and value.is_integer()
                    and -2147483648 <= value <= 2147483647 for value in values):
                raise LoopError(f"line {line}: timestamp and samples must be finite/positive")
            if rows:
                interval = timestamp - rows[-1]["timestamp_ns"]
                if interval <= 0:
                    raise LoopError(f"line {line}: timestamps must be strictly increasing")
                lower = EXPECTED_INTERVAL_NS * (1 - TOLERANCE)
                upper = EXPECTED_INTERVAL_NS * (1 + TOLERANCE)
                if not lower <= interval <= upper:
                    raise LoopError(f"line {line}: sampling interval outside quality gate")
            rows.append({"timestamp_ns": timestamp, "sequence": len(rows),
                         "channel_count": 2, "sample_rate_hz": 250,
                         "unit": "nanovolt", "quality_flags": 0,
                         "source_kind": "fixture", "samples": values})
            if len(rows) > 4096:
                raise LoopError("frame limit exceeded")
    if not rows:
        raise LoopError("fixture contains no frames")
    return rows


def _run_pipeline(frames, pipeline: Path):
    with tempfile.TemporaryDirectory(prefix="xtos-eeg-loop-") as directory:
        input_path = Path(directory) / "validated.csv"
        input_path.write_text(EXPECTED_HEADER + "\n" + "".join(
            f"{frame['timestamp_ns']},{int(frame['samples'][0])},{int(frame['samples'][1])}\n"
            for frame in frames), encoding="utf-8")
        output = Path(directory) / "filtered.csv"
        result = subprocess.run(
            [str(pipeline), "--input", str(input_path), "--strict-quality",
             "--bandpass", "5", "40", "--dump-filtered", str(output)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            raise LoopError(f"preprocessing quality gate failed: {result.stderr.strip()}")
        try:
            summary = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise LoopError("preprocessor returned invalid JSON") from exc
        if not isinstance(summary, dict):
            raise LoopError("preprocessor summary must be an object")
        if summary.get("status") != "ok":
            raise LoopError("preprocessor did not produce a clean quality result")
        counters = ("quality_rejected", "frames_in", "frames_out")
        if any(type(summary.get(name)) is not int or summary[name] < 0 for name in counters):
            raise LoopError("preprocessor summary counter must be a non-negative integer")
        if summary["quality_rejected"] != 0:
            raise LoopError("preprocessor did not produce a clean quality result")
        if summary["frames_in"] != len(frames):
            raise LoopError("preprocessor frames_in count mismatch")
        filtered = output.read_text(encoding="utf-8").splitlines()
        expected = "# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt"
        if not filtered or filtered[0] != expected:
            raise LoopError("filtered output header mismatch")
        rows = []
        for line in filtered[1:]:
            parts = line.split(",")
            if len(parts) != 3:
                raise LoopError("filtered output row shape mismatch")
            values = [float(parts[1]), float(parts[2])]
            if not all(math.isfinite(value) for value in values):
                raise LoopError("filtered output contains non-finite values")
            rows.append((int(parts[0]), values))
        if len(rows) != summary.get("frames_out") or len(rows) != len(frames):
            raise LoopError("filtered output count mismatch")
        if [row[0] for row in rows] != [frame["timestamp_ns"] for frame in frames]:
            raise LoopError("filtered timestamp correlation mismatch")
        return summary, rows


def fixture_features(frames):
    """Transport smoke: [mean0_uV, mean1_uV, rms0_uV, rms1_uV].

    This explicit fixture mapping has no claimed EEG classification accuracy.
    """
    if not frames:
        raise LoopError("feature mapping requires at least one frame")
    channels = [[], []]
    for frame in frames:
        values = frame["samples"]
        if len(values) != 2 or not all(math.isfinite(value) and abs(value) <= 1_000_000 for value in values):
            raise LoopError("feature mapping input is outside fixture bounds")
        for index, value in enumerate(values):
            channels[index].append(float(value) / 1000.0)
    means = [math.fsum(values) / len(values) for values in channels]
    rms = [math.sqrt(math.fsum(value * value for value in values) / len(values))
           for values in channels]
    return means + rms


def run_fixture_loop(input_path: Path, pipeline: Path | Callable = None,
                     socket_path: Path | None = None, execute_inference: bool = False,
                     request_fn: Callable | None = None) -> dict[str, Any]:
    """Validate fixture frames, preprocess them, and optionally smoke-call UDS.

    Even when the smoke call returns action_allowed, fixture input is always
    returned as no_action until a real EEG feature contract is separately
    specified and accepted.
    """
    frames = _read_frames(Path(input_path))
    if callable(pipeline):
        raise LoopError("pipeline callable injection is not supported in production loop")
    if pipeline is None:
        pipeline = Path(__file__).resolve().parents[1] / "xtos-eeg-pipeline/xtos-eeg-pipeline"
    summary, filtered = _run_pipeline(frames, Path(pipeline))
    features = fixture_features([{"samples": values} for _, values in filtered])
    result = {
        "status": "preprocessing_only", "safe_action": "no_action",
        "reason": "inference_not_requested", "source_kind": "fixture",
        "feature_mapping": "fixture-eeg-v1", "frames_in": summary["frames_in"],
        "frames_out": summary["frames_out"], "quality_rejected": summary["quality_rejected"],
        "frame_contract": frames, "features": features,
        "inference": {"status": "not_called"},
    }
    if execute_inference:
        if socket_path is None:
            raise LoopError("socket_path required when inference is requested")
        try:
            if request_fn is None:
                from xtos_ai_runtime.client import request as request_fn
            response = request_fn(socket_path, {
                "id": "fixture-eeg-1", "op": "infer", "model": "fixed-classifier",
                "input": [features], "deadline_ms": 3000,
            }, timeout=10)
        except (ImportError, OSError, TimeoutError, ValueError) as exc:
            result["inference"] = {"status": "unavailable", "safe_action": "no_action",
                                    "reason": "inference_unavailable", "output": None,
                                    "diagnostics": type(exc).__name__}
            result["safe_action"] = "no_action"
            result["reason"] = "inference_unavailable"
            return result
        result["inference"] = {
            "status": response.get("status"),
            "reason": response.get("reason"),
            "output": response.get("output"),
            "safe_action": "no_action",
        }
        result["safe_action"] = "no_action"
        result["reason"] = "fixture_action_blocked"
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--pipeline", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(run_fixture_loop(args.input, args.pipeline),
                         allow_nan=False, sort_keys=True))
    except (OSError, LoopError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "rejected", "safe_action": "no_action",
                          "reason": "data_loop_failed", "diagnostics": type(exc).__name__},
                         sort_keys=True))
        raise SystemExit(1)
