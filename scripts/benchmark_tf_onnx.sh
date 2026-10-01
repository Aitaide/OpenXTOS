#!/usr/bin/env bash
# Run TensorFlow/TFLite ONNX export parity benchmark
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-.build}"
OUTPUT_DIR="$ROOT/$BUILD_ROOT/benchmarks"
OUTPUT_FILE="$OUTPUT_DIR/xtos-tf-onnx-parity-v1.json"
TF_EXPORT="$ROOT/tools/xtos-onnx-baseline/tf_export.py"

# Isolated dependency roots
XTOS_TF_ROOT="${XTOS_TF_ROOT:-}"
XTOS_ONNX_ROOT="${XTOS_ONNX_ROOT:-}"

mkdir -p "$OUTPUT_DIR"

# Build Python path for isolated dependencies
PYTHONPATH_PARTS=()

if [[ -n "$XTOS_TF_ROOT" ]]; then
    for candidate in \
        "$XTOS_TF_ROOT/usr/lib/python3/dist-packages" \
        "$XTOS_TF_ROOT/usr/lib/python3/site-packages" \
        "$XTOS_TF_ROOT/lib/python3/dist-packages" \
        "$XTOS_TF_ROOT/lib/python3/site-packages"
    do
        [[ -d "$candidate" ]] && PYTHONPATH_PARTS+=("$candidate")
    done
fi

if [[ -n "$XTOS_ONNX_ROOT" ]]; then
    for candidate in \
        "$XTOS_ONNX_ROOT/usr/lib/python3/dist-packages" \
        "$XTOS_ONNX_ROOT/usr/lib/python3/site-packages" \
        "$XTOS_ONNX_ROOT/lib/python3/dist-packages" \
        "$XTOS_ONNX_ROOT/lib/python3/site-packages" \
        "$XTOS_ONNX_ROOT"/onnxruntime-*/onnxruntime
    do
        [[ -d "$candidate" ]] && PYTHONPATH_PARTS+=("$candidate")
    done
fi

# Build library path
LD_LIBRARY_PATH_PARTS=()

if [[ -n "$XTOS_TF_ROOT" ]]; then
    for candidate in \
        "$XTOS_TF_ROOT/usr/lib" \
        "$XTOS_TF_ROOT/usr/lib/x86_64-linux-gnu" \
        "$XTOS_TF_ROOT/lib" \
        "$XTOS_TF_ROOT/lib/x86_64-linux-gnu"
    do
        [[ -d "$candidate" ]] && LD_LIBRARY_PATH_PARTS+=("$candidate")
    done
fi

if [[ -n "$XTOS_ONNX_ROOT" ]]; then
    for candidate in \
        "$XTOS_ONNX_ROOT"/onnxruntime-*/onnxruntime/capi \
        "$XTOS_ONNX_ROOT/usr/lib" \
        "$XTOS_ONNX_ROOT/usr/lib/x86_64-linux-gnu"
    do
        [[ -d "$candidate" ]] && LD_LIBRARY_PATH_PARTS+=("$candidate")
    done
fi

# Join paths
export PYTHONPATH="${PYTHONPATH_PARTS[*]}"
PYTHONPATH="${PYTHONPATH// /:}"
export PYTHONPATH

export LD_LIBRARY_PATH="${LD_LIBRARY_PATH_PARTS[*]}"
LD_LIBRARY_PATH="${LD_LIBRARY_PATH// /:}"
export LD_LIBRARY_PATH

export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1

# Run benchmark
if ! python3 "$TF_EXPORT" --output "$OUTPUT_DIR/tf_classifier.tflite" >"$OUTPUT_FILE" 2>/dev/null; then
    echo "TensorFlow/TFLite parity benchmark failed" >&2
    exit 1
fi

# Validate output
if ! python3 -c "import json; doc=json.load(open('$OUTPUT_FILE')); assert doc['schema']=='xtos-tf-onnx-parity-v1'" 2>/dev/null; then
    echo "Invalid benchmark output schema" >&2
    exit 1
fi

cat "$OUTPUT_FILE"
