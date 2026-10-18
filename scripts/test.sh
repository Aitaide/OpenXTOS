#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
cleanup() {
    for component in sdk/libxtos-bci sdk/libxtos-rt sdk/libxtos-signal tools/xtos-eeg-pipeline tools/xtos-signal-benchmark tools/xtos-fuzz tools/xtos-sbom; do
        make -C "$ROOT/$component" clean >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT
make -C "$ROOT/sdk/libxtos-bci" clean all
make -C "$ROOT/sdk/libxtos-rt" clean test all
make -C "$ROOT/sdk/libxtos-signal" clean test all
"$ROOT/scripts/check_abi.sh"
python3 -m unittest discover -s "$ROOT/sdk/libxtos-signal/tests" -p 'test_signal_reference.py' -v
make -C "$ROOT/tools/xtos-eeg-pipeline" clean test
make -C "$ROOT/tools/xtos-signal-benchmark" clean test
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-replay" -p 'test_replay.py' -v
make -C "$ROOT/tools/xtos-fuzz" check
python3 -m unittest discover -s "$ROOT/tools/xtos-sbom" -p 'test_sbom.py' -v
python3 "$ROOT/tools/xtos-sbom/gen_sbom.py" --check
python3 "$ROOT/tests/test_native_tools_persist.py"
python3 "$ROOT/tests/test_native_tools_rt.py"
python3 "$ROOT/tests/test_native_tools_ui.py"
python3 "$ROOT/tests/test_native_tools_clock.py"
python3 "$ROOT/tests/test_native_tools_xpkg.py"
python3 "$ROOT/tests/test_native_tools_update.py"
python3 "$ROOT/tests/test_verify_xpkg.py"
python3 "$ROOT/tests/test_make_xpkg.py"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_userspace_repo.py' -v
python3 -m unittest discover -s "$ROOT/tests" -p 'test_release_index.py' -v
python3 "$ROOT/tests/test_live_build.py"
python3 "$ROOT/tests/test_live_runtime.py"
python3 "$ROOT/tests/test_live_fsck_policy.py"
python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_onnx_baseline.py' -v
python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_torch_onnx_parity.py' -v
python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_tf_onnx_parity.py' -v
printf 'user_space_tests=pass\n'
