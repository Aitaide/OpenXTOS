"""Provider v2: real signed CPU child; GPU API mocks are labelled separately."""
from dataclasses import asdict, replace
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools/xtos-ai-runtime'))
from xtos_ai_runtime import worker as implementation
from xtos_ai_runtime.worker import Worker, WorkerError
from xtos_ai_runtime.protocol import validate_execution_profile
from ai_trust_fixtures import make_fixture, model_claims, signed_envelope
from ai_service_trust_fixtures import fixture_ticket
from ai_provider_runtime_fixtures import observe_runtime, execution_profile, synthetic_profile

def actual_profile(provider='CPUExecutionProvider'):
    return execution_profile(observe_runtime(os.environ['XTOS_TEST_AI_PYTHON']), provider)

@unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'),'explicit locked AI Python not selected')
class RealProviderWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='xtos-v2-worker-')
        self.addCleanup(self.tmp.cleanup)
        self.fx=make_fixture(Path(self.tmp.name))
        self.fx.model_bytes=(ROOT/'tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx').read_bytes()
        (self.fx.model_root/'model.onnx').write_bytes(self.fx.model_bytes)

    def worker(self,provider='CPUExecutionProvider',profile=None):
        claims=model_claims(self.fx.now,'model.onnx',self.fx.model_bytes,provider=provider,
                            execution=profile or actual_profile(provider),min_runtime_version='1.1.0')
        env=signed_envelope('xtos-ai-model-v2',hashlib.sha256(self.fx.publisher.public_raw).hexdigest(),claims,self.fx.publisher,b'XTOS-AI-MODEL-v2\0')
        ticket=self.fx.policy.authorize(env,self.fx.model_root)
        w=Worker(ticket,python_command=[os.environ['XTOS_TEST_AI_PYTHON']],load_timeout=30)
        self.addCleanup(w.close)
        return w

    def test_real_signed_cpu_receipt_golden_and_parent_placement_unchanged(self):
        affinity=os.sched_getaffinity(0); scheduler=os.sched_getscheduler(0)
        w=self.worker(); ready=w.start()
        p=actual_profile()
        self.assertEqual(ready.get('execution'),{**p,'provider_options':{}})
        self.assertEqual(ready['runtime_version'],p['onnxruntime_version'])
        denominator=math.e+2
        for value,expected in [([[0.,0.,0.,0.]],[[1/3,1/3,1/3]]),
                               ([[1.,0.,0.,0.]],[[math.e/denominator,1/denominator,1/denominator]])]:
            result=w.infer('golden',value,3000)
            self.assertEqual(result['status'],'success',result)
            for a,b in zip(result['output'][0],expected[0]): self.assertAlmostEqual(a,b,places=6)
        self.assertEqual(os.sched_getaffinity(0),affinity)
        self.assertEqual(os.sched_getscheduler(0),scheduler)
        self.assertEqual(w.diagnostics(),'')

    def test_real_runtime_mismatches_environment_and_unavailable_gpu_fail_closed(self):
        for field,value,reason in [('python_abi','cpython-312','runtime_abi_mismatch'),
                                   ('onnxruntime_version','1.23.1','runtime_version_mismatch'),
                                   ('runtime_files',{**actual_profile()['runtime_files'],next(iter(actual_profile()['runtime_files'])):'0'*64},'runtime_file_mismatch')]:
            with self.subTest(field=field):
                p=actual_profile();p[field]=value;w=self.worker(profile=p)
                with self.assertRaises(WorkerError) as caught: w.start()
                self.assertEqual(caught.exception.reason,reason)
                self.assertFalse(w.alive)
        for provider in ('CUDAExecutionProvider','MIGraphXExecutionProvider'):
            with self.subTest(scope='unavailable provider before library verification',provider=provider):
                if provider in observe_runtime(os.environ['XTOS_TEST_AI_PYTHON'])['providers']:
                    continue  # Placeholder files do not attest an installed vendor runtime.
                w=self.worker(provider)
                with self.assertRaises(WorkerError) as caught:w.start()
                self.assertEqual(caught.exception.reason,'provider_unavailable')
                self.assertFalse(w.alive)
        w=self.worker()
        with patch.dict(os.environ,{'ORT_CUDA_UNSIGNED':'1'}):
            with self.assertRaises(WorkerError) as caught:w.start()
        self.assertEqual(caught.exception.reason,'runtime_environment_override')

    def test_real_cpu_deadline_cancel_kill_and_reap(self):
        w=self.worker();w.start();pid=w.pid
        os.kill(pid,signal.SIGSTOP)
        self.assertEqual(w.infer('timeout',[[1.,0.,0.,0.]],100)['status'],'timeout')
        self.assertFalse(w.alive)
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        w.restart();pid=w.pid;cancel=threading.Event();cancel.set()
        self.assertEqual(w.infer('cancel',[[1.,0.,0.,0.]],1000,cancel)['status'],'cancelled')
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

