#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
OUTPUT_DIR="$BUILD_ROOT/benchmarks"
OUTPUT_FILE="$OUTPUT_DIR/xtos-onnx-cpu-baseline-v1.json"
ITERATIONS="${ITERATIONS:-1000}"
DEADLINE_MS="${DEADLINE_MS:-50}"

mkdir -p "$OUTPUT_DIR"
ONNX_ROOT="${XTOS_ONNX_ROOT:-$BUILD_ROOT/onnx-root}"
PYTHONPATH="$ONNX_ROOT/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}" \
LD_LIBRARY_PATH="$ONNX_ROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tools/xtos-onnx-baseline/baseline.py" \
    --iterations "$ITERATIONS" --deadline-ms "$DEADLINE_MS" > "$OUTPUT_FILE"

python3 - "$OUTPUT_FILE" "$ROOT" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

path = Path(sys.argv[1])
root = Path(sys.argv[2])
document = json.loads(path.read_text())
document["git_commit"] = subprocess.run(
    ["git", "-C", str(root), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
required = {
    "schema", "status", "safe_action", "execution_provider",
    "onnxruntime_version", "input_shape", "threads", "model_sha256",
    "cpu_model", "latency_ns", "max_abs_error", "git_commit",
}
missing = sorted(required.difference(document))
if missing:
    raise SystemExit(f"invalid ONNX baseline document; missing={missing}")
if document["schema"] != "xtos-onnx-cpu-baseline-v1":
    raise SystemExit("invalid ONNX baseline schema")
if document["status"] != "ok" or document["safe_action"] != "action_allowed":
    raise SystemExit(
        f"ONNX baseline did not complete: status={document['status']} "
        f"safe_action={document['safe_action']}"
    )
if document["execution_provider"] != "CPUExecutionProvider":
    raise SystemExit("ONNX baseline did not use CPUExecutionProvider")
if document["max_abs_error"] > 1e-5:
    raise SystemExit(f"ONNX output error too large: {document['max_abs_error']}")
print(f"onnx_cpu_baseline={path}")
print(f"onnxruntime={document['onnxruntime_version']}")
print(f"provider={document['execution_provider']}")
print(f"latency_ns={json.dumps(document['latency_ns'], sort_keys=True)}")
PY
