"""Pure EEG feature binding; operator semantics are not model/task authentication.

Callers must reject duplicate JSON keys before decoding, enforce configuration
provenance, and handle replay, cancellation and post-inference freshness separately.
This module performs no I/O and grants no action authority.
"""
from dataclasses import asdict, dataclass
import math
import re
import struct

U64_MAX = (1 << 64) - 1
IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,128}', re.ASCII)
BINDING_FIELDS = frozenset('''
    schema model model_sha256 feature_mapping unit order channels sample_rate_hz
    enabled_mask window_frames hop_frames max_receive_age_ns input_name input_dtype
    input_shape
'''.split())
WINDOW_FIELDS = frozenset('''
    schema feature_mapping unit order channels sample_rate_hz enabled_mask frame_count
    window_frames hop_frames max_receive_age_ns emitted_at_ns source_flags source_kind
    barrier_id session_id stream_generation config_generation window_id first_sequence
    last_sequence first_host_rx_ns last_host_rx_ns values safe_action physical_acceptance
'''.split())


def _object(raw, fields):
    if type(raw) is not dict or raw.keys() != fields:
        raise BindingError('expected exact object fields')
    return raw.copy()


def _integer(value, name, low=0, high=U64_MAX):
    if type(value) is not int or not low <= value <= high:
        raise BindingError(f'{name}: integer outside bounds')


def _identifier(value, name):
    if type(value) is not str or IDENTIFIER.fullmatch(value) is None:
        raise BindingError(f'{name}: expected bounded ASCII identifier')



class BindingError(ValueError):
    """The binding, feature record or request metadata violates the contract."""


@dataclass(frozen=True, slots=True)
class ModelBinding:
    schema: str
    model: str
    model_sha256: str
    feature_mapping: str
    unit: str
    order: str
    channels: int
    sample_rate_hz: int
    enabled_mask: int
    window_frames: int
    hop_frames: int
    max_receive_age_ns: int
    input_name: str
    input_dtype: str
    input_shape: tuple[int, int]


@dataclass(frozen=True, slots=True)
class PreparedInference:
    request_id: str
    model: str
    expected_model_sha256: str
    values: tuple[float, ...]
    deadline_ms: int
    source_kind: str
    session_id: int
    stream_generation: int
    config_generation: int
    window_id: int
    barrier_id: int
    first_sequence: int
    last_sequence: int
    first_host_rx_ns: int
    last_host_rx_ns: int
    max_receive_age_ns: int
    prepared_at_ns: int

    def request_document(self):
        """Return a fresh infer document, without an action approval."""
        return {'id': self.request_id, 'op': 'infer', 'model': self.model,
                'expected_model_sha256': self.expected_model_sha256,
                'input': [list(self.values)], 'deadline_ms': self.deadline_ms}


def validate_binding(raw):
    """Validate exact decoded operator configuration and own an immutable copy."""
    raw = _object(raw, BINDING_FIELDS)
    fixed = {'schema': 'xtos.eeg.model-binding.v1',
             'feature_mapping': 'xtos.eeg.mean-rms.v1', 'unit': 'microvolt',
             'order': 'mean_then_rms', 'input_dtype': 'float32'}
    for name, expected in fixed.items():
        if type(raw[name]) is not str or raw[name] != expected:
            raise BindingError(f'{name}: mismatch')
    _identifier(raw['model'], 'model')
    name = raw['input_name']
    if type(name) is not str or re.fullmatch(r'[!-~]{1,128}', name) is None:
        raise BindingError('input_name: expected bounded printable ASCII tensor name')
    digest = raw['model_sha256']
    if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise BindingError('model_sha256: expected lowercase SHA256')
    for name, low, high in (('channels', 8, 8), ('sample_rate_hz', 250, 250),
                            ('enabled_mask', 1, 255), ('window_frames', 2, 4096),
                            ('max_receive_age_ns', 1, U64_MAX)):
        _integer(raw[name], name, low, high)
    _integer(raw['hop_frames'], 'hop_frames', 1, raw['window_frames'])
    shape = raw['input_shape']
    if (type(shape) is not list or len(shape) != 2 or
            any(type(value) is not int for value in shape) or shape != [1, 16]):
        raise BindingError('input_shape: expected [1,16]')
    raw['input_shape'] = tuple(shape)
    return ModelBinding(**raw)


