#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
OUTPUT_DIR="$BUILD_ROOT/benchmarks"
OUTPUT_FILE="$OUTPUT_DIR/xtos-torch-onnx-parity-v1.json"
TORCH_ROOT="${XTOS_TORCH_ROOT:-$BUILD_ROOT/torch-root}"
ONNX_ROOT="${XTOS_ONNX_ROOT:-$BUILD_ROOT/onnx-wheel-root}"
PROTOBUF_ROOT="${XTOS_PROTOBUF_ROOT:-$ONNX_ROOT}"

mkdir -p "$OUTPUT_DIR"
PYTHONPATH="$TORCH_ROOT/usr/lib/python3/dist-packages:$ONNX_ROOT:$PROTOBUF_ROOT/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}" \
LD_LIBRARY_PATH="$TORCH_ROOT/usr/lib/x86_64-linux-gnu:$TORCH_ROOT/usr/lib:$ONNX_ROOT/onnxruntime/capi:$PROTOBUF_ROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/tools/xtos-onnx-baseline/torch_export.py" \
    --output "$OUTPUT_DIR/torch_classifier.onnx" > "$OUTPUT_FILE"

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
path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")

if document.get("schema") != "xtos-torch-onnx-parity-v1":
    raise SystemExit("invalid PyTorch/ONNX parity schema")
if document.get("status") == "unverified":
    if document.get("safe_action") != "no_action":
        raise SystemExit("unverified parity result must use no_action")
    print(f"torch_onnx_parity=unverified reason={document.get('reason', 'unknown')}")
    raise SystemExit(0)
required = {
    "safe_action", "execution_provider", "torch_version", "onnxruntime_version",
    "input_shape", "output_shape", "threads", "model_sha256", "max_abs_error",
    "mean_abs_error", "torch_predicted_index", "onnx_predicted_index", "git_commit",
}
missing = sorted(required.difference(document))
if missing:
    raise SystemExit(f"invalid PyTorch/ONNX parity document; missing={missing}")
if document["status"] != "ok" or document["safe_action"] != "action_allowed":
    raise SystemExit(
        f"PyTorch/ONNX parity did not complete: status={document['status']} "
        f"safe_action={document['safe_action']}"
    )
if document["execution_provider"] != "CPUExecutionProvider":
    raise SystemExit("PyTorch/ONNX parity did not use CPUExecutionProvider")
if document["threads"] != {"intra_op": 1, "inter_op": 1}:
    raise SystemExit("PyTorch/ONNX parity did not use one thread per pool")
if document["input_shape"] != [1, 4] or document["output_shape"] != [1, 3]:
    raise SystemExit("unexpected parity tensor shape")
if document["torch_predicted_index"] != document["onnx_predicted_index"]:
    raise SystemExit("PyTorch and ONNX predicted classes differ")
if document["max_abs_error"] > 1e-5 or document["mean_abs_error"] > 1e-6:
    raise SystemExit(
        f"PyTorch/ONNX output error too large: max={document['max_abs_error']} "
        f"mean={document['mean_abs_error']}"
    )
print(f"torch_onnx_parity={path}")
print(f"torch={document['torch_version']}")
print(f"onnxruntime={document['onnxruntime_version']}")
print(f"provider={document['execution_provider']}")
print(f"max_abs_error={document['max_abs_error']}")
PY
