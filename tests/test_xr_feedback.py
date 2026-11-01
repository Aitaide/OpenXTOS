"""Real private authority store and deterministic consume/barrier interleaving."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools/xtos-xr'), str(ROOT / 'tools')]
import xtos_xr as xr
from xtos_action_gate import ActionGate, FixtureStore, GateError
from test_xr_multimodal import Clock, sample, fixture_convert

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.build')
        self.addCleanup(self.tmp.cleanup)
        self.store = FixtureStore(Path(self.tmp.name) / 'store')
        self.gate = ActionGate(self.store, clock=lambda: 1000)
        self.clock = Clock()
        self.session = xr.MultimodalSession(clock=self.clock,gate=self.gate,converter=fixture_convert,
            max_age_ns=100, max_skew_ns=10, max_uncertainty_ns=5, candidate_ttl_ns=50)
        self.addCleanup(self.session.close)
        for m in ('eeg', 'eye_gaze', 'emg'): self.session.ingest(sample(m))
        self.candidate = self.session.join()
    def approve(self):
        return self.gate.approve(self.session.approval_request(self.candidate),
                                 actor='fixture-only', ttl_seconds=10)
    def test_exact_candidate_real_nonce_once(self):
        self.assertTrue(hasattr(self.session, 'approval_request'), 'approval API missing')
        with self.assertRaises(xr.XRError): self.session.approval_request(replace(self.candidate))
        nonce = self.approve()
        receipt = self.session.confirm(self.candidate, nonce)
        self.assertEqual(receipt.runtime_mode, 'simulated')
        self.assertEqual(receipt.safe_action, 'no_action')
        self.assertIs(receipt.physical_acceptance, False)
        self.assertTrue((self.store.path / (nonce + '.spent')).is_file())
        with self.assertRaises((xr.XRError, GateError)): self.session.confirm(self.candidate, nonce)
    def test_barrier_completes_while_real_consume_held(self):
        self.assertTrue(hasattr(self.session, 'approval_request'), 'approval API missing')
        nonce = self.approve()
        entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
        consume = self.store.consume
        def held(directory, token):
            raw = consume(directory, token)
            entered.set()
            if not release.wait(3): raise RuntimeError('fixture release timeout')
            return raw
        self.store.consume = held
        result, errors = [], []
        def confirm():
            try: result.append(self.session.confirm(self.candidate, nonce))
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=confirm)
        revoker = threading.Thread(target=lambda: (self.session.barrier('runtime lost'), revoked.set()))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            revoker.start()
            self.assertTrue(revoked.wait(1), 'barrier blocked behind authority I/O')
        finally:
            release.set()
            worker.join(3)
            if revoker.ident is not None: revoker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(result, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], xr.XRError)
        self.assertTrue((self.store.path / (nonce + '.spent')).is_file())

    def test_demo_cli(self):
        import json, os, subprocess
        flags = ['-O'] if sys.flags.optimize else []
        env = dict(os.environ, PYTHONPATH=str(ROOT/'tools/xtos-xr') + ':' + str(ROOT/'tools'))
        run = subprocess.run([sys.executable, *flags, '-m', 'xtos_xr', '--demo'],
                             env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result['runtime_mode'], 'simulated')
        self.assertEqual(result['safe_action'], 'no_action')
        self.assertIs(result['physical_acceptance'], False)

    def test_maximal_samples_bind_approval(self):
        self.session = xr.MultimodalSession(clock=self.clock,gate=self.gate,converter=fixture_convert)
        self.addCleanup(self.session.close)
        for m in ('eeg', 'eye_gaze', 'emg'):
            self.session.ingest(sample(m, sequence=2, source_id='x'*128,
                values=[-3.4028234663852886e38]*32))
        self.candidate = self.session.join()
        nonce = self.approve()
        self.assertEqual(self.session.confirm(self.candidate, nonce).safe_action, 'no_action')

    def test_changed_request_burns_real_nonce(self):
        req = self.session.approval_request(self.candidate)
        req['argv'].append('changed')
        nonce = self.gate.approve(req, actor='fixture', ttl_seconds=10)
        with self.assertRaises(GateError): self.session.confirm(self.candidate, nonce)
        self.assertTrue((self.store.path/(nonce+'.spent')).exists())
        with self.assertRaises(xr.XRError): self.session.approval_request(self.candidate)

    def test_expiry_and_clock_change_after_consume(self):
        for new_time in (1051, 999):
            with self.subTest(now=new_time):
                self.setUp()
                nonce = self.approve()
                consume = self.store.consume
                def delayed(directory, token):
                    raw = consume(directory, token)
                    self.clock.now = new_time
                    return raw
                self.store.consume = delayed
                with self.assertRaises(xr.XRError): self.session.confirm(self.candidate, nonce)
                self.assertTrue((self.store.path/(nonce+'.spent')).is_file())

    def test_close_idempotent_keeps_callers_store(self):
        self.session.close()
        self.session.close()
        self.assertTrue((self.store.path/'.key').is_file())
        with self.assertRaises(xr.XRError): self.session.approval_request(self.candidate)

    def test_spent_nonce_cannot_confirm_replacement(self):
        nonce = self.approve()
        self.session.confirm(self.candidate, nonce)
        for m in ('eeg', 'eye_gaze', 'emg'):
            self.session.ingest(sample(m, sequence=2))
        replacement = self.session.join()
        with self.assertRaises(GateError): self.session.confirm(replacement, nonce)
        self.assertTrue((self.store.path / (nonce + '.spent')).is_file())

    def test_confirmation_deadline_is_inclusive(self):
        nonce = self.approve()
        self.clock.now = self.candidate.expires_ns
        self.assertEqual(self.session.confirm(self.candidate, nonce).confirmed_ns, 1050)

    def test_maximum_converted_candidate_fits_gate_binding(self):
        self.session = xr.MultimodalSession(clock=self.clock, gate=self.gate,
            converter=lambda *args: dict(converted_host_ns=990, uncertainty_ns=0,
                                         conversion_id='c'*128))
        self.addCleanup(self.session.close)
        for m in ('eeg', 'eye_gaze', 'emg'):
            self.session.ingest(sample(m, source_id='s'*128, clock_domain='openxr_time',
                values=[-3.4028234663852886e38]*32))
        self.candidate = self.session.join()
        nonce = self.approve()
        self.assertEqual(self.session.confirm(self.candidate, nonce).safe_action, 'no_action')

if __name__ == '__main__': unittest.main()