class ProviderAPIMockTests(unittest.TestCase):
    """Pure API injection: no GPU runtime/artifact/hardware claims."""
    def test_thread_start_failure_reaps_real_child_and_closes_all_pipes(self):
        before=set(os.listdir('/proc/self/fd'))
        w=Worker(fixture_ticket())
        popen=subprocess.Popen
        def sleeper(argv,**kwargs):
            return popen([sys.executable,'-I','-c','import time; time.sleep(30)'],**kwargs)
        try:
            with patch.object(implementation.subprocess,'Popen',side_effect=sleeper), \
                 patch.object(implementation.threading.Thread,'start',
                              side_effect=RuntimeError('injected thread resource exhaustion')):
                with self.assertRaises(WorkerError) as caught:w.start()
            self.assertIn('injected thread resource exhaustion',str(caught.exception))
            self.assertFalse(w.alive)
            with self.assertRaises(ProcessLookupError):os.kill(w.pid,0)
            self.assertTrue(all(s.closed for s in (w.process.stdin,w.process.stdout,w.process.stderr)))
            w.close();w.close()
            self.assertEqual(set(os.listdir('/proc/self/fd')),before)
        finally:
            # Test-owned cleanup occurs only after product assertions, retaining
            # the failure without leaking the intentionally broken baseline.
            if w.process is not None:
                if w.process.poll() is None:w.process.kill();w.process.wait(timeout=3)
                for stream in (w.process.stdin,w.process.stdout,w.process.stderr):stream.close()
            w._stderr_thread=None
            w.close()

    def test_parent_refuses_missing_or_inexact_typed_execution_receipts(self):
        profile=validate_execution_profile('CPUExecutionProvider',synthetic_profile())
        ticket=fixture_ticket()
        ticket=replace(ticket,manifest=replace(ticket.manifest,execution=profile))
        receipt={**synthetic_profile(),'provider_options':{}}
        ready={'status':'ready','provider':'CPUExecutionProvider',
               'model_sha256':ticket.manifest.sha256,'runtime_version':'1.23.2',
               'execution':receipt}
        cases=[None,{**receipt,'device_id':False},
               {**receipt,'memory_limit_bytes':0.0},
               {**receipt,'cpu_affinity':[False]},
               {**receipt,'runtime_files':{}},
               {**receipt,'provider_options':{'unsigned':'1'}}]
        popen=subprocess.Popen
        def sleeper(argv,**kwargs):
            return popen([sys.executable,'-I','-c','import time; time.sleep(30)'],**kwargs)
        for value in cases:
            with self.subTest(receipt=value):
                w=Worker(ticket)
                try:
                    with patch.object(implementation.subprocess,'Popen',side_effect=sleeper), \
                         patch.object(w,'_exchange',return_value={**ready,'execution':value}):
                        with self.assertRaises(WorkerError) as caught:w.start()
                    self.assertEqual(caught.exception.reason,'execution_mismatch')
                    self.assertFalse(w.alive)
                finally:
                    w.close()

    def test_mock_cpu_options_cannot_silently_drift(self):
        class Session:
            def get_providers(self):return ['CPUExecutionProvider']
            def get_provider_options(self):return {'CPUExecutionProvider':{'unsigned':'1'}}
        with self.assertRaises(WorkerError):implementation._check_session(Session(),'CPUExecutionProvider',{})

    def test_mock_gpu_config_disable_and_readback_controls(self):
        from types import SimpleNamespace
        for provider in ('CUDAExecutionProvider','MIGraphXExecutionProvider'):
            p=validate_execution_profile(provider,synthetic_profile(provider));events=[]
            expected=implementation._provider_options(provider,p)
            class Options:
                def add_session_config_entry(self,k,v):events.append((k,v))
            class Session:
                def disable_fallback(self):events.append('disable')
                def get_providers(self):return [provider]
                def get_provider_options(self):return {provider:dict(expected)}
            def create(*args,**kwargs):
                self.assertEqual(events,[('session.disable_cpu_ep_fallback','1')])
                self.assertEqual(kwargs['providers'],[(provider,expected)])
                events.append('create');return Session()
            s=implementation._create_v2_session(SimpleNamespace(InferenceSession=create),b'model',Options(),provider,p)
            self.assertEqual(events[-2:],['create','disable'])
            s.get_providers=lambda:['CPUExecutionProvider']
            with self.assertRaises(WorkerError):implementation._check_session(s,provider,expected)
            s.get_providers=lambda:[provider]
            s.get_provider_options=lambda:{provider:{**expected,'device_id':'31'}}
            with self.assertRaises(WorkerError):implementation._check_session(s,provider,expected)

    def test_transport_revalidation_and_no_follow_regular_files(self):
        p=validate_execution_profile('CPUExecutionProvider',synthetic_profile())
        wire=json.loads(json.dumps(asdict(p)))
        self.assertEqual(implementation._wire_profile('CPUExecutionProvider',wire),p)
        wire['cpu_affinity']=[True]
        with self.assertRaises(ValueError):implementation._wire_profile('CPUExecutionProvider',wire)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);f=root/'file';f.write_bytes(b'x');link=root/'link';link.symlink_to(f)
            self.assertEqual(implementation._runtime_hash(f),hashlib.sha256(b'x').hexdigest())
            with self.assertRaises(OSError):implementation._runtime_hash(link)
            fifo=root/'fifo';os.mkfifo(fifo)
            with self.assertRaises(ValueError):implementation._runtime_hash(fifo)
            ancestor=root/'alias';ancestor.symlink_to(root,target_is_directory=True)
            with self.assertRaises(OSError):implementation._runtime_hash(ancestor/'file')

if __name__=='__main__': unittest.main()
