"""Pure process-local EEG continuity evidence; never action authority."""
from dataclasses import asdict, dataclass
from threading import RLock

from .feature_binding import (BindingError, ModelBinding, PreparedInference,
                              U64_MAX, prepare_inference, validate_binding)


class LifecycleError(ValueError):
    """A reservation or lifecycle event was denied."""


@dataclass(frozen=True, slots=True, eq=False)
class InferenceLease:
    prepared: PreparedInference
    revision: int


@dataclass(frozen=True, slots=True)
class Completion:
    status: str
    safe_action: str = 'no_action'
    prepared: PreparedInference | None = None
    reason: str = ''


class StreamFence:
    def __init__(self, binding):
        if type(binding) is not ModelBinding or type(binding.input_shape) is not tuple:
            raise LifecycleError('expected validated immutable binding')
        raw = asdict(binding)
        raw['input_shape'] = list(binding.input_shape)
        try:
            self._binding = validate_binding(raw)
        except BindingError as exc:
            raise LifecycleError(str(exc)) from exc
        self._lock = RLock()
        self._revision = 1
        self._active = None
        self._session = 0
        self._barrier = 0
        self._identity = None
        self._max_window = 0
        self._sequence = None
        self._invalidated = False
        self._closed = False
        self._exhausted = False

    def _revoke(self):
        self._active = None
        self._identity = None
        self._sequence = None
        self._invalidated = True
        if self._revision == U64_MAX:
            self._exhausted = True
            raise LifecycleError('revision exhausted')
        self._revision += 1

    def _deny(self, reason):
        self._revoke()
        raise LifecycleError(reason)

    def reserve(self, record, now_ns, request_id, deadline_ms):
        with self._lock:
            if self._exhausted:
                raise LifecycleError('revision exhausted')
            if self._closed:
                raise LifecycleError('closed')
            try:
                prepared = prepare_inference(self._binding, record, now_ns,
                                             request_id, deadline_ms)
            except BindingError as exc:
                self._revoke()
                raise LifecycleError(str(exc)) from exc
            session, barrier = prepared.session_id, prepared.barrier_id
            if session < self._session or (session == self._session and barrier < self._barrier):
                raise LifecycleError('stale session/barrier')
            newer_session = session > self._session
            newer_barrier = newer_session or barrier > self._barrier
            identity = (prepared.stream_generation, prepared.config_generation)
            if self._active is not None and not newer_barrier:
                if identity != self._identity:
                    self._deny('generation changed without barrier')
                if prepared.window_id > self._max_window and self._sequence is not None:
                    first, last = self._sequence
                    if (prepared.first_sequence - first != self._binding.hop_frames or
                            prepared.last_sequence <= last):
                        self._deny('sequence hop mismatch')
                raise LifecycleError('busy')
            if not newer_session and prepared.window_id <= self._max_window:
                self._deny('window replay')
            if self._invalidated and not newer_barrier:
                raise LifecycleError('invalidated barrier')
            identity = (prepared.stream_generation, prepared.config_generation)
            if newer_barrier:
                self._revoke()
                if newer_session:
                    self._max_window = 0
                self._session, self._barrier = session, barrier
            elif self._identity != identity:
                self._deny('generation changed without barrier')
            elif self._sequence is not None:
                first, last = self._sequence
                if (prepared.first_sequence - first != self._binding.hop_frames or
                        prepared.last_sequence <= last):
                    self._deny('sequence hop mismatch')
            self._invalidated = False
            self._identity = identity
            self._sequence = (prepared.first_sequence, prepared.last_sequence)
            self._max_window = prepared.window_id
            lease = InferenceLease(prepared, self._revision)
            self._active = lease
            return lease

    def invalidate(self, session_id, barrier_id, reason):
        with self._lock:
            if (type(session_id) is not int or not 1 <= session_id <= U64_MAX or
                    type(barrier_id) is not int or not 1 <= barrier_id <= U64_MAX or
                    not self._valid_reason(reason)):
                self._deny('malformed event')
            if session_id < self._session or (session_id == self._session and barrier_id < self._barrier):
                raise LifecycleError('stale event')
            if session_id > self._session:
                self._max_window = 0
            if (session_id, barrier_id) != (self._session, self._barrier) or not self._invalidated:
                self._revoke()
            self._session, self._barrier = session_id, barrier_id

    def close(self, reason):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._revoke()
            if not self._valid_reason(reason):
                raise LifecycleError('malformed close reason')

    @staticmethod
    def _valid_reason(reason):
        return (type(reason) is str and 1 <= len(reason) <= 128 and
                all(' ' <= character <= '~' for character in reason))

    def complete(self, lease, now_ns):
        with self._lock:
            if lease is not self._active or lease is None:
                return Completion('rejected', reason='inactive lease')
            self._active = None
            prepared = lease.prepared
            if (lease.revision != self._revision or self._invalidated or
                    (prepared.session_id, prepared.barrier_id) != (self._session, self._barrier) or
                    (prepared.stream_generation, prepared.config_generation) != self._identity):
                return Completion('rejected', reason='obsolete continuity')
            if (type(now_ns) is not int or not 0 <= now_ns <= U64_MAX or
                    now_ns < prepared.prepared_at_ns):
                return Completion('rejected', reason='invalid monotonic time')
            if now_ns - prepared.first_host_rx_ns > prepared.max_receive_age_ns:
                return Completion('rejected', reason='oldest receive expired')
            if now_ns - prepared.prepared_at_ns >= prepared.deadline_ms * 1_000_000:
                return Completion('rejected', reason='deadline expired')
            return Completion('window_current', prepared=prepared)
