"""Real JSONL child and socket tests; all outputs are software evidence."""
import importlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tools/xtos-ai-runtime')]
from test_eeg_feature_binding import configuration, window
from test_eeg_cpu_transport import success

ART = Path(os.environ.get('XTOS_PROCESS_ARTIFACTS', ROOT/'.build/xtos-roadmap-v1/p3-process/implementation-evidence/children'))
ART.mkdir(parents=True, exist_ok=True)


def record(serdev=False):
    now = time.monotonic_ns()
    return dict(window(), first_host_rx_ns=now, last_host_rx_ns=now,
                emitted_at_ns=now, max_receive_age_ns=10_000_000_000,
                source_flags=1 if serdev else 2, source_kind='serdev' if serdev else 'synthetic')


def event(summary=False):
    row = dict(schema='xtos.eeg.barrier.v1', session_id=2, barrier_id=2,
               reason='stop', safe_action='no_action', physical_acceptance=False)
    if summary:
        row.update(schema='xtos.eeg.stream-summary.v1', status='complete', windows_emitted=1)
    return row


@contextmanager
def held_peer(*, connections=1):
    with tempfile.TemporaryDirectory(prefix='ep-', dir=os.environ.get('TMPDIR')) as tmp:
        path = Path(tmp)/'s'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path)); listener.listen(1); listener.settimeout(4)
        ready, release = threading.Event(), threading.Event()
        received, errors = [], []
        def serve():
            try:
                for _ in range(connections):
                    connection, _ = listener.accept()
                    with connection, connection.makefile('rb') as stream:
                        received.append(json.loads(stream.readline(65537)))
                        ready.set()
                        if not release.wait(4): raise AssertionError('held peer timeout')
                        connection.sendall(json.dumps(dict(success(), id=received[-1]['id'])).encode()+b'\n')
            except (BrokenPipeError, ConnectionResetError): pass
            except BaseException as exc: errors.append(repr(exc)); ready.set()
            finally: listener.close()
        thread = threading.Thread(target=serve); thread.start()
        try: yield path, ready, release, received
        finally:
            release.set(); thread.join(5)
            if thread.is_alive() or errors: raise AssertionError(errors)


