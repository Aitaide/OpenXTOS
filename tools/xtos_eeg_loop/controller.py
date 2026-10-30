"""Process-local stream owner. Every result remains harmless software evidence."""
from dataclasses import asdict, dataclass
from threading import RLock
from time import monotonic_ns

from .feature_binding import ModelBinding, validate_binding
from .inference import infer_window
from .lifecycle import StreamFence, LifecycleError
from .simulation import SimulationFeedback, SimulationError, _transport


@dataclass(frozen=True, slots=True)
class ControllerResult:
    status: str
    reason: str = ''
    safe_action: str = 'no_action'
    candidate: object = None
    output: tuple | None = None


class StreamController:
    def __init__(self, binding, socket_path, simulation, *, now_ns=monotonic_ns):
        if type(binding) is ModelBinding:
            binding = asdict(binding)
            binding['input_shape'] = list(binding['input_shape'])
        elif type(binding) is dict:
            binding = binding.copy()
            shape = binding.get('input_shape')
            if type(shape) is list:
                binding['input_shape'] = list(tuple(shape))
        self._binding = validate_binding(binding)
        if type(socket_path) is not str or not socket_path.startswith('/') or '\x00' in socket_path or len(socket_path.encode()) > 107:
            raise ValueError('expected fixed absolute AF_UNIX path')
        if type(simulation) is not SimulationFeedback or not callable(now_ns):
            raise ValueError('expected trusted simulator and clock')
        self._socket_path = socket_path
        self._simulation = simulation
        self._now_ns = now_ns
        self._lock = RLock()
        self._fence = StreamFence(self._binding)
        self._closed = False
        self._active = None

    def _invalidate(self, reason):
        self._active = None
        try:
            # Malformed lifecycle event deliberately revokes without inventing
            # a second controller continuity tracker.
            self._fence.invalidate(None, None, reason)
        except LifecycleError:
            pass
        self._simulation.invalidate(reason)

    @staticmethod
    def _window_copy(record):
        if type(record) is not dict:
            raise ValueError('expected native object')
        owned = record.copy()
        values = owned.get('values')
        if type(values) is not list:
            raise ValueError('expected values array')
        snapshot = tuple(values)
        if len(snapshot) != 16 or any(type(v) not in (int, float) for v in snapshot):
            raise ValueError('expected 16 scalar values')
        owned['values'] = list(snapshot)
        return owned

    def infer(self, record, request_id, deadline_ms, *, candidate_ttl_ms=1000):
        with self._lock:
            if self._closed:
                return ControllerResult('rejected', 'closed')
            try:
                owned = self._window_copy(record)
                if type(candidate_ttl_ms) is not int or not 1 <= candidate_ttl_ms <= 300000:
                    raise ValueError('invalid candidate ttl')
                lease = self._fence.reserve(owned, self._now_ns(), request_id, deadline_ms)
            except LifecycleError as exc:
                if str(exc) not in ('busy', 'stale session/barrier', 'closed'):
                    self._invalidate('invalid input')
                return ControllerResult('rejected', str(exc))
            except (ValueError, TypeError, OSError, OverflowError) as exc:
                self._invalidate('invalid input')
                return ControllerResult('rejected', 'invalid input')
            self._active = lease
            self._simulation.invalidate('new reservation')
        first = True
        def clock():
            nonlocal first
            if first:
                first = False
                return lease.prepared.prepared_at_ns
            return self._now_ns()
        try:
            result = infer_window(self._binding, owned, self._socket_path,
                                  request_id, deadline_ms, now_ns=clock)
            with self._lock:
                if self._active is not lease:
                    return ControllerResult('rejected', 'inactive lease')
                completion = self._fence.complete(lease, self._now_ns())
                if completion.status != 'window_current':
                    self._invalidate('inference expired')
                    return ControllerResult('rejected', completion.reason)
                if result.get('status') != 'transport_verified' or result.get('safe_action') != 'no_action':
                    self._invalidate('inference refused')
                    return ControllerResult('rejected', result.get('reason', 'inference refused'))
                # Shared strict transport validation also checks canonical digest,
                # times and identities for synthetic transport-only results.
                transport = _transport(result, lease.prepared, self._now_ns())
                candidate = None
                if lease.prepared.source_kind == 'serdev':
                    candidate = self._simulation.offer(lease.prepared, result, ttl_ms=candidate_ttl_ms)
                self._active = None
                return ControllerResult('candidate' if candidate else 'transport_verified',
                                        'unverified' if candidate else 'transport_only',
                                        candidate=candidate, output=transport.output)
        except BaseException as exc:
            with self._lock:
                # A completed old lease cannot revoke a newer reservation.
                if self._active is lease:
                    self._invalidate('inference failure')
            if not isinstance(exc, Exception):
                raise
            return ControllerResult('rejected', 'inference failure')

    def approval_request(self, candidate):
        return self._simulation.approval_request(candidate)

    def confirm(self, candidate, nonce):
        return self._simulation.confirm(candidate, nonce)

    def handle(self, record):
        with self._lock:
            if self._closed:
                return ControllerResult('rejected', 'closed')
            try:
                if type(record) is not dict:
                    raise ValueError('expected native object')
                record = record.copy()
                schema = record.get('schema')
                if schema == 'xtos.eeg.window.v1':
                    from .feature_binding import prepare_inference
                    prepare_inference(self._binding, self._window_copy(record),
                                      self._now_ns(), 'handle-validation', 1)
                    return ControllerResult('use_infer', 'window not reserved')
                fields = {'schema', 'session_id', 'barrier_id', 'reason',
                          'safe_action', 'physical_acceptance'}
                summary = schema == 'xtos.eeg.stream-summary.v1'
                if summary:
                    fields |= {'status', 'windows_emitted'}
                elif schema != 'xtos.eeg.barrier.v1':
                    raise ValueError('unknown native schema')
                if (record.keys() != fields or type(schema) is not str or
                        type(record['safe_action']) is not str or record['safe_action'] != 'no_action' or
                        record['physical_acceptance'] is not False or
                        not StreamFence._valid_reason(record['reason'])):
                    raise ValueError('malformed native event')
                for name in ('session_id', 'barrier_id'):
                    if type(record[name]) is not int or not 1 <= record[name] < 1 << 64:
                        raise ValueError('malformed event identity')
                if summary:
                    if (type(record['status']) is not str or record['status'] not in
                            ('complete', 'degraded', 'interrupted', 'error') or
                            type(record['windows_emitted']) is not int or
                            not 0 <= record['windows_emitted'] <= 100000):
                        raise ValueError('malformed summary')
                    return self.close(record['reason'])
                self._fence.invalidate(record['session_id'], record['barrier_id'], record['reason'])
                self._active = None
                self._simulation.invalidate(record['reason'])
                return ControllerResult('invalidated', record['reason'])
            except LifecycleError as exc:
                if str(exc) != 'stale event':
                    self._invalidate('malformed event')
                return ControllerResult('rejected', str(exc))
            except (ValueError, TypeError, OSError, OverflowError):
                self._invalidate('malformed event')
                return ControllerResult('rejected', 'malformed event')

    def eof(self):
        return self.close('eof')

    def close(self, reason):
        with self._lock:
            self._closed = True
            self._active = None
            try:
                self._fence.close(reason)
            finally:
                self._simulation.close(reason)
            return ControllerResult('closed', reason)
