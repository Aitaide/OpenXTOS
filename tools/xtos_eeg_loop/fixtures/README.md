# Mean/RMS transport fixture

`mean_rms_transport.onnx` is a deterministic arithmetic fixture with SHA-256 `1437840f5685a55e43bbd4be10153e3a660f0c7f84b92db12fe9a105422ccc62`. It is not trained and has no EEG classification accuracy claim. Its purpose is to exercise every position in the new 16-value tensor contract without reusing the legacy four-input classifier.

Input is float32 `features_uv` with shape `[1,16]`: eight means followed by eight RMS values, in microvolts. The graph computes MatMul + bias + Softmax to `probabilities` shape `[1,3]`, opset 17, IR 9. For input position i starting at zero, the three weights are `[((i+1)%5-2)/256, ((i+2)%7-3)/256, ((i+3)%11-5)/256]`; biases are `[0.125,-0.125,0]`. All coefficients are exact binary fractions. No calibration, filtering or preprocessing occurs inside the model.

Reconstruct using the locked development environment:

    scripts/ai-python-qemu.sh -B tools/xtos_eeg_loop/make_transport_fixture.py --output /absolute/new/path/model.onnx

The output path must not exist; the generator preserves an existing file. ONNX and NumPy are development-only generation dependencies. The shipped binary model needs only the existing ONNX Runtime execution environment, and normal signed-model admission still applies. Local model-feature binding remains an operator declaration; signing these bytes does not authenticate a physical device or establish domain semantics.

Run maintained verification with the real selected runtime:

    XTOS_TEST_AI_PYTHON="$PWD/scripts/ai-python-qemu.sh" python3 -B tests/test_eeg_transport_fixture.py

The suite checks exact binary identity, deterministic reconstruction, create-only preservation, real native Ed25519 signed admission, real killable CPU worker execution against an independent arithmetic oracle and worker reaping. Trust policy persistence and time are explicit test fixtures. This is host functional evidence under QEMU CPU emulation; it is not latency, AF_UNIX integration, Live guest, hardware or action acceptance. Any synthetic/fixture loop consuming this model must keep its top-level result `no_action`.
