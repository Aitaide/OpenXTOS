"""Harmless software simulation. SERDEV labels are unverified metadata only.

The caller owns validated model-binding semantics and native barrier ordering.
No runner, physical action, transport client or production store initialization.
"""
from dataclasses import asdict, dataclass, replace
import hashlib
import math
import re
import struct
import secrets
import threading
import time

from .feature_binding import PreparedInference
from xtos_action_gate.schema import canonical


class SimulationError(ValueError):
    """Candidate refused or revoked."""


@dataclass(frozen=True, slots=True)
class _Transport:
    status: str
    safe_action: str
    reason: str
    request_id: str
    model: str
    model_sha256: str
    source_kind: str
    session_id: int
    stream_generation: int
    config_generation: int
    window_id: int
    barrier_id: int
    feature_digest: str
    completed_at_ns: int
    output: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class Candidate:
    schema: str
    candidate_id: str
    prepared: PreparedInference
    transport: _Transport
    provenance: str
    created_at_ns: int
    expires_at_ns: int


@dataclass(frozen=True, slots=True)
class Receipt:
    status: str
    safe_action: str
    candidate_id: str
    request_id: str
    simulated_at_ns: int


_U64 = (1 << 64) - 1
_IDENTITIES = ('session_id', 'stream_generation', 'config_generation', 'window_id', 'barrier_id')


def _integer(value, low=0, high=_U64):
    if type(value) is not int or not low <= value <= high:
        raise SimulationError('integer outside bounds')


def _prepared(value):
    if type(value) is not PreparedInference:
        raise SimulationError('expected exact PreparedInference')
    for name in ('request_id', 'model'):
        item = getattr(value, name)
        if type(item) is not str or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', item, re.ASCII) is None:
            raise SimulationError('invalid identifier')
    if (type(value.expected_model_sha256) is not str or
            re.fullmatch(r'[0-9a-f]{64}', value.expected_model_sha256) is None):
        raise SimulationError('invalid model digest')
    if type(value.source_kind) is not str or value.source_kind not in ('synthetic', 'serdev'):
        raise SimulationError('invalid source kind')
    for name in _IDENTITIES:
        _integer(getattr(value, name), 1)
    for name in ('first_sequence', 'last_sequence', 'first_host_rx_ns', 'last_host_rx_ns', 'prepared_at_ns'):
        _integer(getattr(value, name))
    _integer(value.max_receive_age_ns, 1)
    _integer(value.deadline_ms, 1, 300000)
    # PreparedInference omits full window dimensions/mask: those remain the
    # validated binding caller's contract, never reconstruct a native record.
    if not 2 <= value.last_sequence - value.first_sequence + 1 <= 4096:
        raise SimulationError('invalid sequence span')
    if not value.first_host_rx_ns <= value.last_host_rx_ns <= value.prepared_at_ns:
        raise SimulationError('invalid prepared time order')
    if value.prepared_at_ns - value.first_host_rx_ns > value.max_receive_age_ns:
        raise SimulationError('stale prepared values')
    if type(value.values) is not tuple or len(value.values) != 16:
        raise SimulationError('expected immutable 16 float32 values')
    for item in value.values:
        if type(item) is not float or not math.isfinite(item):
            raise SimulationError('invalid feature value')
        try:
            normalized = struct.unpack('!f', struct.pack('!f', item))[0]
        except (OverflowError, struct.error) as exc:
            raise SimulationError('feature outside float32 range') from exc
        if normalized != item:
            raise SimulationError('feature is not normalized float32')
    for mean, rms in zip(value.values[:8], value.values[8:]):
        if rms < 0 or rms + 1e-6 * max(abs(mean), rms) < abs(mean):
            raise SimulationError('inconsistent mean/RMS')
    return replace(value, values=tuple(item for item in value.values))


