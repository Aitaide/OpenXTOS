"""Dedicated 16-input arithmetic transport model; no EEG task accuracy claim."""
from pathlib import Path
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'tools/xtos_eeg_loop/fixtures/mean_rms_transport.onnx'
DIGEST = '1437840f5685a55e43bbd4be10153e3a660f0c7f84b92db12fe9a105422ccc62'


class TransportFixtureTests(unittest.TestCase):
    def test_versioned_model_has_pinned_exact_bytes(self):
        self.assertEqual(hashlib.sha256(MODEL.read_bytes()).hexdigest(), DIGEST)
        self.assertLess(MODEL.stat().st_size, 4096)

    def test_generator_reproduces_the_fixture(self):
        runtime = os.environ.get('XTOS_TEST_AI_PYTHON')
        if not runtime:
            self.skipTest('locked AI runtime not selected')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            output = Path(directory) / 'model.onnx'
            result = subprocess.run([runtime, '-B', str(ROOT / 'tools/xtos_eeg_loop/make_transport_fixture.py'),
                                     '--output', str(output)], capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertEqual(output.read_bytes(), MODEL.read_bytes())
            self.assertEqual(json.loads(result.stdout)['sha256'], DIGEST)
            second = subprocess.run([runtime, '-B', str(ROOT / 'tools/xtos_eeg_loop/make_transport_fixture.py'),
                                     '--output', str(output)], capture_output=True, text=True, timeout=45)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), DIGEST)

    def test_signed_cpu_worker_matches_independent_oracle(self):
        # Post-implementation integration regression; runtime implementation exists.
        runtime = os.environ.get('XTOS_TEST_AI_PYTHON')
        if not runtime:
            self.skipTest('locked AI runtime not selected')
        sys.path[:0] = [str(ROOT / 'tests'), str(ROOT / 'tools/xtos-ai-runtime')]
        from ai_service_trust_fixtures import make_real_signed_service_fixture
        from ai_trust_fixtures import model_claims, signed_envelope
        from xtos_ai_runtime.worker import Worker
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            fx, _ = make_real_signed_service_fixture(Path(directory))
            payload = MODEL.read_bytes()
            (fx.model_root / 'model.onnx').write_bytes(payload)
            claims = model_claims(fx.now, 'model.onnx', payload, name='mean-rms-transport-fixture',
                                  input={'name': 'features_uv', 'dtype': 'float32', 'shape': [1, 16]})
            envelope = signed_envelope('xtos-ai-model-v1', hashlib.sha256(fx.publisher.public_raw).hexdigest(),
                                       claims, fx.publisher, b'XTOS-AI-MODEL-v1\x00')
            ticket = fx.policy.authorize(envelope, fx.model_root)
            self.assertEqual(ticket.manifest.sha256, DIGEST)
            self.assertEqual(ticket.manifest.input_shape, (1, 16))
            worker = Worker(ticket, python_command=[runtime], load_timeout=60)
            values = [float(i+1) for i in range(8)] * 2
            weights = [[((i+1)%5-2)/256, ((i+2)%7-3)/256, ((i+3)%11-5)/256] for i in range(16)]
            logits = [sum(values[i]*weights[i][c] for i in range(16)) + [0.125, -0.125, 0][c]
                      for c in range(3)]
            exps = [math.exp(value-max(logits)) for value in logits]
            expected = [value/sum(exps) for value in exps]
            try:
                health = worker.start()
                self.assertEqual(health['provider'], 'CPUExecutionProvider')
                self.assertEqual(health['model_sha256'], DIGEST)
                response = worker.infer('eeg-transport-worker', [values], 5000)
                self.assertEqual(response['status'], 'success', response)
                self.assertEqual(response['provider'], 'CPUExecutionProvider')
                for actual, golden in zip(response['output'][0], expected):
                    self.assertAlmostEqual(actual, golden, delta=2e-7)
                self.assertEqual(worker.diagnostics(), '')
            finally:
                worker.close()
            self.assertFalse(worker.alive)
            self.assertIsNotNone(worker.process.poll())


if __name__ == '__main__':
    unittest.main(verbosity=2)
