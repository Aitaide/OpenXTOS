"""Bounded application-only multimodal simulation. No device/actuator I/O."""
from dataclasses import asdict, dataclass
import hashlib
from xtos_action_gate import ActionGate
from xtos_action_gate.schema import canonical, snapshot
import math
import re
import struct
import threading

I64 = 2**63 - 1
U64 = 2**64 - 1
FIELDS = set('schema modality source_id source_kind clock_domain source_timestamp_ns host_observed_ns sequence generation quality values'.split())

def integer(value, low=1, high=I64):
    if type(value) is not int or not low <= value <= high: raise XRError('invalid integer')

def identifier(value):
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is None:
        raise XRError('invalid identifier')

def owned_fields(data):
    # Copy each mutable container before validating its retained shape and values.
    if type(data) is not dict: raise XRError('sample must be a dict')
    d = data.copy()
    if set(d) != FIELDS: raise XRError('unexpected sample fields')
    q, values = d['quality'], d['values']
    if type(q) is not dict or type(values) not in (list, tuple): raise XRError('invalid containers')
    q, values = q.copy(), tuple(values)
    if set(q) != {'valid', 'tracked', 'predicted'} or any(type(v) is not bool for v in q.values()):
        raise XRError('invalid quality')
    if type(d['schema']) is not str or d['schema'] != 'xtos.multimodal.sample.v1': raise XRError('unknown schema')
    if type(d['modality']) is not str or d['modality'] not in ('eeg', 'eye_gaze', 'emg'):
        raise XRError('invalid modality')
    identifier(d['source_id'])
    if type(d['source_kind']) is not str or d['source_kind'] not in ('live_device', 'fixture_replay', 'simulated_runtime'):
        raise XRError('invalid source kind')
    if type(d['clock_domain']) is not str or d['clock_domain'] not in ('host_monotonic_ns', 'openxr_time', 'emg_device_time'):
        raise XRError('invalid clock domain')
    if d['modality']=='eye_gaze' and d['clock_domain']!='openxr_time':
        raise XRError('eye gaze requires runtime sample time')
    for field in ('source_timestamp_ns', 'host_observed_ns'): integer(d[field])
    for field in ('sequence', 'generation'): integer(d[field], high=U64)
    if not 1 <= len(values) <= 32: raise XRError('invalid values length')
    floats = []
    for value in values:
        if type(value) not in (int, float): raise XRError('invalid scalar')
        try:
            f = float(value)
            f32 = struct.unpack('!f', struct.pack('!f', f))[0]
        except (OverflowError, ValueError, struct.error) as exc: raise XRError('float32 overflow') from exc
        if not math.isfinite(f32) or (f != 0 and f32 == 0): raise XRError('non-finite or underflow')
        floats.append(f32)
    d['quality'], d['values'] = Quality(**q), tuple(floats)
    return d

class XRError(ValueError):
    """Refusal; no action was performed."""

@dataclass(frozen=True, slots=True)
class Quality:
    valid: bool
    tracked: bool
    predicted: bool

@dataclass(frozen=True, slots=True)
class Sample:
    schema: str
    modality: str
    source_id: str
    source_kind: str
    clock_domain: str
    source_timestamp_ns: int
    host_observed_ns: int
    sequence: int
    generation: int
    quality: Quality
    values: tuple[float, ...]
    converted_host_ns: int
    uncertainty_ns: int
    conversion_id: str
    barrier: int

@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    samples: tuple[Sample, Sample, Sample]
    barrier: int
    created_ns: int
    expires_ns: int

@dataclass(frozen=True, slots=True)
class Receipt:
    candidate_id: str
    barrier: int
    confirmed_ns: int
    runtime_mode: str = 'simulated'
    safe_action: str = 'no_action'
    physical_acceptance: bool = False

