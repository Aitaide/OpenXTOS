"""Bounded local EEG JSONL process; every output is no-action software evidence."""
import argparse
from dataclasses import asdict
import json
import os
import re
import select
import signal
import sys
import threading
import time

from xtos_action_gate import ActionGate, TrustedStore
from xtos_action_gate.schema import GateError
from .controller import StreamController
from .feature_binding import validate_binding
from .simulation import SimulationFeedback

MAX_JSON = 65536
HEX = re.compile(r'[0-9a-f]{64}\Z')
REQUEST = 'xtos.eeg.approval-request.v1'
CONFIRM = 'xtos.eeg.confirm.v1'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('non-finite JSON')


def _decode(data):
    if len(data) > MAX_JSON:
        raise ValueError('oversized JSON')
    value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs,
                       parse_constant=_reject_constant)
    if type(value) is not dict:
        raise ValueError('expected JSON object')
    return value


class _Unavailable:
    def execute(self, nonce, request, executor):
        raise GateError('approval_unavailable')


def _production_gate():
    try:
        store = TrustedStore()
        # Verify initialized key/lock without creating or approving anything.
        with store.locked():
            pass
        return ActionGate(store)
    except (OSError, ValueError):
        return _Unavailable()


class _Process:
    """Internal library driver: only tests may supply an actual fixture gate."""
    def __init__(self, binding, socket_path, deadline_ms, candidate_ttl_ms, *, gate=None,
                 input_fd=0, output_fd=1):
        self.gate = _production_gate() if gate is None else gate
        self.controller = StreamController(binding, socket_path, SimulationFeedback(self.gate))
        self.deadline = deadline_ms
        self.ttl = candidate_ttl_ms
        self.input_fd, self.output_fd = input_fd, output_fd
        self.lock = threading.RLock()
        self.output_lock = threading.Lock()
        self.stopped = False
        self.wake_r, self.wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.inference = self.confirmation = None
        self.candidate = None
        self.serial = 0
        self.revision = 0
        self.failed = False
        self.closed = False

    def wake(self):
        try: os.write(self.wake_w, b'x')
        except OSError: pass

    def terminate(self, signum=None, frame=None):
        # Signal path never takes an owner lock or waits on a thread.
        self.stopped = True
        self.wake()

    def emit(self, status, reason='', **fields):
        document = dict(schema='xtos.eeg.process-result.v1', status=status, reason=reason,
                        **fields, safe_action='no_action', physical_acceptance=False)
        payload = json.dumps(document, allow_nan=False, separators=(',', ':')).encode()+b'\n'
        with self.output_lock:
            end = time.monotonic()+0.25
            try:
                while payload:
                    if self.stopped or time.monotonic() >= end:
                        raise OSError('output stopped')
                    try:
                        count = os.write(self.output_fd, payload)
                        if count == 0: raise OSError('output stopped')
                        payload = payload[count:]
                    except BlockingIOError:
                        select.select([], [self.output_fd], [], min(0.02, max(0, end-time.monotonic())))
            except OSError:
                self.failed = True
                self.terminate()

    def close(self, reason):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.candidate = None
                self.controller.close(reason)

    def malformed(self):
        with self.lock:
            self.revision += 1
            self.candidate = None
            result = self.controller.handle({})
            self.emit(result.status, result.reason)

    @staticmethod
    def _reap(owner):
        if owner is not None and not owner.is_alive():
            owner.join()
            return None
        return owner

    def _infer(self, row, identity, revision):
        result = self.controller.infer(row, identity, self.deadline, candidate_ttl_ms=self.ttl)
        # Same lock as all input invalidations, through the actual publication.
        with self.lock:
            if self.closed or self.stopped:
                return
            if revision != self.revision:
                self.controller.handle({})
                self.emit('rejected', 'input invalidated')
                return
            fields = {}
            if result.candidate is not None:
                try:
                    self.controller.approval_request(result.candidate)
                except ValueError:
                    self.emit('rejected', 'candidate revoked')
                    return
                self.candidate = result.candidate
                fields['candidate_id'] = result.candidate.candidate_id
                fields['provenance'] = 'unverified'
                if isinstance(self.gate, _Unavailable):
                    self.emit('approval_unavailable', 'approval_unavailable', **fields)
                    return
            if result.output is not None:
                fields['output'] = result.output
            self.emit(result.status, result.reason, **fields)

    def _confirm(self, candidate, nonce):
        try:
            receipt = self.controller.confirm(candidate, nonce)
            with self.lock:
                if not self.closed and not self.stopped:
                    if self.candidate is candidate:
                        self.candidate = None
                    # Receipt intentionally contains no nonce.
                    self.emit(receipt.status, candidate_id=receipt.candidate_id,
                              request_id=receipt.request_id, simulated_at_ns=receipt.simulated_at_ns)
        except (ValueError, OSError):
            with self.lock:
                if not self.closed and not self.stopped:
                    self.emit('rejected', 'confirmation refused')

    def handle(self, row):
        with self.lock:
            self.inference = self._reap(self.inference)
            self.confirmation = self._reap(self.confirmation)
            schema = row.get('schema')
            if schema in (REQUEST, CONFIRM):
                fields = {'schema', 'candidate_id'} | ({'nonce'} if schema == CONFIRM else set())
                if (row.keys() != fields or type(row.get('candidate_id')) is not str or
                        HEX.fullmatch(row['candidate_id']) is None or
                        (schema == CONFIRM and (type(row.get('nonce')) is not str or HEX.fullmatch(row['nonce']) is None))):
                    self.malformed(); return
                candidate = self.candidate
                if candidate is None or candidate.candidate_id != row['candidate_id']:
                    self.malformed(); return
                try:
                    request = self.controller.approval_request(candidate)
                except ValueError:
                    self.malformed(); return
                if isinstance(self.gate, _Unavailable):
                    self.emit('approval_unavailable', 'approval_unavailable'); return
                if schema == REQUEST:
                    self.emit('approval_request', candidate_id=candidate.candidate_id, request=request)
                elif self.confirmation is not None:
                    self.emit('rejected', 'confirmation busy')
                else:
                    self.confirmation = threading.Thread(target=self._confirm, args=(candidate, row['nonce']),
                                                         name='eeg-confirmation')
                    self.confirmation.start()
                return
            result = self.controller.handle(row)
            if result.status == 'use_infer':
                incoming = tuple(row[k] for k in ('session_id','barrier_id','stream_generation','config_generation'))
                current = getattr(self, '_inflight_identity', None)
                # Keep the same continuity check after the last owner has joined.
                if current is not None and incoming[:2] < current[:2]:
                    self.emit('rejected', 'stale session/barrier'); return
                if self.inference is not None:
                    # No pending queue. Continuity-changing windows revoke the old
                    # owner too; malformed controls always use controller handling.
                    if incoming != current:
                        self.revision += 1
                        self.candidate = None
                        self.controller.handle({})
                    self.emit('rejected', 'busy'); return
                self.candidate = None
                self.serial += 1
                self._inflight_identity = tuple(row[k] for k in ('session_id','barrier_id','stream_generation','config_generation'))
                self.inference = threading.Thread(target=self._infer, args=(row, 'stream-'+str(self.serial), self.revision),
                                                  name='eeg-inference')
                self.inference.start()
            else:
                if result.status == 'rejected' and result.reason == 'stale event':
                    self.emit(result.status, result.reason)
                    return
                self.revision += 1
                self.candidate = None
                self.emit(result.status, result.reason)
                if result.status == 'closed':
                    self.closed = True

    def run(self):
        old_blocking = os.get_blocking(self.output_fd)
        os.set_blocking(self.output_fd, False)
        buffer = bytearray()
        try:
            while not self.closed and not self.stopped:
                try:
                    ready, _, _ = select.select([self.input_fd, self.wake_r], [], [])
                except InterruptedError:
                    continue
                if self.wake_r in ready:
                    os.read(self.wake_r, 4096)
                    if self.stopped: break
                if self.input_fd not in ready: continue
                chunk = os.read(self.input_fd, 4096)
                if not chunk:
                    if buffer:
                        try: self.handle(_decode(bytes(buffer)))
                        except (ValueError, RecursionError, OverflowError): self.malformed()
                    self.close('eof')
                    self.emit('closed', 'eof')
                    break
                buffer.extend(chunk)
                while b'\n' in buffer and not self.closed and not self.stopped:
                    line, _, rest = buffer.partition(b'\n')
                    buffer = bytearray(rest)
                    try: self.handle(_decode(bytes(line)))
                    except (ValueError, RecursionError, OverflowError): self.malformed()
                if len(buffer) > MAX_JSON:
                    self.malformed()
                    self.failed = True
                    self.close('oversized input')
            return 1 if self.failed or self.stopped else 0
        finally:
            self.close('process exit')
            # Reviewed client/store calls have fixed finite budgets. Output writes
            # also have a total bound, so these owners cannot await stdout forever.
            for owner in (self.inference, self.confirmation):
                if owner is not None: owner.join()
            os.set_blocking(self.output_fd, old_blocking)
            os.close(self.wake_r); os.close(self.wake_w)


def _milliseconds(value):
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError('expected 1..10000 milliseconds')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--deadline-ms', type=_milliseconds, required=True)
    parser.add_argument('--candidate-ttl-ms', type=_milliseconds, required=True)
    args = parser.parse_args()
    try:
        with open(args.binding, 'rb') as stream:
            binding = validate_binding(_decode(stream.read(MAX_JSON+1)))
        process = _Process(binding, args.socket, args.deadline_ms, args.candidate_ttl_ms)
    except (ValueError, OSError, RecursionError, OverflowError) as exc:
        parser.exit(2, 'EEG process configuration refused: '+str(exc)+'\n')
    previous = {s: signal.signal(s, process.terminate) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        return process.run()
    finally:
        for s, handler in previous.items(): signal.signal(s, handler)


if __name__ == '__main__':
    raise SystemExit(main())
