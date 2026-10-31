"""Unsigned ONNX provider experiment; isolated from the trusted CPU runtime."""
from __future__ import annotations

import importlib
import hashlib
from numbers import Real
from pathlib import Path
import sys

_RUNTIME = Path(__file__).resolve().parents[1] / 'xtos-ai-runtime'
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))
from xtos_ai_runtime.protocol import validate_input, validate_manifest, validate_output


class ProviderExperimentError(RuntimeError):
    """Fail-closed experimental error; never an action authorization."""
    safe_action = 'no_action'


class ProviderUnavailableError(ProviderExperimentError):
    """The explicitly requested provider is absent from this runtime."""


class RuntimeUnavailableError(ProviderExperimentError):
    """An optional runtime dependency cannot be imported."""


class ProviderSelectionError(ProviderExperimentError):
    """An explicit allowed provider is required."""


_ALLOWED = ('CPUExecutionProvider', 'CUDAExecutionProvider', 'ROCMExecutionProvider')


def _selected(session, requested):
    selected = session.get_providers()
    if type(selected) is not list or selected != [requested]:
        raise ProviderUnavailableError('requested provider was not selected exclusively')


def run_experiment(raw, model_root, value, *, runtime=None):
    """Run one explicitly selected provider, preserving a no-action result.

    This path validates model bytes and tensor metadata but deliberately bypasses
    production admission. It cannot authorize an action and is not a GPU claim.
    """
    if (type(raw) is not dict or type(raw.get('provider')) is not str
            or raw['provider'] not in _ALLOWED):
        raise ProviderSelectionError('an explicit allowed provider is required')
    requested = raw['provider']
    copied = dict(raw, provider='CPUExecutionProvider')
    manifest = validate_manifest(copied, model_root)
    if runtime is None:
        try:
            runtime = importlib.import_module('onnxruntime')
        except (ImportError, OSError) as exc:
            raise RuntimeUnavailableError('optional onnxruntime unavailable') from exc
    registered = list(runtime.get_available_providers())
    if requested not in registered:
        raise ProviderUnavailableError(f'provider unavailable: {requested}')
    try:
        np = importlib.import_module('numpy')
    except (ImportError, OSError) as exc:
        raise RuntimeUnavailableError('optional numpy unavailable') from exc
    checked = validate_input(manifest, value)
    options = runtime.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = runtime.ExecutionMode.ORT_SEQUENTIAL
    try:
        model = manifest.model_path.read_bytes()
        if hashlib.sha256(model).hexdigest() != manifest.sha256:
            raise ProviderExperimentError('model bytes changed after manifest validation')
        session = runtime.InferenceSession(model, sess_options=options, providers=[requested])
        disable_fallback = getattr(session, 'disable_fallback', None)
        if disable_fallback is None:
            raise ProviderExperimentError('runtime cannot disable provider fallback')
        disable_fallback()
        _selected(session, requested)
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ProviderExperimentError('model must expose one input and one output')
        for meta, name, shape in ((inputs[0], manifest.input_name, manifest.input_shape),
                                  (outputs[0], manifest.output_name, manifest.output_shape)):
            if meta.name != name or meta.type != 'tensor(float)' or tuple(meta.shape) != shape:
                raise ProviderExperimentError('model tensor metadata mismatch')
        zero = [[0.0] * manifest.input_shape[1] for _ in range(manifest.input_shape[0])]
        smoke = session.run([manifest.output_name], {
            manifest.input_name: np.asarray(zero, dtype=np.float32)})
        _selected(session, requested)
        if type(smoke) is not list or len(smoke) != 1:
            raise ProviderExperimentError('invalid zero-input provider smoke output')
        validate_output(manifest, smoke[0].tolist())
        result = session.run([manifest.output_name], {
            manifest.input_name: np.asarray(checked, dtype=np.float32)})
        _selected(session, requested)
        if type(result) is not list or len(result) != 1:
            raise ProviderExperimentError('invalid provider inference output')
        output = validate_output(manifest, result[0].tolist())
        if any(type(number) not in (int, float) or not isinstance(number, Real)
               for row in output for number in row):
            raise ProviderExperimentError('invalid numeric output')
    except ProviderExperimentError:
        raise
    except Exception as exc:
        raise ProviderExperimentError('provider experiment failed closed: ' + type(exc).__name__) from exc
    return {
        'status': 'success', 'safe_action': 'no_action', 'reason': 'experimental_only',
        'provider': requested, 'selected_provider': requested,
        'registered_providers': registered, 'output': output,
    }