class MultimodalSession:
    """One owner lock, three latest snapshots, and at most one pending candidate.

    clock: trusted monotonic nanoseconds. converter(domain, timestamp_ns): exact
    dict of converted_host_ns/uncertainty_ns/conversion_id for EACH point.
    gate: trusted existing ActionGate; no executor injection is exposed.
    """
    def __init__(self, *, clock, converter=None, gate=None, max_age_ns=100000000,
                 max_skew_ns=10000000, max_uncertainty_ns=1000000,
                 candidate_ttl_ns=50000000):
        for policy in (max_age_ns, max_skew_ns, max_uncertainty_ns, candidate_ttl_ns):
            integer(policy, high=3600000000000)
        if not callable(clock): raise XRError('trusted clock required')
        self._clock, self._converter, self._gate = clock, converter, gate
        self._age, self._skew = max_age_ns, max_skew_ns
        self._uncertainty, self._ttl = max_uncertainty_ns, candidate_ttl_ns
        self._lock = threading.RLock()
        self._latest = {}
        self._history = {}  # bounded: one continuity record per modality
        self._pending = None
        self._barrier = 0
        self._closed = False
        self._last_now = 0
    def _candidate(self, candidate):
        # Identity is deliberate: a reconstructed equal dataclass is not authority.
        if type(candidate) is not Candidate or candidate is not self._pending:
            raise XRError('not exact pending candidate')
        now = self._now()
        if candidate.barrier != self._barrier or now > candidate.expires_ns:
            self._invalidate()
            raise XRError('candidate revoked or expired')
        try:
            for sample in candidate.samples: self._fresh(sample, now)
        except XRError:
            self._invalidate()
            raise
        return now
    def approval_request(self, candidate):
        with self._lock:
            self._candidate(candidate)
            payload = hashlib.sha256(canonical(asdict(candidate))).hexdigest()
            request = dict(schema='xtos.action.v1',
                argv=['/usr/libexec/xtos/xr-feedback-sim', '--candidate-sha256', payload],
                root='/', cwd='/', uid=65534, gid=65534,
                limits=dict(cpu_sec=1, as_bytes=67108864, nofile=16, nproc=1),
                timeout_ms=1000, writable_dirs=[],
                sandbox_policy=dict(version='xtos.runner.v1', clear_groups=True,
                                    deny_socket=True, deny_getpid=True))
            snapshot(request)  # Full existing sandbox schema, not a parallel authority.
            return request
    def confirm(self, candidate, nonce):
        request = self.approval_request(candidate)
        if type(self._gate) is not ActionGate: raise XRError('existing ActionGate required')
        def memory_only(frozen):
            with self._lock:
                now = self._candidate(candidate)  # after durable consume; races revoke here
                self._pending = None  # burn before publishing any simulated receipt
                return Receipt(candidate.candidate_id, candidate.barrier, now)
        try:
            # NEVER hold owner lock across filesystem/authority waits.
            return self._gate.execute(nonce, request, memory_only)
        except Exception:
            with self._lock:
                if self._pending is candidate: self._invalidate()
            raise
    def _invalidate(self):
        self._barrier += 1
        self._latest.clear()
        self._pending = None
    def barrier(self, reason):
        with self._lock:
            self._invalidate()
            if type(reason) is not str or not 1 <= len(reason) <= 256:
                raise XRError('invalid barrier reason')
            return self._barrier
    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._invalidate()
    def _now(self):
        if self._closed: raise XRError('closed')
        try:
            now = self._clock()
            integer(now)
            if now < self._last_now: raise XRError('clock rollback')
            self._last_now = now
            return now
        except Exception as exc:
            self._invalidate()
            raise XRError('trusted clock failure or rollback') from exc
    def _fresh(self, sample, now):
        if (sample.barrier != self._barrier or
                not sample.converted_host_ns <= sample.host_observed_ns <= now or
                now - sample.converted_host_ns > self._age or
                sample.uncertainty_ns > self._uncertainty):
            raise XRError('stale, future, uncertain or barrier mismatch')
    def _check_latest(self, now):
        for sample in self._latest.values(): self._fresh(sample, now)
    def ingest(self, data):
        with self._lock:
            try:
                now = self._now()
                self._check_latest(now)
                fields = owned_fields(data)
                if fields['quality'] != Quality(True, True, False): raise XRError('low quality')
                domain, timestamp = fields['clock_domain'], fields['source_timestamp_ns']
                if domain == 'host_monotonic_ns':
                    converted = dict(converted_host_ns=timestamp, uncertainty_ns=0,
                                     conversion_id='host_monotonic_ns')
                else:
                    if not callable(self._converter): raise XRError('trusted converter unavailable')
                    converted = self._converter(domain, timestamp)
                    if type(converted) is not dict: raise XRError('invalid conversion')
                    converted = converted.copy()
                    if set(converted) != {'converted_host_ns', 'uncertainty_ns', 'conversion_id'}:
                        raise XRError('invalid conversion fields')
                    integer(converted['converted_host_ns'])
                    integer(converted['uncertainty_ns'], 0, self._uncertainty)
                    identifier(converted['conversion_id'])
                modality = fields['modality']
                previous = self._history.get(modality)
                if previous is not None:
                    sid, kind, domain0, generation, seq, timestamp0, host0 = previous
                    changed = (fields['source_id'], fields['source_kind'], domain) != (sid, kind, domain0)
                    if not changed and fields['generation'] < generation: raise XRError('old generation')
                    if changed or fields['generation'] != generation:
                        self._invalidate()
                    elif (fields['sequence'] != seq + 1 or timestamp < timestamp0 or
                          converted['converted_host_ns'] < host0):
                        raise XRError('sequence gap/replay or timestamp rollback')
                result = Sample(**fields, **converted, barrier=self._barrier)
                now = self._now()  # converter may have taken time; no cached receive age
                self._check_latest(now)
                self._fresh(result, now)
                self._history[modality] = (result.source_id, result.source_kind, domain,
                    result.generation, result.sequence, timestamp, result.converted_host_ns)
                self._latest[modality] = result
                self._pending = None
                return result
            except Exception as exc:
                self._invalidate()
                if isinstance(exc, XRError): raise
                raise XRError('sample or conversion refused') from exc
    def join(self):
        with self._lock:
            try:
                now = self._now()
                self._check_latest(now)
                if len(self._latest) != 3: raise XRError('three modalities required')
                samples = tuple(self._latest[m] for m in ('eeg', 'eye_gaze', 'emg'))
                times = [s.converted_host_ns for s in samples]
                if max(times) - min(times) > self._skew: raise XRError('skew exceeded')
                if self._pending is not None:
                    if now > self._pending.expires_ns: raise XRError('candidate expired')
                    return self._pending
                import secrets
                self._pending = Candidate(secrets.token_hex(16), samples, self._barrier, now,
                    min(I64, now + self._ttl, min(t + self._age for t in times)))
                return self._pending
            except Exception:
                self._invalidate()
                raise
