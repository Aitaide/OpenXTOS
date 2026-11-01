"""Real AF_UNIX peer framing; GPU response fixtures are not GPU execution."""
from pathlib import Path
import json
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-runtime'))
from xtos_ai_runtime.client import request

CPU = 'CPUExecutionProvider'
GPU = 'CUDAExecutionProvider'
AMD = 'MIGraphXExecutionProvider'


class ProviderClientTests(unittest.TestCase):
    def exchange(self, provider, document, **response_fields):
        with tempfile.TemporaryDirectory(prefix='pv-') as directory:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.settimeout(2)
            path = Path(directory) / 's'
            listener.bind(str(path))
            listener.listen(1)
            errors = []
            response = {'schema': 'xtos-ai-runtime-v1', 'id': 'p', 'status': 'success',
                        'safe_action': 'action_allowed', 'reason': None, 'model': 'model',
                        'provider': provider, 'latency_ms': 1.0, 'output': [[0.5]]}
            response.update(response_fields)
            def peer():
                try:
                    with listener.accept()[0] as conn:
                        conn.settimeout(2)
                        data = bytearray()
                        while b'\n' not in data:
                            part = conn.recv(4096)
                            if not part:
                                raise RuntimeError('request EOF')
                            data.extend(part)
                        conn.sendall((json.dumps(response) + '\n').encode())
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=peer)
            thread.start()
            try:
                return request(path, document, timeout=1)
            finally:
                thread.join(3)
                listener.close()
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_explicit_provider_pin_accepts_matching_success(self):
        for provider in (CPU, GPU, AMD):
            with self.subTest(provider=provider):
                result = self.exchange(provider, {'id': 'p', 'op': 'infer', 'model': 'model',
                                                   'expected_provider': provider})
                self.assertEqual(result['provider'], provider)
                self.assertEqual(result['output'], [[0.5]])

    def test_response_cannot_switch_provider_or_implicitly_enable_gpu(self):
        documents = ({'id': 'p', 'op': 'infer', 'model': 'model'},
                     {'id': 'p', 'op': 'infer', 'model': 'model', 'expected_provider': CPU},
                     {'id': 'p', 'op': 'infer', 'model': 'model', 'expected_provider': AMD})
        for document in documents:
            with self.subTest(document=document), self.assertRaisesRegex(ValueError, 'provider identity mismatch'):
                self.exchange(GPU, document)
        with self.assertRaisesRegex(ValueError, 'provider identity mismatch'):
            self.exchange(CPU, {'id': 'p', 'op': 'infer', 'model': 'model', 'expected_provider': GPU})

    def test_invalid_pin_refuses_before_socket_creation(self):
        for provider in (None, True, 1, '', [], {}, 'ROCMExecutionProvider', 'unknown'):
            with self.subTest(provider=provider), patch('xtos_ai_runtime.client.socket.socket') as create:
                with self.assertRaisesRegex(ValueError, 'invalid expected provider'):
                    request('/unused', {'id': 'p', 'op': 'infer', 'expected_provider': provider})
                create.assert_not_called()
        for op in ('list', 'load', 'health', 'cancel'):
            with self.subTest(op=op), patch('xtos_ai_runtime.client.socket.socket') as create:
                with self.assertRaisesRegex(ValueError, 'invalid expected provider'):
                    request('/unused', {'id': 'p', 'op': op, 'expected_provider': GPU})
                create.assert_not_called()

    def test_no_action_preserves_optional_provider_identity(self):
        document = {'id': 'p', 'op': 'infer', 'model': 'model', 'expected_provider': GPU}
        fields = {'status': 'unavailable', 'safe_action': 'no_action', 'reason': 'provider_unavailable', 'output': None}
        for provider in (None, GPU):
            with self.subTest(provider=provider):
                self.assertEqual(self.exchange(provider, document, **fields)['safe_action'], 'no_action')
        with self.assertRaisesRegex(ValueError, 'provider identity mismatch'):
            self.exchange(CPU, document, **fields)


if __name__ == '__main__':
    unittest.main(verbosity=2)
