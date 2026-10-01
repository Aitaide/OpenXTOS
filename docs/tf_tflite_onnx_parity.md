# TensorFlow/TFLite ONNX Export Parity

## Overview

This baseline validates that a fixed TensorFlow model converted to TFLite produces inference results that match ONNX Runtime CPU output within numerical tolerance. It extends the ONNX Runtime CPU baseline with TensorFlow/TFLite integration.

**Purpose**: Establish that the same fixed 3-class classifier can be represented in TFLite format and produce equivalent results to ONNX Runtime, enabling cross-framework model validation.

**Status**: Implementation complete, awaiting TensorFlow dependency availability for verification.

## Model

- **Architecture**: `Dense(4 -> 3) -> Softmax`
- **Weights**: Fixed identity mapping for first 3 inputs, zero for 4th input
- **Bias**: Zero
- **Input shape**: `[1, 4]`
- **Output shape**: `[1, 3]`
- **Fixed input**: `[2.0, 0.0, -1.0, 0.0]`
- **TFLite optimization**: None (no quantization)

## Comparison Chain

1. **TensorFlow eager execution** → get reference output
2. **Convert to TFLite** → export model
3. **TFLite inference** → get TFLite output
4. **ONNX Runtime inference** → get ONNX output (using `fixed_classifier.onnx`)
5. **Compare TFLite vs ONNX** → validate max/mean absolute error and predicted class

## Quality Gates

- **Max absolute error**: ≤ 1e-5
- **Predicted class**: TFLite and ONNX must agree
- **Execution provider**: CPUExecutionProvider only
- **Threading**: Single-threaded (intra_op=1, inter_op=1)

## Dependencies

**Required**:
- TensorFlow >= 2.x
- NumPy
- ONNX Runtime with CPUExecutionProvider
- Fixed ONNX classifier: `tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx`

**Availability**: Optional. When dependencies are missing, tools return `status: unverified` and `safe_action: no_action`.

## Usage

### Run Parity Benchmark

```bash
# With isolated TensorFlow and ONNX Runtime
BUILD_ROOT=.build \
XTOS_TF_ROOT=.build/tf-root \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
./scripts/benchmark_tf_onnx.sh
```

### Run Tests

```bash
# Parity tests (requires TensorFlow, ONNX Runtime, NumPy)
XTOS_TF_ROOT=.build/tf-root \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
PYTHONNOUSERSITE=1 \
PYTHONDONTWRITEBYTECODE=1 \
python3 -m unittest tools.xtos-onnx-baseline.test_tf_onnx_parity -v

# Test unavailable-dependency degradation
XTOS_TF_DISABLE=1 \
python3 -m unittest tools.xtos-onnx-baseline.test_tf_onnx_parity.TFOnnxParityTests.test_missing_tf_is_explicitly_unverified -v
```

### Probe Dependencies

```bash
XTOS_TF_ROOT=.build/tf-root \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
python3 tools/xtos-onnx-baseline/tf_export.py --probe-only
```

## Output Schema

### Success (`status: ok`)

```json
{
  "schema": "xtos-tf-onnx-parity-v1",
  "status": "ok",
  "safe_action": "action_allowed",
  "tensorflow_version": "2.21.0",
  "tf_output": [0.843, 0.119, 0.037],
  "tflite_output": [0.843, 0.119, 0.037],
  "onnx_output": [0.843, 0.119, 0.037],
  "max_abs_error": 0.0,
  "mean_abs_error": 0.0,
  "tf_predicted_class": 0,
  "tflite_predicted_class": 0,
  "onnx_predicted_class": 0,
  "predictions_match": true,
  "input_shape": [1, 4],
  "output_shape": [1, 3],
  "tflite_model_bytes": 1024,
  "execution_provider": "CPUExecutionProvider",
  "threads": {"intra_op": 1, "inter_op": 1},
  "cpu_model": "x86_64",
  "git_commit": "abc123..."
}
```

### Unavailable (`status: unverified`)

```json
{
  "schema": "xtos-tf-onnx-parity-v1",
  "status": "unverified",
  "safe_action": "no_action",
  "reason": "tf_or_runtime_unavailable"
}
```

Possible `reason` values:
- `tf_disabled`: `XTOS_TF_DISABLE=1` set
- `tf_or_runtime_unavailable`: TensorFlow, NumPy, or ONNX Runtime missing or CPUExecutionProvider unavailable
- `fixed_classifier_onnx_missing`: Reference ONNX model not found

### Error (`status: error`)

```json
{
  "schema": "xtos-tf-onnx-parity-v1",
  "status": "error",
  "safe_action": "no_action",
  "max_abs_error": 0.001,
  "predictions_match": false,
  "..."
}
```

## What This Does NOT Validate

- **TensorFlow training**: Only inference with fixed weights
- **Quantization**: TFLite model uses full precision (float32)
- **GPU execution**: CPU provider only
- **Real EEG models**: Fixed 3-class classifier only
- **Hard real-time**: Userspace baseline, no deadline guarantees
- **Multi-threaded inference**: Single thread only
- **Mobile deployment**: x86_64 development environment
- **Production optimization**: Research baseline

## Integration

- **Test entry point**: `scripts/test.sh` includes TF/TFLite parity when dependencies available
- **Benchmark**: `scripts/benchmark_tf_onnx.sh`
- **Status**: Tracked in `STATUS.md` under Stage 3D

## Related Documentation

- [ONNX Runtime CPU Baseline](onnx_runtime_cpu_baseline.md)
- [PyTorch ONNX Export Parity](torch_onnx_export_parity.md)
- [Signal Processing Baseline](signal_processing_baseline.md)