def _transport(raw: dict, prepared, now):
    if type(raw) is not dict or raw.keys() != _Transport.__dataclass_fields__.keys():
        raise SimulationError('expected complete transport object')
    raw = raw.copy()
    expected = dict(status='transport_verified', safe_action='no_action', reason='transport_only',
                    request_id=prepared.request_id, model=prepared.model,
                    model_sha256=prepared.expected_model_sha256, source_kind=prepared.source_kind,
                    feature_digest=hashlib.sha256(canonical(asdict(prepared))).hexdigest(),
                    **{name: getattr(prepared, name) for name in _IDENTITIES})
    for name, item in expected.items():
        if type(raw[name]) is not type(item) or raw[name] != item:
            raise SimulationError('transport identity or status mismatch')
    _integer(raw['completed_at_ns'])
    if not prepared.prepared_at_ns <= raw['completed_at_ns'] <= now:
        raise SimulationError('transport time order')
    output = raw['output']
    if type(output) is not list:
        raise SimulationError('expected one row of three probabilities')
    rows = tuple(output)
    if len(rows) != 1 or type(rows[0]) is not list:
        raise SimulationError('expected one row of three probabilities')
    probabilities = tuple(rows[0])
    if len(probabilities) != 3:
        raise SimulationError('expected one row of three probabilities')
    if any(type(item) not in (int, float) or not 0 <= item <= 1 or not math.isfinite(item) for item in probabilities):
        raise SimulationError('invalid probability')
    if abs(math.fsum(probabilities) - 1.0) > 1e-5:
        raise SimulationError('probability sum')
    return _Transport(**(raw | {'output': (probabilities,)}))


class SimulationFeedback:
    def __init__(self, gate, *, now_ns=time.monotonic_ns):
        self._gate = gate
        self._now_ns = now_ns
        self._lock = threading.RLock()
        self._pending = None
        self._closed = False

    def offer(self, prepared, transport, *, ttl_ms):
        with self._lock:
            try:
                if self._closed:
                    raise SimulationError('simulation closed')
                now = self._now_ns()
                _integer(now)
                _integer(ttl_ms, 1, 300000)
                owned = _prepared(prepared)
                result = _transport(transport, owned, now)
                if owned.source_kind != 'serdev':
                    raise SimulationError('synthetic cannot become candidate')
                deadlines = (now + ttl_ms * 1_000_000,
                             owned.prepared_at_ns + owned.deadline_ms * 1_000_000,
                             owned.first_host_rx_ns + owned.max_receive_age_ns + 1)
                for deadline in deadlines:
                    _integer(deadline)
                expires = min(deadlines)
                if now >= expires:
                    raise SimulationError('expired transport')
                candidate = Candidate('xtos.eeg.simulation-candidate.v1', secrets.token_hex(32),
                                      owned, result, 'unverified', now, expires)
            except BaseException:
                self._pending = None
                raise
            self._pending = candidate
            return candidate

    def _fresh(self, candidate):
        now = self._now_ns()
        _integer(now)
        prepared = candidate.prepared
        if (not candidate.created_at_ns <= now < candidate.expires_at_ns or
                now - prepared.prepared_at_ns >= prepared.deadline_ms * 1_000_000 or
                now - prepared.first_host_rx_ns > prepared.max_receive_age_ns):
            raise SimulationError('candidate expired or clock regressed')
        return now

    def approval_request(self, candidate):
        with self._lock:
            if candidate is not self._pending or candidate is None:
                raise SimulationError('not exact pending candidate')
            try:
                self._fresh(candidate)
            except BaseException:
                self._pending = None
                raise
            return {'schema': 'xtos.action.v1',
                    'argv': ['/usr/libexec/xtos/eeg-feedback-sim', canonical(asdict(candidate)).decode('ascii')],
                    'root': '/', 'cwd': '/', 'uid': 65534, 'gid': 65534,
                    'timeout_ms': 1000, 'writable_dirs': [],
                    'limits': {'cpu_sec': 1, 'as_bytes': 67108864, 'nofile': 16, 'nproc': 1},
                    'sandbox_policy': {'version': 'xtos.runner.v1', 'clear_groups': True,
                                       'deny_socket': True, 'deny_getpid': False}}

    def confirm(self, candidate, nonce):
        request = self.approval_request(candidate)

        def internal_callback(_frozen):
            with self._lock:
                if self._closed or candidate is not self._pending:
                    raise SimulationError('candidate revoked before callback')
                # Burn the owned candidate before clock checks/result creation.
                self._pending = None
                now = self._fresh(candidate)
                return Receipt('simulated', 'no_action', candidate.candidate_id,
                               candidate.prepared.request_id, now)

        # Never hold owner lock while storage/another confirmer waits. The gate
        # consumes durably before invoking the fixed, memory-only closure.
        return self._gate.execute(nonce, request, internal_callback)

    def invalidate(self, reason):
        with self._lock:
            self._pending = None
            self._reason(reason)

    def close(self, reason):
        with self._lock:
            self._pending = None
            self._closed = True
            self._reason(reason)

    @staticmethod
    def _reason(reason):
        if (type(reason) is not str or not 1 <= len(reason) <= 128 or
                any(ord(char) < 32 or ord(char) > 126 for char in reason)):
            raise SimulationError('invalid revocation reason')
