"""Installed production-provider sources; no optional vendor runtime bundled."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'__init__.py', '__main__.py', 'client.py', 'protocol.py',
         'server.py', 'trust.py', 'worker.py'}


def installer():
    spec = importlib.util.spec_from_file_location('provider_source_install', ROOT/'scripts/install_eeg_loop.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('source installer unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProviderInstallTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('XTOS_TEST_AI_PYTHON'), 'explicit locked AI Python not selected')
    def test_installed_signed_cpu_service_executes_installed_worker(self):
        with tempfile.TemporaryDirectory(prefix='p5-install-') as directory:
            stage = Path(directory)
            installer().install(stage, ROOT)
            package = stage/'usr/lib/xtos/xtos_ai_runtime'
            code = '''import hashlib,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from xtos_ai_runtime.server import InferenceService
from xtos_ai_runtime import worker
sys.path.insert(0,sys.argv[2])
from ai_service_trust_fixtures import make_real_signed_service_fixture
from ai_trust_fixtures import model_claims,signed_envelope
from ai_provider_runtime_fixtures import observe_runtime,execution_profile
fx,_=make_real_signed_service_fixture(Path(sys.argv[3]))
p=execution_profile(observe_runtime(os.environ['XTOS_TEST_AI_PYTHON']))
claims=model_claims(fx.now,'model.onnx',fx.model_bytes,min_runtime_version='1.1.0',execution=p)
envelope=signed_envelope('xtos-ai-model-v2',hashlib.sha256(fx.publisher.public_raw).hexdigest(),claims,fx.publisher,b'XTOS-AI-MODEL-v2\\x00')
s=InferenceService(fx.model_root,python_command=[os.environ['XTOS_TEST_AI_PYTHON']],trust_policy=fx.policy)
try:
 loaded=s.dispatch({'id':'load','op':'load','manifest':envelope.decode()})
 if loaded['status']!='ok':raise RuntimeError(str(loaded))
 output=s.dispatch({'id':'infer','op':'infer','model':'fixed-classifier','expected_provider':'CPUExecutionProvider','input':[[2.,0.,-1.,0.]],'deadline_ms':5000})
 if output['status']!='success' or abs(output['output'][0][0]-0.8437947034835815)>1e-5:raise RuntimeError(str(output))
 print(json.dumps({'loaded':loaded,'output':output,'worker_module':worker.__file__}))
finally:s.close()
'''
            child = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                str(package.parent), str(ROOT/'tests'), str(stage/'fixture')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(Path(result['worker_module']), package/'worker.py')
            self.assertEqual(result['loaded']['provider'], 'CPUExecutionProvider')
            self.assertEqual(result['output']['provider'], 'CPUExecutionProvider')

    def test_installed_package_has_exact_current_sources_and_v2_profile(self):
        with tempfile.TemporaryDirectory(prefix='xtos-provider-install-') as directory:
            stage = Path(directory)
            manifest = installer().install(stage, ROOT)
            package = stage/'usr/lib/xtos/xtos_ai_runtime'
            self.assertEqual({p.name for p in package.iterdir()}, NAMES)
            for name in NAMES:
                source = ROOT/'tools/xtos-ai-runtime/xtos_ai_runtime'/name
                self.assertEqual((package/name).read_bytes(), source.read_bytes())
                self.assertEqual(manifest['source_hashes'][f'usr/lib/xtos/xtos_ai_runtime/{name}'],
                                 hashlib.sha256(source.read_bytes()).hexdigest())
            raw = json.loads((ROOT/'tools/xtos-ai-runtime/model-manifest.example.json').read_text())
            raw['model_path'] = str((ROOT/raw['model_path']).resolve())
            raw.update(schema='xtos-ai-manifest-v2', execution={
                'onnxruntime_version': '1.23.2', 'python_abi': 'cpython-311',
                'device_id': 0, 'memory_limit_bytes': 0, 'scheduler': 'SCHED_OTHER',
                'cpu_affinity': [0],
                'runtime_files': {'capi/onnxruntime_pybind11_state.so': 'a'*64}})
            # Synthetic profile digests establish structure only, not runtime admission.
            data = stage/'manifest.json'
            data.write_text(json.dumps(raw))
            code = ('import json,sys; from pathlib import Path; '
                    'sys.path.insert(0,sys.argv[1]); import xtos_ai_runtime as x; '
                    'm=x.validate_manifest(json.loads(Path(sys.argv[2]).read_text()),sys.argv[3]); '
                    'print(json.dumps({"version":x.RUNTIME_VERSION,"execution":m.execution.cpu_affinity,'
                    '"provider":m.provider,"module":x.__file__}))')
            child = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                                    str(package.parent), str(data), str(ROOT)],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(result['version'], '1.1.0')
            self.assertEqual(result['execution'], [0])
            self.assertEqual(result['provider'], 'CPUExecutionProvider')
            self.assertEqual(Path(result['module']), package/'__init__.py')
            self.assertFalse((stage/'opt/xtos-ai').exists())
            self.assertFalse(any('test_' in p.name or 'fixture' in p.name
                                 for p in package.rglob('*')))

    def test_both_installers_declare_the_same_production_module_inventory(self):
        spec = importlib.util.spec_from_file_location('provider_live_install', ROOT/'scripts/live_ai.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('Live AI installer unavailable')
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(ROOT/'scripts'), *sys.path]):
            spec.loader.exec_module(module)
        self.assertEqual(set(module.LIVE_AI_SOURCE_NAMES), NAMES)
        self.assertEqual(set(installer().PACKAGES['xtos_ai_runtime']), NAMES)


if __name__ == '__main__':
    unittest.main()
