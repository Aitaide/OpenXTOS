#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
cleanup() {
    for component in sdk/libxtos-bci sdk/libxtos-rt sdk/libxtos-signal tools/xtos-eeg-pipeline tools/xtos-signal-benchmark tests/scheduler; do
        make -C "$ROOT/$component" clean >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT
make -C "$ROOT/sdk/libxtos-bci" clean all
make -C "$ROOT/sdk/libxtos-rt" clean test all
make -C "$ROOT/sdk/libxtos-signal" clean test all
python3 -m unittest discover -s "$ROOT/sdk/libxtos-signal/tests" -p 'test_signal_reference.py' -v
make -C "$ROOT/tools/xtos-eeg-pipeline" clean test
make -C "$ROOT/tools/xtos-signal-benchmark" clean test
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-replay" -p 'test_replay.py' -v
python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_onnx_baseline.py' -v
make -C "$ROOT/tests/scheduler" clean test
printf 'user_space_tests=pass\n'
