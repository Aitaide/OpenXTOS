#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
python3 "$ROOT/tests/test_scheduler_docs_currentness.py"
python3 "$ROOT/tests/test_sched_periodic.py"
python3 "$ROOT/tests/test_sched_bench_runner.py"
python3 "$ROOT/tests/test_sched_trace_parser.py"
python3 "$ROOT/tests/test_sched_preflight.py"
python3 "$ROOT/tests/test_sched_m0_gate.py"
python3 "$ROOT/tests/test_scheduler_class_readiness.py"
python3 "$ROOT/tests/test_sched_class_hooks.py"
python3 "$ROOT/tests/test_eeg_device_contract.py"
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_devices.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_serial_transport.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_network_transport.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_device_network_install.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_device_network_integration.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_cyton_protocol.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_cyton_serial_plugin.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_ganglion_protocol.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_ganglion_plugin.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_ganglion_stream.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_xtos_ganglion_ble.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_device_serial_install.py' -v
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_p4_device_plugin_integration.py' -v
python3 "$ROOT/tests/test_cyton_runtime.py"
bash "$ROOT/scripts/test_cyton_kernel_parser.sh"
python3 "$ROOT/tests/test_cyton_driver.py"
python3 "$ROOT/tests/test_cyton_kernel_integration.py"
python3 "$ROOT/tests/test_cyton_fuzz.py"
python3 "$ROOT/tests/test_bci_lifetime.py"
python3 "$ROOT/tests/test_bci_kbuild.py"
python3 "$ROOT/scripts/test_bci_frames.py"
python3 "$ROOT/tests/test_bci_snapshot_integration.py"
python3 "$ROOT/tests/test_bci_snapshot_kernel.py"
python3 "$ROOT/tests/test_bci_snapshot_sdk.py"
python3 "$ROOT/tests/test_bci_packaging.py"
python3 -B -m unittest discover -s "$ROOT/tests" -p 'test_eeg_stream_install.py' -v
python3 "$ROOT/tests/test_bci_uapi.py"
python3 "$ROOT/tests/test_bci_cli.py"
for suite in test_cyton_config.py test_bci_device_config.py test_bci_device_kernel.py test_bci_device_sdk.py test_bci_config_cli.py test_bci_main_config.py test_bci_main_calibration.py test_bci_main_diagnostic.py test_bci_diagnostic.py test_bci_diagnostic_options.py test_bci_diagnostic_cli.py test_bci_calibration.py test_bci_calibration_cli.py; do
    python3 "$ROOT/tests/$suite"
done
python3 "$ROOT/tools/xtos-cyton/test_cyton_diagnostic_serial.py"
make -C "$ROOT/tools/xtos-cyton" test
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-pipeline" -p 'test_openbci_cyton.py' -v
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-pipeline" -p 'test_cyton_capture.py' -v
python3 -m unittest discover -s "$ROOT/tools/xtos-eeg-pipeline" -p 'test_cyton_replay.py' -v
python3 -m unittest discover -s "$ROOT/tests" -p 'test_xtos_sched_model*.py' -v
# The service smoke tests spawn ONNX workers in the selected locked runtime.
export XTOS_TEST_AI_PYTHON="${XTOS_TEST_AI_PYTHON:-${XTOS_AI_PYTHON:-}}"
cleanup() {
    for component in sdk/libxtos-bci sdk/libxtos-rt sdk/libxtos-signal tools/xtos-eeg-pipeline tools/xtos-cyton tools/xtos-signal-benchmark tools/xtos-fuzz tools/xtos-sbom; do
        make -C "$ROOT/$component" clean >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT
make -C "$ROOT/sdk/libxtos-bci" clean all
make -C "$ROOT/sdk/libxtos-rt" clean test all
make -C "$ROOT/sdk/libxtos-signal" clean test all
python3 "$ROOT/tests/test_eeg_window.py"
python3 "$ROOT/tests/test_eeg_window_install.py"
python3 "$ROOT/tests/test_eeg_window_integration.py"
for suite in test_ai_model_binding.py test_eeg_stream.py test_eeg_stream_export.py \
    test_eeg_feature_binding.py test_eeg_transport_fixture.py test_eeg_cpu_transport.py \
    test_eeg_lifecycle.py test_eeg_simulation.py test_eeg_controller.py \
    test_eeg_stream_process.py test_eeg_loop_install.py; do
    python3 -B -m unittest discover -s "$ROOT/tests" -p "$suite"
done
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
for suite in test_ai_runtime.py test_ai_provider_experiment.py test_ai_provider_trust.py test_ai_provider_client.py test_ai_provider_worker.py test_ai_recovery_budget.py test_ai_provider_install.py test_ai_provider_service_real.py test_ai_provider_integration.py test_ai_service.py test_ai_client.py test_ai_worker.py test_ai_service_real.py test_ai_model_trust.py test_ai_model_revocation.py test_ai_signed_lifecycle.py test_ai_ticket_lifecycle.py test_ai_runtime_bundle.py test_ai_runtime_assembly.py; do
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
