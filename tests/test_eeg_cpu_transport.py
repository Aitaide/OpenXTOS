"""Actual AF_UNIX peer tests for one-window no-action transport decisions."""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'tools/xtos-ai-runtime')]
from test_eeg_feature_binding import configuration, window
from xtos_eeg_loop.feature_binding import validate_binding


@contextmanager
def peer(response, *, fragment_interval=0.0):
    with tempfile.TemporaryDirectory(prefix='eeg-peer-', dir=os.environ.get('TMPDIR')) as directory:
        path = Path(directory)/'s'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(3)
        received = []
        errors = []
        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    stream = connection.makefile('rb')
                    with stream:
                        received.append(json.loads(stream.readline(65537)))
                    payload = json.dumps(response, allow_nan=False).encode()+b'\n'
                    if fragment_interval:
                        for index in range(6):
                            time.sleep(fragment_interval)
                            connection.sendall(payload[index*len(payload)//6:(index+1)*len(payload)//6])
                    else:
                        connection.sendall(payload)
            except (BrokenPipeError, ConnectionResetError) as exc:
                if not fragment_interval:
                    errors.append(exc)
            except BaseException as exc:
                errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield path, received
        finally:
            thread.join(timeout=4)
            if thread.is_alive():
                listener.close()
                raise AssertionError('peer thread did not exit')
            if errors:
                raise AssertionError(repr(errors))


def success():
    return dict(schema='xtos-ai-runtime-v1', id='transport-1', status='success',
                reason=None, model='eeg-fixture', model_sha256='a'*64,
                provider='CPUExecutionProvider', latency_ms=0.1,
                safe_action='action_allowed', output=[[0.25, 0.25, 0.5]])


class CpuTransportTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module('xtos_eeg_loop.inference')

    def call(self, path, record=None, clock=lambda: 200):
        return self.api.infer_window(validate_binding(configuration()),
                                     window() if record is None else record,
                                     path, 'transport-1', 3000, now_ns=clock)

    def test_slow_fragmented_peer_cannot_reset_total_socket_budget(self):
        stamp = time.monotonic_ns()
        record = dict(window(), first_host_rx_ns=stamp, last_host_rx_ns=stamp,
                      emitted_at_ns=stamp, max_receive_age_ns=5_000_000_000)
        binding = validate_binding(dict(configuration(), max_receive_age_ns=5_000_000_000))
        with peer(success(), fragment_interval=0.1) as (path, received):
            started = time.monotonic()
            result = self.api.infer_window(binding, record, path, 'transport-1', 40)
            elapsed = time.monotonic() - started
        self.assertEqual(len(received), 1)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['safe_action'], 'no_action')
        self.assertIsNone(result['output'])
        self.assertEqual(result['reason'], 'deadline_exceeded')
        self.assertLessEqual(elapsed, 0.44, 'total timeout must not reset for each received fragment')

    def test_invalid_request_identity_is_not_echoed(self):
        for identity in ('x'*129, 'bad/id', '\ud800', '', True, 1):
            with self.subTest(identity=repr(identity)):
                result = self.api.infer_window(validate_binding(configuration()), window(),
                                              '/missing', identity, 3000, now_ns=lambda: 200)
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(result['request_id'])
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')

    def test_invalid_features_and_absent_service_are_safe(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            absent = Path(directory)/'missing'
            bad = dict(window(), unit='nanovolt')
            result = self.call(absent, bad)
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(result['reason'], 'invalid_features')
            self.assertEqual(result['safe_action'], 'no_action')
            self.assertIsNone(result['output'])
            result = self.call(absent)
            self.assertEqual(result['reason'], 'inference_unavailable')
            self.assertIsNone(result['output'])

    def test_refused_or_misattributed_peer_never_exposes_output(self):
        denied = dict(schema='xtos-ai-runtime-v1',id='transport-1',status='unavailable',
                      model=None,reason='model_digest_mismatch',safe_action='no_action',output=None)
        for response in (denied, dict(success(), model_sha256='b'*64), dict(success(), output=None)):
            with self.subTest(response=response), peer(response) as (path, received):
                result = self.call(path)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['safe_action'], 'no_action')
                self.assertIsNone(result['output'])

    def test_response_rechecks_oldest_age_clock_and_total_deadline(self):
        for after in (201, 199, True, 200.0):
            ticks = iter([200, after])
            with self.subTest(after=after), peer(success()) as (path, received):
                result = self.call(path, clock=lambda: next(ticks))
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(result['output'])
        record = dict(window(), first_host_rx_ns=100, last_host_rx_ns=100,
                      emitted_at_ns=100, max_receive_age_ns=10_000_000)
        raw = dict(configuration(), max_receive_age_ns=10_000_000)
        ticks = iter([100, 1_000_100])
        with peer(success()) as (path, received):
            result = self.api.infer_window(validate_binding(raw), record, path,
                                          'transport-1', 1, now_ns=lambda: next(ticks))
        self.assertEqual(result['reason'], 'deadline_exceeded')
        self.assertIsNone(result['output'])

    def test_probability_shape_range_and_normalization_are_checked(self):
        for output in ([[0.5, 0.5]], [[0.25, 0.25, 0.5], [0.25, 0.25, 0.5]],
                       [[1.1, -0.1, 0.0]], [[0.1, 0.1, 0.1]]):
            with self.subTest(output=output), peer(dict(success(), output=output)) as (path, received):
                result = self.call(path)
                self.assertEqual(result['reason'], 'invalid_probabilities')
                self.assertIsNone(result['output'])

    @unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'), 'locked AI runtime not selected')
    def test_native_main_json_to_signed_cpu_service(self):
        # Cross-component integration regression, not reconstructed strict TDD.
        import math
        import subprocess
        import time
        from ai_service_trust_fixtures import make_real_signed_service_fixture
        from ai_trust_fixtures import model_claims, signed_envelope
        from xtos_ai_runtime.server import InferenceService, UnixInferenceServer
        from xtos_ai_runtime.client import request
        with tempfile.TemporaryDirectory(prefix='eeg-chain-', dir=os.environ.get('TMPDIR')) as directory:
            work = Path(directory)
            binary = work/'eeg-stream'
            tool = ROOT/'tools/xtos-eeg-stream'
            command = ['cc','-std=c11','-O2','-Wall','-Wextra','-Werror',
                       '-I'+str(tool),'-I'+str(ROOT/'sdk/libxtos-bci/include'),
                       '-I'+str(ROOT/'sdk/libxtos-signal/include')]
            command += [str(tool/p) for p in ('main.c','eeg_export.c','eeg_stream.c','tests/test_eeg_export.c')]
            command += [str(ROOT/'sdk/libxtos-bci/src/session.c'),str(ROOT/'sdk/libxtos-signal/src/eeg_window.c'),
                        '-lm','-Wl,--wrap=open','-Wl,--wrap=close','-Wl,--wrap=ioctl','-o',str(binary)]
            built = subprocess.run(command,capture_output=True,text=True,timeout=40)
            self.assertEqual(built.returncode,0,built.stderr)
            fx, _ = make_real_signed_service_fixture(work)
            payload = (ROOT/'tools/xtos_eeg_loop/fixtures/mean_rms_transport.onnx').read_bytes()
            (fx.model_root/'model.onnx').write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            claims = model_claims(fx.now,'model.onnx',payload,name='mean-rms-transport-fixture',
                                 input={'name':'features_uv','dtype':'float32','shape':[1,16]})
            envelope = signed_envelope('xtos-ai-model-v1',hashlib.sha256(fx.publisher.public_raw).hexdigest(),
                                       claims,fx.publisher,b'XTOS-AI-MODEL-v1\x00')
            service = InferenceService(fx.model_root,python_command=[os.environ['XTOS_TEST_AI_PYTHON']],
                                       trust_policy=fx.policy)
            path = work/'s'
            server = UnixInferenceServer(path,service)
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            pid = None
            try:
                loaded = request(path,dict(id='load16',op='load',manifest=json.loads(envelope)),timeout=60)
                self.assertEqual(loaded['status'],'ok',loaded)
                self.assertEqual(loaded['model_sha256'],digest)
                self.assertEqual(claims['input'],{'name':'features_uv','dtype':'float32','shape':[1,16]})
                pid = request(path,dict(id='health16',op='health'))['models'][claims['name']]['pid']
                native_command = [str(binary),'--device','/dev/xtos-export-fixture','--session-id','9',
                                  '--window','4','--hop','2','--max-age-ns','2000000000',
                                  '--duration-ms','30','--max-windows','2']
                native = subprocess.run(native_command,env=dict(os.environ,EXPORT_SCENARIO='complete'),
                                        capture_output=True,text=True,timeout=5)
                self.assertEqual(native.returncode,0,native.stderr)
                rows = [json.loads(line) for line in native.stdout.splitlines()]
                self.assertEqual(rows[-1]['status'],'complete')
                self.assertTrue(any(row['schema']=='xtos.eeg.barrier.v1' for row in rows))
                record = next(row for row in rows if row['schema']=='xtos.eeg.window.v1')
                raw = dict(configuration(),model=claims['name'],model_sha256=digest,window_frames=4,
                           hop_frames=2,max_receive_age_ns=2_000_000_000,input_name='features_uv')
                result = self.api.infer_window(validate_binding(raw),record,path,'chain16',1000)
                self.assertEqual(result['status'],'transport_verified',result)
                self.assertEqual(result['safe_action'],'no_action')
                self.assertEqual(result['source_kind'],'synthetic')
                values = record['values']
                weights = [[((i+1)%5-2)/256,((i+2)%7-3)/256,((i+3)%11-5)/256] for i in range(16)]
                logits = [sum(values[i]*weights[i][c] for i in range(16))+[0.125,-0.125,0][c] for c in range(3)]
                exps = [math.exp(value-max(logits)) for value in logits]
                for actual,expected in zip(result['output'][0],[value/sum(exps) for value in exps]):
                    self.assertAlmostEqual(actual,expected,delta=2e-7)
                wrong = dict(raw,model_sha256='0'*64)
                refused = self.api.infer_window(validate_binding(wrong),record,path,'wrong16',1000)
                self.assertEqual(refused['status'],'rejected')
                self.assertIsNone(refused['output'])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                server.server_close()
                service.close()
            self.assertIsNotNone(pid)
            self.assertFalse(Path(f'/proc/{pid}').exists())

    def test_real_socket_request_is_pinned_and_result_has_no_authority(self):
        with peer(success()) as (path, received):
            result = self.call(path)
        self.assertEqual(result['status'], 'transport_verified', result)
        self.assertEqual(result['safe_action'], 'no_action')
        self.assertEqual(result['reason'], 'transport_only')
        self.assertEqual(result['output'], [[0.25, 0.25, 0.5]])
        self.assertEqual(result['model_sha256'], 'a'*64)
        self.assertEqual(result['session_id'], 2)
        self.assertEqual(result['barrier_id'], 1)
        self.assertEqual(result['source_kind'], 'synthetic')
        self.assertEqual(result['completed_at_ns'], 200)
        self.assertEqual(len(result['feature_digest']), 64)
        self.assertEqual(received[0]['expected_model_sha256'], 'a'*64)
        self.assertEqual(received[0]['input'], [window()['values']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
