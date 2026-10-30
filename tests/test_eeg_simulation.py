"""Fixture authority + SERDEV-labelled software metadata, never hardware evidence."""
import dataclasses
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from xtos_action_gate import ActionGate, FixtureStore, GateError
from xtos_action_gate.schema import canonical, snapshot
from xtos_eeg_loop.feature_binding import PreparedInference


def prepared(**changes):
    fields = dict(request_id='request-1', model='eeg-fixture', expected_model_sha256='a'*64,
                  values=tuple([0.0]*8 + [1.0]*8), deadline_ms=100,
                  source_kind='serdev', session_id=1, stream_generation=2,
                  config_generation=3, window_id=4, barrier_id=5, first_sequence=10,
                  last_sequence=11, first_host_rx_ns=1_000_000_000,
                  last_host_rx_ns=1_001_000_000, max_receive_age_ns=200_000_000,
                  prepared_at_ns=1_002_000_000)
    return PreparedInference(**(fields | changes))


def transport(p=None, **changes):
    p = prepared() if p is None else p
    fields = dict(status='transport_verified', safe_action='no_action', reason='transport_only',
                  request_id=p.request_id, model=p.model, model_sha256=p.expected_model_sha256,
                  source_kind=p.source_kind, session_id=p.session_id,
                  stream_generation=p.stream_generation, config_generation=p.config_generation,
                  window_id=p.window_id, barrier_id=p.barrier_id,
                  feature_digest=hashlib.sha256(canonical(dataclasses.asdict(p))).hexdigest(),
                  completed_at_ns=1_003_000_000, output=[[0.25, 0.25, 0.5]])
    return fields | changes


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module('xtos_eeg_loop.simulation')
        evidence = ROOT / '.build/xtos-roadmap-v1/p3-simulation'
        evidence.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='eeg-sim-', dir=evidence)
        self.addCleanup(self.tmp.cleanup)
        self.store = FixtureStore(Path(self.tmp.name) / 'authority')
        self.wall = 1000
        self.gate = ActionGate(self.store, clock=lambda: self.wall)
        self.now = 1_004_000_000
        self.sim = self.api.SimulationFeedback(self.gate, now_ns=lambda: self.now)

    def offer(self, p=None, t=None, ttl_ms=50):
        p = prepared() if p is None else p
        return self.sim.offer(p, transport(p) if t is None else t, ttl_ms=ttl_ms)

    def approve(self, candidate):
        return self.gate.approve(self.sim.approval_request(candidate), actor='fixture-admin', ttl_seconds=60)

    def test_concurrent_probability_row_resize_is_rejected_before_candidate(self):
        raw: dict = transport()
        copying, mutated = threading.Event(), threading.Event()
        errors = []
        lines, first = inspect.getsourcelines(self.api._transport)
        copy_line = next(first + index for index, line in enumerate(lines)
                         if 'probabilities = tuple(' in line)

        def mutate():
            try:
                if not copying.wait(2):
                    raise RuntimeError('copy boundary not reached')
                raw['output'][0][:] = [0.125, 0.375, 0.25, 0.25]
            except BaseException as exc:
                errors.append(exc)
            finally:
                mutated.set()

        def trace(frame, event, arg):
            if (event == 'line' and frame.f_code is self.api._transport.__code__
                    and frame.f_lineno == copy_line):
                copying.set()
                if not mutated.wait(2):
                    raise RuntimeError('mutation thread did not finish')
            return trace

        thread = threading.Thread(target=mutate)
        thread.start()
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            with self.assertRaises(self.api.SimulationError):
                self.offer(t=raw)
        finally:
            sys.settrace(previous)
            copying.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(mutated.is_set())

    def test_probability_snapshot_survives_later_caller_mutation(self):
        # Added-green isolation control, separate from the behavioral RED.
        raw: dict = transport()
        copied, mutated = threading.Event(), threading.Event()
        errors = []
        lines, first = inspect.getsourcelines(self.api._transport)
        validate_line = next(first + index for index, line in enumerate(lines)
                             if 'if len(probabilities)' in line)

        def mutate():
            try:
                if not copied.wait(2):
                    raise RuntimeError('snapshot boundary not reached')
                raw['output'][0][:] = [0.125, 0.375, 0.25, 0.25]
                raw['output'][:] = []
            except BaseException as exc:
                errors.append(exc)
            finally:
                mutated.set()

        def trace(frame, event, arg):
            if (event == 'line' and frame.f_code is self.api._transport.__code__
                    and frame.f_lineno == validate_line):
                copied.set()
                if not mutated.wait(2):
                    raise RuntimeError('mutation thread did not finish')
            return trace

        thread = threading.Thread(target=mutate)
        thread.start()
        previous = sys.gettrace()
        try:
            sys.settrace(trace)
            candidate = self.offer(t=raw)
        finally:
            sys.settrace(previous)
            copied.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(candidate.transport.output, ((0.25, 0.25, 0.5),))
        token = self.approve(candidate)
        self.assertEqual(self.sim.confirm(candidate, token).safe_action, 'no_action')

    def test_candidate_binds_entire_owned_snapshot(self):
        p, t = prepared(), transport()
        candidate = self.offer(p, t)
        request = self.sim.approval_request(candidate)
        snapshot(request)
        document = json.loads(request['argv'][1])
        self.assertEqual(document, json.loads(canonical(dataclasses.asdict(candidate))))
        self.assertEqual(document['prepared'], json.loads(canonical(dataclasses.asdict(p))))
        self.assertEqual(document['transport'], t)
        self.assertEqual(document['provenance'], 'unverified')
        self.assertEqual(document['schema'], 'xtos.eeg.simulation-candidate.v1')
        self.assertEqual(candidate.expires_at_ns, self.now + 50_000_000)
        self.assertEqual(request | {'argv': None}, dict(
            schema='xtos.action.v1', argv=None, root='/', cwd='/', uid=65534, gid=65534,
            timeout_ms=1000, writable_dirs=[],
            limits=dict(cpu_sec=1, as_bytes=67108864, nofile=16, nproc=1),
            sandbox_policy=dict(version='xtos.runner.v1', clear_groups=True,
                                deny_socket=True, deny_getpid=False)))
        self.assertEqual(request['argv'][0], '/usr/libexec/xtos/eeg-feedback-sim')
        self.assertRegex(candidate.candidate_id, r'^[0-9a-f]{64}$')
        t['output'][0][0] = 1.0
        t['session_id'] = 999
        request['limits']['cpu_sec'] = 999
        self.assertEqual(json.loads(self.sim.approval_request(candidate)['argv'][1])['transport']['output'], [[0.25, 0.25, 0.5]])
        self.assertIsNot(candidate.prepared, p)
        for obj, field in ((candidate, 'candidate_id'), (candidate.prepared, 'request_id'), (candidate.transport, 'output')):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, field, None)

    def test_prepared_fields_revalidated_before_acceptance(self):
        cases = [(name, None) for name in dataclasses.asdict(prepared())]
        for name, value in dataclasses.asdict(prepared()).items():
            if type(value) is int:
                cases += [(name, True), (name, float(value)), (name, -1), (name, 1 << 64)]
        for name in ('session_id', 'stream_generation', 'config_generation', 'window_id', 'barrier_id', 'max_receive_age_ns'):
            cases.append((name, 0))
        cases += [('request_id', 'é'), ('model', 'a/b'), ('expected_model_sha256', 'A'*64),
                  ('deadline_ms', 0), ('deadline_ms', 300001), ('source_kind', 'other'),
                  ('values', [0.0]*16), ('values', tuple([0.0]*15)),
                  ('values', tuple([True]*16)), ('values', tuple([0]*16)),
                  ('values', tuple([float('nan')]*16)), ('values', tuple([3.5e38]*16)),
                  ('values', tuple([0.1]*16)), ('values', tuple([1.0]*8+[0.0]*8)),
                  ('values', tuple([0.0]*8+[-1e-100]*8)), ('last_sequence', 9),
                  ('last_sequence', 5000), ('last_host_rx_ns', 999_000_000),
                  ('prepared_at_ns', 999_000_000)]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                old = self.offer()
                with self.assertRaises(self.api.SimulationError):
                    p = dataclasses.replace(prepared(), **{name: value})
                    self.offer(p, transport())
                with self.assertRaises(self.api.SimulationError):
                    self.sim.approval_request(old)
        for p in (None, dataclasses.asdict(prepared())):
            with self.assertRaises(self.api.SimulationError):
                self.sim.offer(p, transport(), ttl_ms=50)

    def test_transport_schema_probabilities_and_synthetic_refusal(self):
        bad = [(name, None) for name in transport()]
        for name in ('request_id', 'model', 'model_sha256', 'source_kind', 'feature_digest'):
            bad.append((name, 'wrong'))
        for name in ('session_id', 'stream_generation', 'config_generation', 'window_id', 'barrier_id', 'completed_at_ns'):
            bad += [(name, True), (name, 1.0), (name, 0), (name, 1 << 64)]
        bad += [('status', 'rejected'), ('safe_action', 'allowed'), ('reason', 'other'),
                ('completed_at_ns', 1_001_000_000), ('completed_at_ns', self.now+1),
                ('output', []), ('output', [[True, 0, 0]]), ('output', [[0, 0, 0]]),
                ('output', [[float('nan'), 0, 1]]), ('output', [[-0.1, 0.6, 0.5]]),
                ('output', [[1.1, 0, 0]]), ('output', [[0.5, 0.5]]),
                ('output', ((0.25, 0.25, 0.5),))]
        for name, value in bad:
            with self.subTest(name=name, value=value), self.assertRaises(self.api.SimulationError):
                self.offer(t=transport(**{name: value}))
        for t in (None, [], transport(extra=True), {k:v for k,v in transport().items() if k != 'reason'}):
            with self.assertRaises(self.api.SimulationError):
                self.sim.offer(prepared(), t, ttl_ms=10)
        p = prepared(source_kind='synthetic')
        with patch.object(self.gate, 'approve', side_effect=AssertionError('must not approve')):
            with self.assertRaises(self.api.SimulationError):
                self.offer(p, transport(p))

    def test_expiry_freshness_and_overflow_boundaries(self):
        for ttl in (True, 1.0, 0, -1, 300001, None):
            with self.subTest(ttl=ttl), self.assertRaises(self.api.SimulationError):
                self.offer(ttl_ms=ttl)
        for now in (True, 1.0, -1, 1 << 64, 1_002_999_999, 1_102_000_000, 1_200_000_001):
            self.now = now
            with self.subTest(now=now), self.assertRaises(self.api.SimulationError):
                self.offer()
        self.now = 1_004_000_000
        c = self.offer(ttl_ms=300000)
        self.assertEqual(c.expires_at_ns, 1_102_000_000)
        self.now = c.expires_at_ns
        with self.assertRaises(self.api.SimulationError):
            self.sim.approval_request(c)
        self.now = 1_200_000_000
        p = prepared(deadline_ms=1000)
        c = self.offer(p, transport(p), ttl_ms=1000)
        self.assertEqual(c.expires_at_ns, self.now+1)
        self.now += 1
        with self.assertRaises(self.api.SimulationError):
            self.sim.approval_request(c)
        for now in (True, 1.0, -1, 1 << 64, 1_003_999_999):
            self.now = 1_004_000_000
            c = self.offer()
            self.now = now
            with self.assertRaises(self.api.SimulationError):
                self.sim.approval_request(c)
        top = (1 << 64)-1
        self.now = top-100
        p = prepared(first_host_rx_ns=top-1000, last_host_rx_ns=top-900,
                     prepared_at_ns=top-800)
        with self.assertRaises(self.api.SimulationError):
            self.offer(p, transport(p, completed_at_ns=top-700))

    def test_confirmation_once_replay_and_request_mutation(self):
        c = self.offer()
        nonce = self.approve(c)
        receipt = self.sim.confirm(c, nonce)
        self.assertEqual(dataclasses.asdict(receipt), dict(status='simulated', safe_action='no_action',
            candidate_id=c.candidate_id, request_id='request-1', simulated_at_ns=self.now))
        self.assertTrue((self.store.path / (nonce+'.spent')).is_file())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.safe_action = 'allowed'
        with self.assertRaises((self.api.SimulationError, GateError)):
            self.sim.confirm(c, nonce)
        c = self.offer()
        request = self.sim.approval_request(c)
        request['argv'][1] += ' '
        nonce = self.gate.approve(request, actor='fixture-admin', ttl_seconds=60)
        with self.assertRaises(GateError):
            self.sim.confirm(c, nonce)
        self.assertTrue((self.store.path / (nonce+'.spent')).exists())
        # An ordinary gate denial does not invent a receipt or bypass freshness.
        self.sim.confirm(c, self.approve(c))

    def test_foreign_reconstructed_replacement_and_second_instance(self):
        old = self.offer()
        old_nonce = self.approve(old)
        new = self.offer()
        for foreign in (old, dataclasses.replace(new), None):
            with self.assertRaises(self.api.SimulationError):
                self.sim.confirm(foreign, old_nonce)
            self.sim.approval_request(new)
        other = self.api.SimulationFeedback(self.gate, now_ns=lambda: self.now)
        foreign = other.offer(prepared(), transport(), ttl_ms=50)
        self.assertNotEqual(new.candidate_id, foreign.candidate_id)
        token = self.gate.approve(other.approval_request(foreign), actor='fixture-admin', ttl_seconds=60)
        with self.assertRaises(GateError):
            self.sim.confirm(new, token)
        self.sim.confirm(new, self.approve(new))

    def test_invalidate_close_and_invalid_reason_revoke(self):
        for method in ('invalidate', 'close'):
            owner = self.api.SimulationFeedback(self.gate, now_ns=lambda: self.now)
            c = owner.offer(prepared(), transport(), ttl_ms=50)
            with self.assertRaises(self.api.SimulationError):
                getattr(owner, method)(None)
            with self.assertRaises(self.api.SimulationError):
                owner.approval_request(c)
            getattr(owner, method)('stop')
            if method == 'close':
                with self.assertRaises(self.api.SimulationError):
                    owner.offer(prepared(), transport(), ttl_ms=50)

    def test_consume_before_callback_and_invalidation_races(self):
        original = self.gate.execute
        for event in ('invalidate', 'close', 'replace', 'expire'):
            owner = self.api.SimulationFeedback(self.gate, now_ns=lambda: self.now)
            c = owner.offer(prepared(), transport(), ttl_ms=50)
            token = self.gate.approve(owner.approval_request(c), actor='fixture-admin', ttl_seconds=60)
            replacements = []
            def execute(nonce, request, callback):
                def stalled(frozen):
                    self.assertTrue((self.store.path / (nonce+'.spent')).exists())
                    if event == 'replace':
                        replacements.append(owner.offer(prepared(), transport(), ttl_ms=50))
                    elif event == 'expire':
                        self.now = c.expires_at_ns
                    else:
                        getattr(owner, event)('barrier')
                    return callback(frozen)
                return original(nonce, request, stalled)
            with patch.object(self.gate, 'execute', side_effect=execute):
                with self.assertRaises(self.api.SimulationError):
                    owner.confirm(c, token)
            if replacements:
                owner.approval_request(replacements[0])
            self.assertTrue((self.store.path / (token+'.spent')).exists())
            self.now = 1_004_000_000

    def test_two_threads_can_confirm_only_once(self):
        c = self.offer()
        tokens = [self.approve(c), self.approve(c)]
        original = self.gate.execute
        rendezvous = threading.Barrier(2)
        results, errors = [], []
        def execute(nonce, request, callback):
            rendezvous.wait(timeout=2)
            return original(nonce, request, callback)
        def worker(token):
            try:
                results.append(self.sim.confirm(c, token))
            except (self.api.SimulationError, GateError) as exc:
                errors.append(exc)
        with patch.object(self.gate, 'execute', side_effect=execute):
            threads = [threading.Thread(target=worker, args=(token,)) for token in tokens]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertTrue(all((self.store.path / (token+'.spent')).exists() for token in tokens))

    def test_full_candidate_every_leaf_is_approval_bound(self):
        c = self.offer()
        request = self.sim.approval_request(c)
        document = json.loads(request['argv'][1])
        def leaves(value, path=()):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield from leaves(item, path+(key,))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from leaves(item, path+(index,))
            else:
                yield path
        for path in leaves(document):
            with self.subTest(path=path):
                changed = json.loads(request['argv'][1])
                node = changed
                for key in path[:-1]:
                    node = node[key]
                value = node[path[-1]]
                node[path[-1]] = value+'x' if type(value) is str else value+1
                changed_request = self.sim.approval_request(c)
                changed_request['argv'][1] = canonical(changed).decode('ascii')
                nonce = self.gate.approve(changed_request, actor='fixture-admin', ttl_seconds=60)
                with self.assertRaises(GateError):
                    self.sim.confirm(c, nonce)
                self.assertTrue((self.store.path / (nonce+'.spent')).exists())
        self.sim.confirm(c, self.approve(c))

    def test_fixed_callback_no_external_io_or_callback_injection(self):
        import builtins
        import os
        import socket
        import subprocess
        from contextlib import ExitStack
        with self.assertRaises(TypeError):
            self.api.SimulationFeedback(self.gate, executor=lambda _: None)
        c = self.offer()
        token = self.approve(c)
        original = self.gate.execute
        def execute(nonce, request, callback):
            def isolated(frozen):
                with ExitStack() as stack:
                    for target, name in ((builtins, 'open'), (os, 'open'), (os, 'system'),
                                         (socket, 'socket'), (subprocess, 'Popen'), (subprocess, 'run')):
                        stack.enter_context(patch.object(target, name, side_effect=AssertionError('external I/O')))
                    return callback(frozen)
            return original(nonce, request, isolated)
        with patch.object(self.gate, 'execute', side_effect=execute):
            self.assertEqual(self.sim.confirm(c, token).safe_action, 'no_action')

    def test_callback_failure_burns_nonce_and_candidate(self):
        c = self.offer()
        token = self.approve(c)
        with patch.object(self.api, 'Receipt', side_effect=RuntimeError('result failure')):
            with self.assertRaises(RuntimeError):
                self.sim.confirm(c, token)
        self.assertTrue((self.store.path / (token+'.spent')).exists())
        with self.assertRaises(self.api.SimulationError):
            self.sim.approval_request(c)

    def test_storage_failure_after_invalidation_preserves_replacement(self):
        c = self.offer()
        token = self.approve(c)
        replacements = []
        def fail(*args):
            self.sim.invalidate('barrier')
            replacements.append(self.offer())
            raise GateError('storage unavailable')
        with patch.object(self.gate, 'execute', side_effect=fail):
            with self.assertRaises(GateError):
                self.sim.confirm(c, token)
        with self.assertRaises(self.api.SimulationError):
            self.sim.confirm(c, token)
        self.sim.approval_request(replacements[0])

    def test_callback_wins_before_later_thread_invalidation(self):
        c = self.offer()
        token = self.approve(c)
        entered, invalidating = threading.Event(), threading.Event()
        original = self.api.Receipt
        results, errors = [], []
        def receipt(*args):
            entered.set()
            self.assertTrue(invalidating.wait(timeout=2))
            return original(*args)
        def confirm():
            try:
                results.append(self.sim.confirm(c, token))
            except BaseException as exc:
                errors.append(exc)
        def invalidate():
            try:
                self.assertTrue(entered.wait(timeout=2))
                invalidating.set()
                self.sim.invalidate('barrier')
            except BaseException as exc:
                errors.append(exc)
        with patch.object(self.api, 'Receipt', side_effect=receipt):
            threads = [threading.Thread(target=confirm), threading.Thread(target=invalidate)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].safe_action, 'no_action')
        with self.assertRaises(self.api.SimulationError):
            self.sim.approval_request(c)

    def test_gate_expiry_and_callback_clock_failures(self):
        c = self.offer()
        token = self.approve(c)
        self.wall += 60
        with self.assertRaises(GateError):
            self.sim.confirm(c, token)
        self.assertTrue((self.store.path / (token+'.spent')).exists())
        for now in (True, 1.0, -1, 1 << 64, 1_003_999_999):
            c = self.offer()
            token = self.approve(c)
            original = self.gate.execute
            def execute(nonce, request, callback):
                def regress(frozen):
                    self.now = now
                    return callback(frozen)
                return original(nonce, request, regress)
            with patch.object(self.gate, 'execute', side_effect=execute):
                with self.assertRaises(self.api.SimulationError):
                    self.sim.confirm(c, token)
            self.assertTrue((self.store.path / (token+'.spent')).exists())
            self.now = 1_004_000_000
            with self.assertRaises(self.api.SimulationError):
                self.sim.approval_request(c)

    def test_invalidation_thread_wins_before_consumed_callback(self):
        original = self.gate.execute
        for operation in ('invalidate', 'close', 'replace'):
            owner = self.api.SimulationFeedback(self.gate, now_ns=lambda: self.now)
            c = owner.offer(prepared(), transport(), ttl_ms=50)
            token = self.gate.approve(owner.approval_request(c), actor='fixture-admin', ttl_seconds=60)
            entered, release = threading.Event(), threading.Event()
            errors, results = [], []
            def execute(nonce, request, callback):
                def blocked(frozen):
                    entered.set()
                    if not release.wait(timeout=2):
                        raise RuntimeError('schedule timeout')
                    return callback(frozen)
                return original(nonce, request, blocked)
            def confirm():
                try:
                    results.append(owner.confirm(c, token))
                except BaseException as exc:
                    errors.append(exc)
            with patch.object(self.gate, 'execute', side_effect=execute):
                thread = threading.Thread(target=confirm)
                thread.start()
                try:
                    self.assertTrue(entered.wait(timeout=2))
                    self.assertTrue((self.store.path / (token+'.spent')).exists())
                    if operation == 'replace':
                        replacement = owner.offer(prepared(), transport(), ttl_ms=50)
                    else:
                        getattr(owner, operation)('barrier')
                finally:
                    release.set()
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results, [])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], self.api.SimulationError)
            if operation == 'replace':
                owner.approval_request(replacement)

    def test_callback_deadline_oldest_age_and_real_nonce_replay(self):
        original = self.gate.execute
        for p, now in ((prepared(), 1_102_000_000),
                       (prepared(deadline_ms=1000), 1_200_000_001)):
            c = self.offer(p, transport(p), ttl_ms=1000)
            token = self.approve(c)
            def execute(nonce, request, callback):
                def expire(frozen):
                    self.now = now
                    return callback(frozen)
                return original(nonce, request, expire)
            with patch.object(self.gate, 'execute', side_effect=execute):
                with self.assertRaises(self.api.SimulationError):
                    self.sim.confirm(c, token)
            self.assertTrue((self.store.path / (token+'.spent')).exists())
            self.now = 1_004_000_000
        c = self.offer()
        token = self.approve(c)
        self.sim.confirm(c, token)
        replacement = self.offer()
        with self.assertRaises(GateError):
            self.sim.confirm(replacement, token)
        self.sim.approval_request(replacement)

    def test_api_available(self):
        for name in ('SimulationFeedback', 'Candidate', 'Receipt', 'SimulationError'):
            self.assertTrue(hasattr(self.api, name))


if __name__ == '__main__':
    unittest.main()
