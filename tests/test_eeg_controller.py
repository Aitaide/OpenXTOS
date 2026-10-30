"""Controller software fixtures; SERDEV labels do not prove physical provenance."""
import dataclasses
import importlib
import os
import hashlib
import json
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tools/xtos-ai-runtime')]
from test_eeg_feature_binding import configuration, window
from xtos_action_gate import ActionGate, FixtureStore
from xtos_eeg_loop.simulation import SimulationFeedback


@contextmanager
def held_proxy(directory, upstream):
    path = Path(directory)/'p'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path)); listener.listen(1); listener.settimeout(5)
    ready, release = threading.Event(), threading.Event()
    errors = []
    stop = threading.Event()
    workers = []
    def forward(connection, hold):
        try:
            with connection, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as target:
                target.settimeout(8); target.connect(str(upstream))
                with connection.makefile('rb') as incoming:
                    target.sendall(incoming.readline(65537))
                with target.makefile('rb') as outgoing:
                    response = outgoing.readline(65537)
                if json.loads(response)['status'] != 'success':
                    raise AssertionError(response)
                if hold:
                    ready.set()
                    if not release.wait(5):
                        raise AssertionError('proxy release timeout')
                connection.sendall(response)
        except BaseException as exc:
            errors.append(exc); ready.set()
    def serve():
        listener.settimeout(0.1)
        try:
            while not stop.is_set():
                try: connection, _ = listener.accept()
                except socket.timeout: continue
                worker = threading.Thread(target=forward,args=(connection,not workers))
                workers.append(worker); worker.start()
        finally:
            listener.close()
    thread = threading.Thread(target=serve); thread.start()
    try:
        yield path, ready, release, errors
    finally:
        release.set(); stop.set(); thread.join(2)
        for worker in workers:
            worker.join(10)
            if worker.is_alive(): raise AssertionError('proxy worker did not join')
        if thread.is_alive():
            raise AssertionError('proxy did not join')
        if errors:
            raise AssertionError(repr(errors))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module('xtos_eeg_loop.controller')
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.gate = ActionGate(FixtureStore(Path(self.tmp.name)/'authority'), clock=lambda: 1000)
        self.now = 200
        self.sim = SimulationFeedback(self.gate, now_ns=lambda: self.now)

    def owner(self, path='/missing', raw=None):
        return self.api.StreamController(configuration() if raw is None else raw, path,
                                         self.sim, now_ns=lambda: self.now)

    def test_pinned_transport_and_harmless_confirmation(self):
        from test_eeg_cpu_transport import peer, success
        raw = dict(configuration(), max_receive_age_ns=5_000_000_000)
        record = dict(window(), max_receive_age_ns=5_000_000_000,
                      source_flags=1, source_kind='serdev')
        with peer(success()) as (path, received):
            owner = self.owner(str(path), raw)
            result = owner.infer(record, 'transport-1', 3000)
        self.assertEqual(result.status, 'candidate', result)
        self.assertEqual(result.safe_action, 'no_action')
        self.assertEqual(result.candidate.provenance, 'unverified')
        request = owner.approval_request(result.candidate)
        nonce = self.gate.approve(request, actor='software-fixture', ttl_seconds=60)
        self.assertEqual(owner.confirm(result.candidate, nonce).status, 'simulated')
        with self.assertRaises(ValueError):
            owner.confirm(result.candidate, nonce)
        self.assertEqual(received[0]['expected_model_sha256'], raw['model_sha256'])

    def test_events_exact_schema_and_terminal_summary(self):
        owner = self.owner()
        barrier = dict(schema='xtos.eeg.barrier.v1', session_id=2, barrier_id=2,
                       reason='stop', safe_action='no_action', physical_acceptance=False)
        self.assertEqual(owner.handle(barrier).status, 'invalidated')
        self.assertEqual(owner.handle(barrier).status, 'invalidated')
        self.assertEqual(owner.handle(dict(barrier, barrier_id=1)).reason, 'stale event')
        self.assertEqual(owner.handle(dict(barrier, physical_acceptance=0)).status, 'rejected')
        summary = dict(schema='xtos.eeg.stream-summary.v1', session_id=2, barrier_id=2,
                       status='complete', reason='requested_windows', windows_emitted=1,
                       safe_action='no_action', physical_acceptance=False)
        self.assertEqual(owner.handle(summary).status, 'closed')
        self.assertEqual(owner.infer(window(), 'transport-1', 3000).reason, 'closed')

    @unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'), 'locked CPU interpreter not selected')
    def test_signed16_cpu_native_records_and_concurrent_invalidation(self):
        from ai_service_trust_fixtures import make_real_signed_service_fixture
        from ai_trust_fixtures import model_claims, signed_envelope
        from xtos_ai_runtime.server import InferenceService, UnixInferenceServer
        from xtos_ai_runtime.client import request
        from xtos_ai_runtime.trust import ProductionTrustPolicy, NativeEd25519Verifier
        work = Path(self.tmp.name)
        tool = ROOT/'tools/xtos-eeg-stream'; binary = work/'native'
        command = ['cc','-std=c11','-O2','-Wall','-Wextra','-Werror',
                   '-I'+str(tool), '-I'+str(ROOT/'sdk/libxtos-bci/include'),
                   '-I'+str(ROOT/'sdk/libxtos-signal/include')]
        command += [str(tool/p) for p in ('main.c','eeg_export.c','eeg_stream.c','tests/test_eeg_export.c')]
        command += [str(ROOT/'sdk/libxtos-bci/src/session.c'), str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),
                    '-lm','-Wl,--wrap=open','-Wl,--wrap=close','-Wl,--wrap=ioctl','-o',str(binary)]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        fx, _ = make_real_signed_service_fixture(work)
        self.assertIsInstance(fx.policy, ProductionTrustPolicy)
        self.assertIsInstance(fx.policy.verifier, NativeEd25519Verifier)
        payload = (ROOT/'tools/xtos_eeg_loop/fixtures/mean_rms_transport.onnx').read_bytes()
        (fx.model_root/'model.onnx').write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        claims = model_claims(fx.now,'model.onnx',payload,name='controller16',
                              input={'name':'features_uv','dtype':'float32','shape':[1,16]})
        envelope = signed_envelope('xtos-ai-model-v1',hashlib.sha256(fx.publisher.public_raw).hexdigest(),
                                   claims,fx.publisher,b'XTOS-AI-MODEL-v1\x00')
        service = InferenceService(fx.model_root, python_command=[os.environ['XTOS_TEST_AI_PYTHON']],trust_policy=fx.policy)
        path = work/'s'; server = UnixInferenceServer(path, service)
        server_thread = threading.Thread(target=server.serve_forever); server_thread.start()
        pid = None
        try:
            loaded = request(path,dict(id='load16',op='load',manifest=json.loads(envelope)),timeout=60)
            self.assertEqual(loaded['status'],'ok',loaded)
            pid = request(path,dict(id='health16',op='health'))['models']['controller16']['pid']
            raw = dict(configuration(),model='controller16',model_sha256=digest,window_frames=4,
                       hop_frames=2,max_receive_age_ns=10_000_000_000,input_name='features_uv')
            def native_rows():
                c = [str(binary),'--device','/dev/xtos-export-fixture','--session-id','9',
                     '--window','4','--hop','2','--max-age-ns','10000000000',
                     '--duration-ms','100','--max-windows','2']
                run = subprocess.run(c,env=dict(os.environ,EXPORT_SCENARIO='complete'),capture_output=True,text=True,timeout=5)
                self.assertEqual(run.returncode,0,run.stderr)
                rows = [json.loads(line) for line in run.stdout.splitlines()]
                self.assertEqual(rows[-1]['status'],'complete')
                return rows
            def controller(sock, config=raw, clock=time.monotonic_ns):
                sim = SimulationFeedback(self.gate)
                return self.api.StreamController(config,str(sock),sim,now_ns=clock)
            rows = native_rows(); owner = controller(path)
            consumed = []
            for row in rows:
                result = owner.infer(row,'native16',3000) if row['schema']=='xtos.eeg.window.v1' else owner.handle(row)
                consumed.append(row['schema'])
                self.assertEqual(result.safe_action,'no_action')
                self.assertIsNone(result.candidate)
                if row['schema']=='xtos.eeg.window.v1':
                    self.assertEqual(result.status,'transport_verified', result)
            self.assertEqual(len(consumed),len(rows))
            self.assertEqual(owner.infer(rows[0],'late',3000).reason,'closed')
            # 3000 ms is within the reviewed API bound and tolerates QEMU TCG variation.
            for event in ('barrier','eof','summary','busy','replace','exception','malformed'):
                with self.subTest(event=event):
                    rows = native_rows(); record = next(r for r in rows if r['schema']=='xtos.eeg.window.v1')
                    # Explicit software SERDEV fixture label, never physical evidence.
                    record = dict(record,source_flags=1,source_kind='serdev')
                    proxy_dir = work/event; proxy_dir.mkdir()
                    with held_proxy(proxy_dir,path) as (proxy, ready, release, errors):
                        def clock():
                            if event=='exception' and threading.current_thread().name=='old-inference' and release.is_set():
                                raise RuntimeError('trusted clock fixture failure')
                            return time.monotonic_ns()
                        owner = controller(proxy,clock=clock); results=[]; newer=None
                        thread = threading.Thread(name='old-inference',target=lambda: results.append(owner.infer(record,'held16',3000)))
                        thread.start()
                        try:
                            self.assertTrue(ready.wait(6)); self.assertEqual(errors,[])
                            if event=='busy':
                                self.assertEqual(owner.infer(record,'busy16',3000).reason,'busy')
                                stale = dict(schema='xtos.eeg.barrier.v1',session_id=8,barrier_id=1,
                                             reason='delayed',safe_action='no_action',physical_acceptance=False)
                                self.assertEqual(owner.handle(stale).reason,'stale event')
                            elif event in ('replace','exception'):
                                replacement = dict(record,barrier_id=record['barrier_id']+1,window_id=record['window_id']+100)
                                newer = owner.infer(replacement,'replacement16',3000)
                                self.assertEqual(newer.status,'candidate',newer)
                            elif event=='eof': owner.eof()
                            elif event=='malformed': owner.handle(dict(record, values=[1.0]*17))
                            else:
                                terminal = next(r for r in reversed(rows) if r['schema']==
                                                ('xtos.eeg.barrier.v1' if event=='barrier' else 'xtos.eeg.stream-summary.v1'))
                                owner.handle(terminal)
                        finally:
                            release.set(); thread.join(8)
                        self.assertFalse(thread.is_alive()); self.assertEqual(len(results),1)
                        result=results[0]; self.assertEqual(result.safe_action,'no_action')
                        if event=='busy':
                            self.assertEqual(result.status,'candidate',result)
                            candidate=result.candidate
                            tokens=[self.gate.approve(owner.approval_request(candidate),actor='software-fixture',ttl_seconds=60) for _ in range(2)]
                            receipts=[]; denied=[]
                            def confirm(token):
                                try: receipts.append(owner.confirm(candidate,token))
                                except ValueError as exc: denied.append(exc)
                            threads=[threading.Thread(target=confirm,args=(token,)) for token in tokens]
                            for t in threads:t.start()
                            for t in threads:t.join(3); self.assertFalse(t.is_alive())
                            self.assertEqual(len(receipts),1); self.assertEqual(len(denied),1)
                        else:
                            self.assertEqual(result.status,'rejected',result)
                            self.assertIsNone(result.candidate); self.assertIsNone(result.output)
                        if newer is not None:
                            owner.approval_request(newer.candidate)
                        for row in rows:
                            if row['schema']!='xtos.eeg.window.v1':owner.handle(row)
                        owner.eof()
            bad=controller(path,dict(raw,model_sha256='0'*64))
            record=next(r for r in native_rows() if r['schema']=='xtos.eeg.window.v1')
            self.assertEqual(bad.infer(record,'wrong16',3000).status,'rejected')
        finally:
            server.shutdown(); server_thread.join(6)
            self.assertFalse(server_thread.is_alive()); server.server_close(); service.close()
        self.assertIsNotNone(pid); self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_snapshot_mutation_and_confirmation_expiry(self):
        import inspect
        from test_eeg_cpu_transport import peer, success
        raw = dict(configuration(),max_receive_age_ns=5_000_000_000)
        # Mutate on the real thread immediately before/after tuple copying.
        for before in (True,False):
            with self.subTest(before=before):
                owner=self.owner(raw=raw)
                record=dict(window(),max_receive_age_ns=5_000_000_000,source_flags=1,source_kind='serdev')
                copied,mutated=threading.Event(),threading.Event()
                lines,first=inspect.getsourcelines(self.api.StreamController._window_copy)
                needle='snapshot = tuple' if before else 'if len(snapshot)'
                line=next(first+i for i,text in enumerate(lines) if needle in text)
                def mutate():
                    if copied.wait(2): record['values'][:]=[1.0]*17
                    mutated.set()
                def trace(frame,event,arg):
                    if event=='line' and frame.f_code is self.api.StreamController._window_copy.__code__ and frame.f_lineno==line:
                        copied.set()
                        if not mutated.wait(2):raise AssertionError('mutator timeout')
                    return trace
                thread=threading.Thread(target=mutate); thread.start(); old=sys.gettrace()
                try:
                    sys.settrace(trace)
                    if before:
                        result=owner.infer(record,'transport-1',3000)
                        self.assertEqual(result.status,'rejected')
                    else:
                        with peer(success()) as (path,received):
                            owner=self.owner(str(path),raw)
                            self.assertEqual(owner.handle(record).status,'use_infer')
                            # handle takes a snapshot too: restore only caller data.
                            record['values']=window()['values']
                            result=owner.infer(record,'transport-1',3000,candidate_ttl_ms=1)
                        self.assertEqual(result.status,'candidate',result)
                        candidate=result.candidate
                        self.assertEqual(len(candidate.prepared.values),16)
                        nonce=self.gate.approve(owner.approval_request(candidate),actor='software-fixture',ttl_seconds=60)
                        self.now=candidate.expires_at_ns
                        with self.assertRaises(ValueError):owner.confirm(candidate,nonce)
                        self.now=200
                finally:
                    sys.settrace(old); copied.set(); thread.join(3)
                self.assertFalse(thread.is_alive())

    def test_gate_storage_wait_does_not_block_barrier(self):
        from test_eeg_cpu_transport import peer,success
        raw=dict(configuration(),max_receive_age_ns=5_000_000_000)
        record=dict(window(),max_receive_age_ns=5_000_000_000,source_flags=1,source_kind='serdev')
        with peer(success()) as (path,received):
            owner=self.owner(str(path),raw); candidate=owner.infer(record,'transport-1',3000).candidate
        token=self.gate.approve(owner.approval_request(candidate),actor='software-fixture',ttl_seconds=60)
        entered,release=threading.Event(),threading.Event(); errors=[]; results=[]
        original=self.gate.store.consume
        def consume(*args):
            value=original(*args); entered.set()
            if not release.wait(2):raise AssertionError('storage release timeout')
            return value
        self.gate.store.consume=consume
        def confirm():
            try:results.append(owner.confirm(candidate,token))
            except ValueError as exc:errors.append(exc)
        thread=threading.Thread(target=confirm); thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(owner.handle(dict(schema='xtos.eeg.barrier.v1',session_id=2,barrier_id=2,
                reason='stop',safe_action='no_action',physical_acceptance=False)).status,'invalidated')
        finally:
            release.set(); thread.join(3); self.gate.store.consume=original
        self.assertFalse(thread.is_alive()); self.assertEqual(results,[]); self.assertEqual(len(errors),1)
        self.assertTrue((self.gate.store.path/(token+'.spent')).exists())

    def test_eof_terminal_owned_result(self):
        owner = self.owner()
        result = owner.eof()
        self.assertEqual(result.status, 'closed')
        self.assertEqual(result.safe_action, 'no_action')
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.status = 'allowed'
        self.assertEqual(owner.infer(window(), 'transport-1', 3000).status, 'rejected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
