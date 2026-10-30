"""Digest binding at the real scheduler/UDS boundary; no AI accuracy claim."""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-runtime'))
from ai_service_trust_fixtures import FixtureTrustPolicy
from xtos_ai_runtime.client import request
from xtos_ai_runtime.server import InferenceService, UnixInferenceServer, SCHEMA

DIGEST = '6e6226cf772934a3f7a398ecb55ffd290503497d91803ff499a7aa28bf90a113'
OTHER = 'a' * 64
INVALID = (None, True, 3, [], {}, '', 'a' * 63, 'a' * 65, 'A' * 64, ' ' + 'a' * 64, 'g' * 64)


class BindingWorker:
    """Test-only authorized scheduler worker, not a runtime surrogate claim."""
    def __init__(self, ticket):
        self.ticket = ticket
        self.calls = 0
        self.alive = True
        self.pid = os.getpid()
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.on_result = None
        self.result = None

    def start(self, cancel=None):
        return {'runtime_version': 'fixture'}

    def infer(self, request_id, value, deadline_ms, cancel=None):
        self.calls += 1
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError('test release was not signalled')
        self.result = {'schema': SCHEMA, 'id': request_id, 'status': 'success',
                       'safe_action': 'action_allowed', 'reason': None,
                       'model': self.ticket.manifest.name, 'provider': 'CPUExecutionProvider',
                       'latency_ms': 0, 'output': [[1.0]]}
        if self.on_result:
            self.on_result()
        return self.result

    def close(self):
        self.release.set()
        self.alive = False


class ModelBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='xtos-binding-')
        self.addCleanup(self.directory.cleanup)
        self.policy = FixtureTrustPolicy()
        self.workers = []
        def factory(ticket):
            worker = BindingWorker(ticket)
            self.workers.append(worker)
            return worker
        self.model_root = Path(self.directory.name)
        model_bytes = (ROOT / 'tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx').read_bytes()
        (self.model_root / 'model.onnx').write_bytes(model_bytes)
        self.service = InferenceService(self.model_root, trust_policy=self.policy, worker_factory=factory)
        self.addCleanup(self.service.close)
        self.manifest = json.loads((ROOT / 'tools/xtos-ai-runtime/model-manifest.example.json').read_text())
        self.manifest['model_path'] = 'model.onnx'
        loaded = self.service.dispatch({'id': 'load', 'op': 'load', 'manifest': self.manifest})
        self.assertEqual(loaded['status'], 'ok', loaded)
        self.worker = self.workers[0]
        self.path = Path(self.directory.name) / 'service.sock'
        self.server = UnixInferenceServer(self.path, self.service)
        self.server_errors = []
        def serve():
            try:
                self.server.serve_forever()
            except BaseException as exc:
                self.server_errors.append(exc)
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        for worker in self.workers:
            worker.release.set()
        self.server.shutdown()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.server_errors, [])
        self.server.server_close()
        self.assertFalse(self.path.exists())

    def document(self, **fields):
        return dict({'id': 'infer', 'op': 'infer', 'model': 'fixed-classifier',
                     'input': [[2., 0., -1., 0.]], 'deadline_ms': 5000}, **fields)

    def raw_request(self, document):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(str(self.path))
            conn.sendall(json.dumps(document).encode() + b'\n')
            with conn.makefile('rb') as stream:
                return json.loads(stream.readline())

    def assert_idle(self):
        with self.service._condition:
            self.assertEqual(self.service._inflight, {})
            self.assertEqual(self.service._inflight_tickets, {})
            self.assertEqual(self.service._request_denials, {})
            self.assertEqual(self.service._worker_users, {})
            self.assertIsNone(self.service._active_id)
            self.assertIsNone(self.service._queued_id)

    def test_wrong_digest_refused_before_worker_entry_and_cleans_ownership(self):
        result = self.raw_request(self.document(expected_model_sha256=OTHER))
        self.assertEqual((result['status'], result['reason']),
                         ('unavailable', 'model_digest_mismatch'))
        self.assertEqual((result['safe_action'], result['output']), ('no_action', None))
        self.assertEqual(self.worker.calls, 0)
        self.assert_idle()
        self.assertEqual(request(self.path, self.document())['status'], 'success')


    def test_server_validates_pin_before_registering_inference(self):
        registrations = []
        class ObservedInflight(dict):
            def __setitem__(self, key, value):
                registrations.append(key)
                super().__setitem__(key, value)
        self.service._inflight = ObservedInflight()
        for digest in INVALID:
            with self.subTest(digest=digest):
                result = self.raw_request(self.document(expected_model_sha256=digest))
                self.assertEqual((result['status'], result['reason']), ('invalid', 'invalid_model_digest'))
        for model in (None, '', True, 1, [], {}):
            with self.subTest(model=model):
                result = self.raw_request(self.document(model=model, expected_model_sha256=DIGEST))
                self.assertEqual((result['status'], result['reason']), ('invalid', 'invalid_request'))
        self.assertEqual(registrations, [])
        self.assertEqual(self.worker.calls, 0)
        self.assert_idle()


    def test_pinned_success_copies_selected_digest_and_preserves_legacy_response(self):
        result = request(self.path, self.document(expected_model_sha256=DIGEST))
        self.assertEqual(result.get('model_sha256'), DIGEST)
        self.assertNotIn('model_sha256', self.worker.result)
        legacy = request(self.path, self.document(id='legacy'))
        self.assertEqual(legacy, self.worker.result)
        self.assertNotIn('model_sha256', legacy)
        self.assert_idle()


    def test_non_infer_operations_reject_pin(self):
        for op in ('list', 'health', 'load', 'reload', 'unload', 'cancel', 'unknown'):
            with self.subTest(op=op):
                result = self.raw_request(self.document(op=op, expected_model_sha256=DIGEST))
                self.assertEqual((result['status'], result['reason']), ('invalid', 'invalid_request'))
        self.assertEqual(self.worker.calls, 0)
        self.assert_idle()


    def test_queued_same_name_reload_checks_promoted_worker_ticket(self):
        self.worker.release.clear()
        queued_waiting = threading.Event()
        original_wait = self.service._condition.wait
        def observe_wait(timeout=None):
            if self.service._queued_id == 'queued' and self.service._active_id == 'active':
                queued_waiting.set()
            return original_wait(timeout)
        results, errors = {}, []
        def infer(identity):
            try:
                results[identity] = request(self.path, self.document(
                    id=identity, expected_model_sha256=DIGEST), timeout=3)
            except BaseException as exc:
                errors.append(exc)
        active = threading.Thread(target=infer, args=('active',))
        queued = threading.Thread(target=infer, args=('queued',))
        with mock.patch.object(self.service._condition, 'wait', side_effect=observe_wait):
            active.start()
            try:
                self.assertTrue(self.worker.started.wait(1))
                queued.start()
                self.assertTrue(queued_waiting.wait(1), 'request never reached scheduler condition wait')
                with self.service._condition:
                    self.assertIs(self.service._inflight_tickets['queued'], self.worker.ticket)
                # Valid protobuf producer_name field; bytes differ without a runtime dependency.
                producer = b'digest-binding-v2'
                replacement = (self.model_root / 'model.onnx').read_bytes() + b'\x12' + bytes([len(producer)]) + producer
                (self.model_root / 'replacement.onnx').write_bytes(replacement)
                digest = hashlib.sha256(replacement).hexdigest()
                changed = dict(self.manifest, model_path='replacement.onnx', sha256=digest)
                reloaded = request(self.path, {'id': 'reload', 'op': 'reload', 'manifest': changed})
                self.assertEqual(reloaded['status'], 'ok', reloaded)
                newer = self.workers[-1]
                self.assertIsNot(newer, self.worker)
                self.worker.release.set()
                active.join(3)
                queued.join(3)
                self.assertFalse(active.is_alive())
                self.assertFalse(queued.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results['active']['model_sha256'], DIGEST)
                self.assertEqual(results['queued']['reason'], 'model_digest_mismatch')
                self.assertEqual((results['queued']['safe_action'], results['queued']['output']), ('no_action', None))
                self.assertEqual(newer.calls, 0)
                self.assert_idle()
                self.assertEqual(request(self.path, self.document(expected_model_sha256=digest))['model_sha256'], digest)
                self.assertNotIn('model_sha256', request(self.path, self.document(id='legacy-new')))
            finally:
                self.worker.release.set()
                active.join(3)
                if queued.ident is not None:
                    queued.join(3)

    def test_post_inference_revocation_dominates_pinned_success(self):
        self.worker.on_result = self.worker.ticket.deny_event.set
        result = request(self.path, self.document(expected_model_sha256=DIGEST))
        self.assertEqual((result['status'], result['reason']), ('unavailable', 'model_revoked'))
        self.assertEqual((result['safe_action'], result['output']), ('no_action', None))
        self.assertEqual(self.worker.calls, 1)
        self.assert_idle()

    def test_pinned_request_error_cleans_ownership(self):
        from xtos_ai_runtime.protocol import RequestError
        def invalid_input():
            raise RequestError('fixture invalid input')
        self.worker.on_result = invalid_input
        result = request(self.path, self.document(expected_model_sha256=DIGEST))
        self.assertEqual((result['status'], result['reason']), ('invalid', 'invalid_request'))
        self.assertEqual((result['safe_action'], result['output']), ('no_action', None))
        self.assert_idle()
        self.worker.on_result = None
        self.assertEqual(request(self.path, self.document())['status'], 'success')