class ProcessTests(unittest.TestCase):
    serial = 0
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pr-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.archive_generated)
        self.binding = Path(self.tmp.name)/'b.json'
        self.binding.write_text(json.dumps(dict(configuration(), max_receive_age_ns=10_000_000_000)))
        self.children = []
        self.outputs = {}
        self.addCleanup(self.cleanup_children)

    def archive_generated(self):
        import shutil
        import stat
        destination = ART/(self._testMethodName+'-generated-'+str(time.monotonic_ns()))
        def ignore(directory, names):
            return [name for name in names if stat.S_ISSOCK((Path(directory)/name).lstat().st_mode)]
        shutil.copytree(self.tmp.name, destination, ignore=ignore)

    def child(self, path='/missing', extra=()):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT/'tools'), str(ROOT/'tools/xtos-ai-runtime')]))
        cmd = [sys.executable, '-m', 'xtos_eeg_loop.stream', '--binding', str(self.binding),
               '--socket', str(path), '--deadline-ms', '3000', '--candidate-ttl-ms', '3000', *extra]
        child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.children.append(child); self.outputs[child.pid] = bytearray()
        return child

    def send(self, child, row):
        child.stdin.write(json.dumps(row).encode()+b'\n'); child.stdin.flush()

    def read(self, child, timeout=2):
        self.assertTrue(select.select([child.stdout], [], [], timeout)[0], 'child did not respond')
        line = child.stdout.readline(); self.outputs[child.pid].extend(line)
        self.assertTrue(line, child.stderr.peek() if child.poll() is not None else b'empty')
        row = json.loads(line)
        self.assertEqual(row['safe_action'], 'no_action'); self.assertIs(row['physical_acceptance'], False)
        return row

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None: child.terminate()
            try: child.wait(5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(); raise
            if child.stdin and not child.stdin.closed: child.stdin.close()
            out = b'' if child.stdout.closed else child.stdout.read()
            err = child.stderr.read()
            ProcessTests.serial += 1
            prefix = ART/(self._testMethodName+'-'+str(ProcessTests.serial))
            prefix.with_suffix('.stdout').write_bytes(bytes(self.outputs[child.pid])+out)
            prefix.with_suffix('.stderr').write_bytes(err)
            prefix.with_suffix('.json').write_text(json.dumps({'command': child.args, 'exit': child.returncode}))
            child.stdout.close(); child.stderr.close()

    def test_reader_revokes_held_response(self):
        with held_peer() as (path, ready, release, received):
            child = self.child(path); self.send(child, record(True))
            self.assertTrue(ready.wait(2), 'inference never reached socket')
            self.send(child, event())
            self.assertEqual(self.read(child)['status'], 'invalidated')
            release.set()
            self.assertEqual(self.read(child)['status'], 'rejected')
            child.stdin.close(); self.assertEqual(child.wait(4), 0)

    def test_strict_json_and_controls_revoke(self):
        child = self.child()
        cases = [b'{"schema":"x","schema":"y"}', b'{"x":NaN}', b'{"x":Infinity}',
                 b'[]', b'\xff', b'{', json.dumps(dict(schema='xtos.eeg.confirm.v1',
                 candidate_id='a'*64, nonce='b'*64, actor='root')).encode(),
                 json.dumps(dict(schema='xtos.eeg.approval-request.v1', candidate_id='A'*64)).encode()]
        for payload in cases:
            child.stdin.write(payload+b'\n'); child.stdin.flush()
            self.assertEqual(self.read(child)['status'], 'rejected')
        child.stdin.close(); self.assertEqual(child.wait(3), 0)

    def test_oversized_line_and_binding(self):
        child = self.child(); child.stdin.write(b' '*65537); child.stdin.flush()
        self.assertEqual(self.read(child)['status'], 'rejected')
        self.assertNotEqual(child.wait(3), 0)
        for payload in (b' '*65537, self.binding.read_bytes()[:-1]+b',"model":"duplicate"}'):
            self.binding.write_bytes(payload)
            child = self.child(); child.stdin.close()
            self.assertEqual(child.wait(3), 2)

    def test_cli_has_no_authority_or_backend_selector(self):
        for option in ('--fixture-store', '--store', '--actor', '--executor', '--backend'):
            child = self.child(extra=(option, '/somewhere')); child.stdin.close()
            self.assertEqual(child.wait(3), 2)
        for value in ('0', '10001', 'true'):
            child = self.child(extra=('--deadline-ms', value)); child.stdin.close()
            self.assertEqual(child.wait(3), 2)

    def test_busy_has_single_inference_owner(self):
        with held_peer() as (path, ready, release, received):
            child = self.child(path); row = record(); self.send(child, row)
            self.assertTrue(ready.wait(2))
            for _ in range(20):
                self.send(child, row)
                self.assertEqual(self.read(child)['reason'], 'busy')
            self.assertLessEqual(len(list(Path('/proc/'+str(child.pid)+'/task').iterdir())), 2)
            release.set(); self.assertEqual(self.read(child)['status'], 'transport_verified')
            self.assertEqual(len(received), 1)
            child.stdin.close(); self.assertEqual(child.wait(4), 0)

    def test_summary_and_eof_before_response(self):
        for terminal in ('summary', 'eof'):
            with self.subTest(terminal=terminal), held_peer() as (path, ready, release, received):
                child = self.child(path); self.send(child, record(True)); self.assertTrue(ready.wait(2))
                if terminal == 'summary': self.send(child, event(True))
                else: child.stdin.close()
                self.assertEqual(self.read(child)['status'], 'closed')
                self.assertIsNone(child.poll(), 'must join held owner')
                release.set(); self.assertEqual(child.wait(4), 0)
                self.assertEqual(child.stdout.read(), b'')

    def test_signals_release_input_and_join(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig=sig), held_peer() as (path, ready, release, received):
                child = self.child(path); self.send(child, record(True)); self.assertTrue(ready.wait(2))
                child.send_signal(sig); time.sleep(0.05)
                self.assertIsNone(child.poll())
                release.set(); self.assertNotEqual(child.wait(4), 0)
                self.assertEqual(child.stdout.read(), b'')
        child = self.child(); time.sleep(0.1); child.terminate()
        self.assertNotEqual(child.wait(3), 0)

    def test_broken_output_releases_input_and_joins(self):
        with held_peer() as (path, ready, release, received):
            child = self.child(path); self.send(child, record(True)); self.assertTrue(ready.wait(2))
            child.stdout.close(); self.send(child, event())
            time.sleep(0.05); self.assertIsNone(child.poll())
            release.set(); self.assertNotEqual(child.wait(4), 0)

    def driver(self, gate, path):
        api = importlib.import_module('xtos_eeg_loop.stream')
        r, w = os.pipe()
        fd, filename = tempfile.mkstemp(prefix='driver-', dir=self.tmp.name)
        out = Path(filename)
        process = api._Process(dict(configuration(), max_receive_age_ns=10_000_000_000), str(path),
                               3000, 3000, gate=gate, input_fd=r, output_fd=fd)
        self.addCleanup(lambda: [os.close(f) for f in (r, w, fd, process.wake_r, process.wake_w)])
        self.addCleanup(process.close, 'test cleanup')
        return process, out

    def join_owner(self, process, name='inference'):
        owner = getattr(process, name); owner.join(4); self.assertFalse(owner.is_alive())

    def test_fixture_exact_request_confirmation_and_replay(self):
        from xtos_action_gate import ActionGate, FixtureStore
        gate = ActionGate(FixtureStore(Path(self.tmp.name)/'authority'))
        with held_peer() as (path, ready, release, received):
            process, out = self.driver(gate, path)
            process.handle(record(True)); self.assertTrue(ready.wait(2)); release.set(); self.join_owner(process)
        candidate = json.loads(out.read_text().splitlines()[0])['candidate_id']
        request = dict(schema='xtos.eeg.approval-request.v1', candidate_id=candidate)
        process.handle(request)
        row = json.loads(out.read_text().splitlines()[-1])
        self.assertEqual(row['status'], 'approval_request')
        nonce = gate.approve(row['request'], actor='software-fixture', ttl_seconds=60)
        confirm = dict(schema='xtos.eeg.confirm.v1', candidate_id=candidate, nonce=nonce)
        process.handle(confirm); self.join_owner(process, 'confirmation')
        row = json.loads(out.read_text().splitlines()[-1]); self.assertEqual(row['status'], 'simulated')
        self.assertNotIn(nonce, json.dumps(row))
        self.assertTrue((gate.store.path/(nonce+'.spent')).exists())
        process.handle(confirm)
        self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'rejected')

    def test_missing_authority_transport_and_simulation_denial(self):
        api = importlib.import_module('xtos_eeg_loop.stream')
        for serdev in (False, True):
            with self.subTest(serdev=serdev), held_peer() as (path, ready, release, received):
                process, out = self.driver(api._Unavailable(), path)
                process.handle(record(serdev)); self.assertTrue(ready.wait(2)); release.set(); self.join_owner(process)
                row = json.loads(out.read_text().splitlines()[-1])
                self.assertEqual(row['status'], 'approval_unavailable' if serdev else 'transport_verified')
                if serdev:
                    process.handle(dict(schema='xtos.eeg.confirm.v1', candidate_id=row['candidate_id'], nonce='b'*64))
                    self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'approval_unavailable')

    def test_store_consume_does_not_block_terminal(self):
        from xtos_action_gate import ActionGate, FixtureStore
        store = FixtureStore(Path(self.tmp.name)/'authority'); gate = ActionGate(store)
        with held_peer() as (path, ready, release, received):
            process, out = self.driver(gate, path); process.handle(record(True))
            self.assertTrue(ready.wait(2)); release.set(); self.join_owner(process)
        candidate = process.candidate
        nonce = gate.approve(process.controller.approval_request(candidate), actor='fixture', ttl_seconds=60)
        entered, leave = threading.Event(), threading.Event(); consume = store.consume
        def held(*args):
            entered.set()
            if not leave.wait(3): raise AssertionError('consume timeout')
            return consume(*args)
        store.consume = held
        try:
            process.handle(dict(schema='xtos.eeg.confirm.v1', candidate_id=candidate.candidate_id, nonce=nonce))
            self.assertTrue(entered.wait(2))
            process.handle(dict(schema='xtos.eeg.confirm.v1', candidate_id=candidate.candidate_id, nonce=nonce))
            self.assertEqual(json.loads(out.read_text().splitlines()[-1])['reason'], 'confirmation busy')
            started = time.monotonic(); process.handle(event(True))
            self.assertLess(time.monotonic()-started, 0.3)
        finally: leave.set(); self.join_owner(process, 'confirmation')
        self.assertNotIn('simulated', out.read_text())
        self.assertTrue((store.path/(nonce+'.spent')).exists())

    def test_publication_rechecks_candidate_after_barrier(self):
        from xtos_action_gate import ActionGate, FixtureStore
        for before in (False, True):
            gate = ActionGate(FixtureStore(Path(self.tmp.name)/('authority'+str(before))))
            with held_peer() as (path, ready, release, received):
                process, out = self.driver(gate, path)
                infer = process.controller.infer
                complete, publish = threading.Event(), threading.Event()
                def delayed(*args, **kwargs):
                    if before:
                        complete.set()
                        if not publish.wait(3): raise AssertionError('reservation timeout')
                    result = infer(*args, **kwargs)
                    if not before:
                        complete.set()
                        if not publish.wait(3): raise AssertionError('publication timeout')
                    return result
                process.controller.infer = delayed
                process.handle(record(True))
                if not before: self.assertTrue(ready.wait(2)); release.set()
                self.assertTrue(complete.wait(2))
                process.handle({} if before else event())
                publish.set(); release.set(); self.join_owner(process)
            rows = [json.loads(line) for line in out.read_text().splitlines()]
            self.assertEqual(rows[-1]['status'], 'rejected')
            self.assertFalse(any('candidate_id' in row for row in rows))

    def test_stale_barrier_preserves_completed_candidate(self):
        from xtos_action_gate import ActionGate, FixtureStore
        gate = ActionGate(FixtureStore(Path(self.tmp.name)/'authority'))
        with held_peer() as (path, ready, release, received):
            process, out = self.driver(gate, path)
            process.handle(record(True)); self.assertTrue(ready.wait(2))
            release.set(); self.join_owner(process)
        candidate, revision = process.candidate, process.revision
        self.assertIsNotNone(candidate)
        process.handle(dict(event(), session_id=1, barrier_id=1))
        self.assertEqual(json.loads(out.read_text().splitlines()[-1])['reason'], 'stale event')
        self.assertIs(process.candidate, candidate)
        self.assertEqual(process.revision, revision)
        process.handle(dict(schema='xtos.eeg.approval-request.v1', candidate_id=candidate.candidate_id))
        self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'approval_request')

    def test_stale_window_preserves_completed_candidate(self):
        from xtos_action_gate import ActionGate, FixtureStore
        for kind in ('older_session', 'older_barrier'):
            with self.subTest(kind=kind):
                gate = ActionGate(FixtureStore(Path(self.tmp.name)/kind))
                with held_peer() as (path, ready, release, received):
                    process, out = self.driver(gate, path)
                    row = dict(record(True), barrier_id=2)
                    process.handle(row); self.assertTrue(ready.wait(2))
                    release.set(); self.join_owner(process)
                candidate, revision = process.candidate, process.revision
                self.assertIsNotNone(candidate)
                stale = dict(row, **({'session_id':1} if kind=='older_session' else {'barrier_id':1}))
                process.handle(stale)
                if process.inference is not None: self.join_owner(process)
                response = json.loads(out.read_text().splitlines()[-1])
                self.assertEqual(response['reason'], 'stale session/barrier')
                self.assertIs(process.candidate, candidate)
                self.assertEqual(process.revision, revision)
                self.assertEqual(len(received), 1, 'stale input must not open another socket')
                process.handle(dict(schema='xtos.eeg.approval-request.v1', candidate_id=candidate.candidate_id))
                self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'approval_request')

    def test_stale_input_preserves_held_inference(self):
        from xtos_action_gate import ActionGate, FixtureStore
        for kind in ('barrier', 'older_session', 'older_barrier'):
            with self.subTest(kind=kind):
                gate = ActionGate(FixtureStore(Path(self.tmp.name)/kind))
                with held_peer() as (path, ready, release, received):
                    process, out = self.driver(gate, path)
                    row = dict(record(True), barrier_id=2)
                    process.handle(row); self.assertTrue(ready.wait(2))
                    revision = process.revision
                    stale = (dict(event(), session_id=1, barrier_id=1) if kind=='barrier'
                             else dict(row, **({'session_id':1} if kind=='older_session' else {'barrier_id':1})))
                    process.handle(stale)
                    self.assertEqual(process.revision, revision)
                    release.set(); self.join_owner(process)
                self.assertIsNotNone(process.candidate)
                self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'candidate')

    def test_old_confirmation_completion_preserves_replacement(self):
        from xtos_action_gate import ActionGate, FixtureStore
        gate = ActionGate(FixtureStore(Path(self.tmp.name)/'authority'))
        callback_done, publish_receipt = threading.Event(), threading.Event()
        with held_peer(connections=2) as (path, ready, release, received):
            process, out = self.driver(gate, path)
            process.handle(record(True)); self.assertTrue(ready.wait(2))
            release.set(); self.join_owner(process)
            first = process.candidate
            nonce = gate.approve(process.controller.approval_request(first), actor='fixture', ttl_seconds=60)
            original = process.controller.confirm
            errors = []
            def delayed(candidate, token):
                receipt = original(candidate, token)
                callback_done.set()
                if not publish_receipt.wait(3):
                    errors.append('receipt handoff timed out')
                    raise ValueError('receipt handoff timed out')
                return receipt
            process.controller.confirm = delayed
            try:
                process.handle(dict(schema='xtos.eeg.confirm.v1', candidate_id=first.candidate_id, nonce=nonce))
                self.assertTrue(callback_done.wait(2))
                # The actual binding hop is one frame, unlike the earlier probe's +2.
                replacement = dict(record(True), window_id=first.prepared.window_id+1,
                                   first_sequence=first.prepared.first_sequence+1,
                                   last_sequence=first.prepared.last_sequence+1)
                process.handle(replacement); self.join_owner(process)
                second = process.candidate
                self.assertIsNotNone(second)
                self.assertIsNot(second, first)
            finally:
                publish_receipt.set()
                if process.confirmation is not None: self.join_owner(process, 'confirmation')
                process.controller.confirm = original
            self.assertEqual(errors, [])
            self.assertEqual(len(received), 2)
            self.assertIs(process.candidate, second)
            process.handle(dict(schema='xtos.eeg.approval-request.v1', candidate_id=second.candidate_id))
            self.assertEqual(json.loads(out.read_text().splitlines()[-1])['status'], 'approval_request')
            self.assertTrue((gate.store.path/(nonce+'.spent')).exists())

    def test_signal_notification_does_not_take_event_lock(self):
        from xtos_eeg_loop.stream import _Unavailable
        process, out = self.driver(_Unavailable(), '/missing')
        # A signal may interrupt another Event operation on the main thread.
        # Holding its condition reproduces that reentrancy boundary deterministically.
        condition = getattr(getattr(process, 'stop', None), '_cond', None)
        if condition is not None: condition.acquire()
        done = threading.Event()
        thread = threading.Thread(target=lambda: (process.terminate(), done.set()))
        thread.start()
        try: self.assertTrue(done.wait(0.1), 'signal notification acquired a non-reentrant Event lock')
        finally:
            if condition is not None: condition.release()
            thread.join(2)

    @unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'), 'locked CPU interpreter not selected')
    def test_real_signed16_cpu_cli(self):
        import hashlib
        from ai_service_trust_fixtures import make_real_signed_service_fixture
        from ai_trust_fixtures import model_claims, signed_envelope
        from xtos_ai_runtime.server import InferenceService, UnixInferenceServer
        from xtos_ai_runtime.client import request
        work = Path(self.tmp.name)
        fx, _ = make_real_signed_service_fixture(work)
        payload = (ROOT/'tools/xtos_eeg_loop/fixtures/mean_rms_transport.onnx').read_bytes()
        (fx.model_root/'model.onnx').write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        claims = model_claims(fx.now, 'model.onnx', payload, name='process16',
                              input={'name':'features_uv','dtype':'float32','shape':[1,16]})
        envelope = signed_envelope('xtos-ai-model-v1', hashlib.sha256(fx.publisher.public_raw).hexdigest(),
                                   claims, fx.publisher, b'XTOS-AI-MODEL-v1\x00')
        (work/'signed-envelope.json').write_bytes(envelope)
        service = InferenceService(fx.model_root, python_command=[os.environ['XTOS_TEST_AI_PYTHON']], trust_policy=fx.policy)
        path = work/'s'; server = UnixInferenceServer(path, service)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        pid = None
        try:
            loaded = request(path, dict(id='load16', op='load', manifest=json.loads(envelope)), timeout=60)
            (work/'load-result.json').write_text(json.dumps(loaded))
            self.assertEqual(loaded['status'], 'ok', loaded)
            pid = request(path, dict(id='health16', op='health'))['models']['process16']['pid']
            raw = dict(configuration(), model='process16', model_sha256=digest,
                       max_receive_age_ns=10_000_000_000, input_name='features_uv')
            self.binding.write_text(json.dumps(raw))
            child = self.child(path); self.send(child, record())
            row = self.read(child, 5)
            self.assertEqual(row['status'], 'transport_verified', row)
            self.assertEqual(len(row['output'][0]), 3)
            self.send(child, event(True)); self.assertEqual(self.read(child)['status'], 'closed')
            self.assertEqual(child.wait(5), 0)
        finally:
            server.shutdown(); thread.join(6)
            self.assertFalse(thread.is_alive()); server.server_close(); service.close()
        self.assertIsNotNone(pid); self.assertFalse(Path('/proc/'+str(pid)).exists())

    def test_unread_full_output_is_bounded(self):
        child = self.child()
        def feed():
            try:
                for _ in range(3000): self.send(child, {})
            except (BrokenPipeError, ValueError): pass
        writer = threading.Thread(target=feed); writer.start()
        try: self.assertNotEqual(child.wait(4), 0)
        finally:
            writer.join(2); self.assertFalse(writer.is_alive())

    def test_entry_eof_closes(self):
        child = self.child(); child.stdin.close()
        self.assertEqual(self.read(child)['status'], 'closed')
        self.assertEqual(child.wait(3), 0)
