#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
ITERATIONS="${ITERATIONS:-1000}"
OUTPUT_DIR="$BUILD_ROOT/benchmarks"
OUTPUT_FILE="$OUTPUT_DIR/xtos-signal-baseline-v2.json"

mkdir -p "$OUTPUT_DIR"
make -C "$ROOT/tools/xtos-signal-benchmark" clean all
"$ROOT/tools/xtos-signal-benchmark/xtos-signal-benchmark" \
    --iterations "$ITERATIONS" --output "$OUTPUT_FILE"

commit="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'uncommitted')"
kernel="$(uname -srvm)"
cpu="$(awk -F: '/^model name[[:space:]]*:/ {print $2; exit}' /proc/cpuinfo | sed 's/^ *//' || true)"
compiler="$(cc --version | sed -n '1p')"
python3 - "$OUTPUT_FILE" "$commit" "$kernel" "$cpu" "$compiler" <<'PY'
import json
import sys
from pathlib import Path

path, commit, kernel, cpu, compiler = sys.argv[1:]
document = json.loads(Path(path).read_text())
document["git_commit"] = commit
document["host_kernel"] = kernel
document["cpu_model"] = cpu
document["compiler"] = compiler
Path(path).write_text(json.dumps(document, indent=2) + "\n")
PY
python3 - "$OUTPUT_FILE" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads(Path(sys.argv[1]).read_text())
required = {"benchmark", "window_length", "sample_rate_hz", "channels",
            "iterations", "threads", "compiler", "compiler_flags",
            "git_commit", "host_kernel", "cpu_model", "operations"}
missing = required.difference(document)
if missing or set(document["operations"]) != {"fft_power", "bandpass", "ssvep_feature", "p300_feature"}:
    raise SystemExit(f"invalid benchmark document: missing={sorted(missing)}")
print(f"benchmark={sys.argv[1]}")
PY