class ClientBindingTests(unittest.TestCase):
    def from_peer(self, response):
        """Untrusted peer bytes on a real AF_UNIX socket, not a client mock."""
        with tempfile.TemporaryDirectory(prefix='xtos-binding-peer-') as directory:
            path = Path(directory) / 'peer.sock'
            errors = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.settimeout(3)
                listener.bind(str(path))
                listener.listen(1)
                def respond():
                    try:
                        conn, _ = listener.accept()
                        with conn, conn.makefile('rb') as stream:
                            stream.readline()
                            conn.sendall(json.dumps(response).encode() + b'\n')
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=respond, daemon=True)
                thread.start()
                try:
                    return request(path, {'id': 'x', 'op': 'infer', 'model': 'fixed-classifier',
                                          'expected_model_sha256': DIGEST}, timeout=2)
                finally:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])

    def test_client_rejects_missing_malformed_or_conflicting_success_digest(self):
        base = {'schema': SCHEMA, 'id': 'x', 'status': 'success', 'safe_action': 'action_allowed',
                'reason': None, 'model': 'fixed-classifier', 'provider': 'CPUExecutionProvider',
                'latency_ms': 0, 'output': [[1.0]]}
        for response in (base, *(dict(base, model_sha256=value) for value in (*INVALID, OTHER))):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, 'model digest'):
                self.from_peer(response)
        valid = dict(base, model_sha256=DIGEST)
        self.assertEqual(self.from_peer(valid), valid)

    def test_client_no_action_allows_absent_identity_but_rejects_bad_identity(self):
        base = {'schema': SCHEMA, 'id': 'x', 'status': 'unavailable',
                'safe_action': 'no_action', 'reason': 'model_unavailable', 'output': None}
        for value in (*INVALID[1:], OTHER):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'model digest'):
                self.from_peer(dict(base, model_sha256=value))
        for response in (base, dict(base, model_sha256=None), dict(base, model_sha256=DIGEST)):
            with self.subTest(response=response):
                self.assertEqual(self.from_peer(response), response)

    def test_client_validates_pin_before_opening_socket(self):
        base = {'id': 'x', 'op': 'infer', 'model': 'fixed-classifier'}
        invalid_documents = [dict(base, expected_model_sha256=value) for value in INVALID]
        invalid_documents += [dict(base, model=value, expected_model_sha256=DIGEST)
                              for value in (None, '', True, 3, [], {})]
        invalid_documents += [dict(base, op=op, expected_model_sha256=DIGEST)
                              for op in ('list', 'health', 'load', 'reload', 'unload', 'cancel')]
        with mock.patch('xtos_ai_runtime.client.socket.socket') as opened:
            opened.return_value.__enter__.return_value.recv.return_value = b''
            for document in invalid_documents:
                with self.subTest(document=document), self.assertRaises(ValueError):
                    request('/unused', document)
            opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
