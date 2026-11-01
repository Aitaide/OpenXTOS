"""Actual signed CPU child and UDS checks; no GPU performance evidence."""
from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tools/xtos-ai-runtime'))
from xtos_ai_runtime.client import request
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer
from ai_service_trust_fixtures import make_real_signed_service_fixture
from ai_trust_fixtures import model_claims, signed_envelope
from ai_provider_runtime_fixtures import observe_runtime, execution_profile

CPU = 'CPUExecutionProvider'
MODEL = 'fixed-classifier'



@unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'), 'explicit locked AI Python not selected')
class RealProviderServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='p5-uds-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fx, _ = make_real_signed_service_fixture(self.root)
        self.python = os.environ['XTOS_TEST_AI_PYTHON']
        self.runtime = observe_runtime(self.python)
        self.assertTrue(self.runtime['files'])
        self.assertTrue(self.runtime['cpus'])

    def envelope(self, provider=CPU):
        execution = execution_profile(self.runtime, provider)
        claims = model_claims(self.fx.now, 'model.onnx', self.fx.model_bytes,
                              provider=provider, min_runtime_version='1.1.0', execution=execution)
        return json.loads(signed_envelope('xtos-ai-model-v2',
            hashlib.sha256(self.fx.publisher.public_raw).hexdigest(), claims,
            self.fx.publisher, b'XTOS-AI-MODEL-v2\x00'))

    def service(self):
        return InferenceService(self.fx.model_root, python_command=[self.python],
                                trust_policy=self.fx.policy)

    def infer(self, service, identity='numeric'):
        return service.dispatch({'id': identity, 'op': 'infer', 'model': MODEL,
                                 'expected_provider': CPU,
                                 'input': [[2., 0., -1., 0.]], 'deadline_ms': 5000})

    def assert_numeric(self, result):
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['provider'], CPU)
        self.assertLessEqual(abs(result['output'][0][0]-0.8437947034835815), 1e-5)

    def test_v2_signed_cpu_runs_through_real_uds_with_provider_pin(self):
        service = self.service()
        server = UnixInferenceServer(self.root/'s', service)
        owner = threading.Thread(target=server.serve_forever)
        owner.start()
        try:
            loaded = request(self.root/'s', {'id': 'load', 'op': 'load',
                                           'manifest': self.envelope()}, timeout=90)
            self.assertEqual(loaded['status'], 'ok', loaded)
            output = request(self.root/'s', {'id': 'numeric', 'op': 'infer',
                'model': MODEL, 'expected_provider': CPU,
                'expected_model_sha256': hashlib.sha256(self.fx.model_bytes).hexdigest(),
                'input': [[2., 0., -1., 0.]], 'deadline_ms': 5000}, timeout=15)
            self.assert_numeric(output)
            wrong = request(self.root/'s', {'id': 'wrong', 'op': 'infer', 'model': MODEL,
                'expected_provider': 'CUDAExecutionProvider',
                'input': [[2., 0., -1., 0.]], 'deadline_ms': 5000}, timeout=15)
            self.assertEqual(wrong['safe_action'], 'no_action', wrong)
            self.assertEqual(wrong['reason'], 'provider_mismatch', wrong)
            self.assertIsNone(wrong['output'])
        finally:
            server.shutdown()
            owner.join(5)
            self.assertFalse(owner.is_alive())
            server.server_close()
            service.close()

    def test_missing_gpu_reload_refuses_without_replacing_working_cpu(self):
        missing = [p for p in ('CUDAExecutionProvider', 'MIGraphXExecutionProvider')
                   if p not in self.runtime['providers']]
        if not missing:
            self.skipTest('both GPU providers are installed; unavailable-provider case not applicable')
        service = self.service()
        try:
            loaded = service.dispatch({'id': 'cpu', 'op': 'load', 'manifest': self.envelope()})
            self.assertEqual(loaded['status'], 'ok', loaded)
            pid = service._models[MODEL].pid
            for provider in missing:
                result = service.dispatch({'id': provider, 'op': 'reload',
                                           'manifest': self.envelope(provider)})
                self.assertEqual(result['status'], 'unavailable', result)
                self.assertEqual(result['reason'], 'provider_unavailable', result)
                self.assertEqual(result['safe_action'], 'no_action')
                self.assertIsNone(result['output'])
                self.assertEqual(service._models[MODEL].pid, pid)
                self.assertTrue(service._models[MODEL].alive)
            self.assert_numeric(self.infer(service))
        finally:
            service.close()

    def test_actual_killed_child_recovery_is_finite_and_reload_resets(self):
        service = self.service()
        envelope = self.envelope()
        try:
            loaded = service.dispatch({'id': 'load', 'op': 'load', 'manifest': envelope})
            self.assertEqual(loaded['status'], 'ok', loaded)
            for attempt in range(3):
                worker = service._models[MODEL]
                old_pid = worker.pid
                original = worker.infer
                entered = threading.Event()
                def stopped(identity, value, deadline_ms, cancel=None):
                    entered.set()
                    os.kill(old_pid, signal.SIGSTOP)
                    return original(identity, value, min(deadline_ms, 250), cancel)
                with patch.object(worker, 'infer', side_effect=stopped):
                    result = self.infer(service, 'stop-'+str(attempt))
                self.assertTrue(entered.is_set(), result)
                self.assertEqual(result['status'], 'timeout', result)
                self.assertEqual(result['safe_action'], 'no_action')
                with self.assertRaises(ProcessLookupError):
                    os.kill(old_pid, 0)
                state = service.dispatch({'id': 'initial-health', 'op': 'health'})['models'][MODEL]
                until = time.monotonic()+45
                while time.monotonic() < until:
                    state = service.dispatch({'id': 'health', 'op': 'health'})['models'][MODEL]
                    if not state['recovering']:
                        break
                    time.sleep(.02)
                self.assertFalse(state['recovering'], state)
                self.assertEqual(state['recovery_used'], min(attempt+1, 2))
                if attempt < 2:
                    self.assertTrue(state['alive'], state)
                    self.assertNotEqual(state['pid'], old_pid)
                    self.assert_numeric(self.infer(service, 'after-'+str(attempt)))
                else:
                    self.assertFalse(state['alive'], state)
                    self.assertEqual(state['recovery_remaining'], 0)
                    rejected = self.infer(service, 'exhausted')
                    self.assertEqual(rejected['reason'], 'recovery_budget_exhausted', rejected)
                    self.assertEqual(rejected['safe_action'], 'no_action')
            reloaded = service.dispatch({'id': 'reload', 'op': 'reload', 'manifest': envelope})
            self.assertEqual(reloaded['status'], 'ok', reloaded)
            state = service.dispatch({'id': 'reset', 'op': 'health'})['models'][MODEL]
            self.assertEqual(state['recovery_used'], 0)
            self.assertEqual(state['recovery_remaining'], 2)
            self.assert_numeric(self.infer(service, 'reset-infer'))
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
