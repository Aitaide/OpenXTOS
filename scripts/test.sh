#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
# The service smoke tests spawn ONNX workers in the selected locked runtime.
export XTOS_TEST_AI_PYTHON="${XTOS_TEST_AI_PYTHON:-${XTOS_AI_PYTHON:-}}"
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
python3 -m unittest discover -s "$ROOT/tests" -p 'test_eeg_loop.py' -v
make -C "$ROOT/tools/xtos-signal-benchmark" clean test
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-replay" -p 'test_replay.py' -v
make -C "$ROOT/tools/xtos-fuzz" check
python3 -m unittest discover -s "$ROOT/tools/xtos-sbom" -p 'test_sbom.py' -v
python3 "$ROOT/tools/xtos-sbom/gen_sbom.py" --check
python3 "$ROOT/tests/test_native_tools_persist.py"
python3 "$ROOT/tests/test_native_tools_rt.py"
python3 "$ROOT/tests/test_native_tools_rt_affinity.py"
python3 "$ROOT/tests/test_native_tools_gpu.py"
python3 "$ROOT/tests/test_native_tools_pkg_capacity.py"
python3 "$ROOT/tests/test_native_tools_ui.py"
python3 "$ROOT/tests/test_native_tools_clock.py"
python3 "$ROOT/tests/test_native_tools_cgroup.py"
if [[ "${EUID:-$(id -u)}" == 0 ]]; then
    python3 "$ROOT/tests/test_native_tools_runner.py"
    python3 "$ROOT/tests/test_action_gate.py"
    python3 "$ROOT/tests/test_action_executor.py"
else
    printf 'runner_fixture_tests=skipped reason=requires-root-for-child-chroot-and-identity-drop\n'
fi
python3 "$ROOT/tests/test_action_executor_resources.py"
python3 "$ROOT/tests/test_native_modelverify.py"
python3 "$ROOT/tests/test_runner_kernel_config.py"
python3 "$ROOT/tests/test_native_tools_xpkg.py"
python3 "$ROOT/tests/test_native_tools_update.py"
python3 "$ROOT/tests/test_verify_xpkg.py"
python3 "$ROOT/tests/test_make_xpkg.py"
python3 -m unittest discover -s "$ROOT/tests" -p 'test_userspace_repo.py' -v
python3 -m unittest discover -s "$ROOT/tests" -p 'test_release_index.py' -v
for suite in test_ai_runtime.py test_ai_service.py test_ai_client.py test_ai_worker.py test_ai_service_real.py test_ai_model_trust.py test_ai_model_revocation.py test_ai_signed_lifecycle.py test_ai_ticket_lifecycle.py test_ai_runtime_bundle.py test_ai_runtime_assembly.py; do
    python3 -m unittest discover -s "$ROOT/tests" -p "$suite" -v
done
python3 "$ROOT/tests/test_live_build.py"
python3 "$ROOT/tests/test_live_ai.py"
python3 "$ROOT/tests/test_live_acceptance.py"
python3 "$ROOT/tests/test_live_runtime.py"
python3 "$ROOT/tests/test_live_devices.py"
python3 "$ROOT/tests/test_verify_live_gpu.py"
python3 "$ROOT/tests/test_userspace_fetch.py"
python3 "$ROOT/tests/test_live_fsck_policy.py"
python3 "$ROOT/tests/test_ai_acceptance.py"
python3 "$ROOT/tests/test_ai_provenance.py"
if [[ -n "${XTOS_AI_PYTHON:-}" ]]; then
    "$XTOS_AI_PYTHON" "$ROOT/scripts/test_ai.py"
else
    python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_onnx_baseline.py' -v
    python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_torch_onnx_parity.py' -v
    python3 -m unittest discover -s "$ROOT/tools/xtos-onnx-baseline" -p 'test_tf_onnx_parity.py' -v
fi
printf 'user_space_tests=pass\n'