def prepare_inference(binding, record, now_ns, request_id, deadline_ms):
    """Validate one native window at caller CLOCK_MONOTONIC time; prepare only."""
    if type(binding) is not ModelBinding or type(binding.input_shape) is not tuple:
        raise BindingError('binding: expected validated immutable ModelBinding')
    raw_binding = asdict(binding)
    raw_binding['input_shape'] = list(binding.input_shape)
    binding = validate_binding(raw_binding)
    _integer(now_ns, 'now_ns')
    _identifier(request_id, 'request_id')
    _integer(deadline_ms, 'deadline_ms', 1, 300000)
    record = _object(record, WINDOW_FIELDS)
    identities = ('barrier_id', 'session_id', 'stream_generation', 'config_generation', 'window_id')
    numeric = ('channels', 'sample_rate_hz', 'enabled_mask', 'frame_count', 'window_frames',
               'hop_frames', 'max_receive_age_ns', 'emitted_at_ns', 'source_flags',
               'first_sequence', 'last_sequence', 'first_host_rx_ns', 'last_host_rx_ns')
    for name in identities:
        _integer(record[name], name, 1)
    for name in numeric:
        _integer(record[name], name)
    for name in ('feature_mapping', 'unit', 'order', 'channels', 'sample_rate_hz',
                 'enabled_mask', 'window_frames', 'hop_frames', 'max_receive_age_ns'):
        expected = getattr(binding, name)
        if type(record[name]) is not type(expected) or record[name] != expected:
            raise BindingError(f'{name}: binding mismatch')
    if (type(record['schema']) is not str or record['schema'] != 'xtos.eeg.window.v1' or
            type(record['safe_action']) is not str or record['safe_action'] != 'no_action' or
            record['physical_acceptance'] is not False):
        raise BindingError('window schema/action metadata mismatch')
    if (record['source_flags'] not in (1, 2) or type(record['source_kind']) is not str or
            record['source_kind'] != {1: 'serdev', 2: 'synthetic'}[record['source_flags']]):
        raise BindingError('source metadata mismatch')
    if (record['frame_count'] != binding.window_frames or
            record['last_sequence'] - record['first_sequence'] + 1 != record['frame_count']):
        raise BindingError('frame count/sequence span mismatch')
    first, last, emitted = (record[name] for name in
                            ('first_host_rx_ns', 'last_host_rx_ns', 'emitted_at_ns'))
    if not first <= last <= emitted <= now_ns:
        raise BindingError('receive/emission/current time order mismatch')
    if (now_ns - first > binding.max_receive_age_ns or
            emitted - first > binding.max_receive_age_ns):
        raise BindingError('oldest frame receive age exceeded')
    raw_values = record['values']
    if type(raw_values) is not list or len(raw_values) != 16:
        raise BindingError('values: expected 16-number JSON array')
    values = []
    for index, value in enumerate(raw_values):
        if type(value) not in (int, float):
            raise BindingError('values: expected finite float32 numbers')
        # Check disabled zeros before conversion: underflow must not hide a value.
        if not binding.enabled_mask & (1 << (index % 8)) and value != 0:
            raise BindingError('disabled channel must be exactly zero')
        if index >= 8 and value < 0:
            raise BindingError('RMS must be nonnegative before float32 conversion')
        try:
            normalized = struct.unpack('!f', struct.pack('!f', value))[0]
        except (OverflowError, struct.error) as exc:
            raise BindingError('value outside float32 range') from exc
        if not math.isfinite(normalized):
            raise BindingError('value must be finite float32')
        values.append(normalized)
    # Float32 rounding allowance, relative only (no fixed absolute floor).
    for mean, rms in (*zip(raw_values[:8], raw_values[8:]), *zip(values[:8], values[8:])):
        if rms < 0 or rms + 1e-6 * max(abs(mean), rms) < abs(mean):
            raise BindingError('RMS must be nonnegative and at least absolute mean')
    names = ('source_kind', 'session_id', 'stream_generation', 'config_generation',
             'window_id', 'barrier_id', 'first_sequence', 'last_sequence',
             'first_host_rx_ns', 'last_host_rx_ns', 'max_receive_age_ns')
    return PreparedInference(request_id, binding.model, binding.model_sha256,
                             tuple(values), deadline_ms,
                             **{name: record[name] for name in names}, prepared_at_ns=now_ns)
