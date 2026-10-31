"""Software-only API tests: injected providers are not hardware evidence."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-runtime'))
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-provider-experiment'))
from provider_experiment import (  # noqa: E402
    ProviderExperimentError,
    ProviderUnavailableError,
    run_experiment,
)
experiment = sys.modules['provider_experiment']

CPU = 'CPUExecutionProvider'
CUDA = 'CUDAExecutionProvider'
ROCM = 'ROCMExecutionProvider'


class FixtureRuntime:
    """Only the optional runtime API boundary is substituted."""
    def __init__(self, available=(CPU,)):
        self.available = list(available)
        self.created = []

    def get_available_providers(self):
        return self.available[:]

    def InferenceSession(self, *args, **kwargs):
        self.created.append((args, kwargs))
        raise AssertionError('must not create unavailable-provider session')


class FixtureArray:
    def __init__(self, value, dtype='float32'):
        self.value = copy.deepcopy(value)
        self.dtype = dtype
        self.shape = (len(value), len(value[0]))

    def tolist(self):
        return copy.deepcopy(self.value)


class FixtureArrays:
    float32 = 'float32'

    @staticmethod
    def asarray(value, dtype):
        return FixtureArray(value, dtype)


class ExecutingFixtureRuntime(FixtureRuntime):
    def __init__(self, provider=CUDA):
        super().__init__((CPU, provider))
        self.provider = provider
        self.selected = [provider]
        self.disabled = False
        self.calls = []
        self.SessionOptions = SimpleNamespace
        self.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL='sequential')

    def InferenceSession(self, model_bytes, *, sess_options, providers):
        self.created.append((model_bytes, sess_options, providers))
        return self

    def disable_fallback(self):
        self.disabled = True

    def get_providers(self):
        return self.selected[:]

    def get_inputs(self):
        return [SimpleNamespace(name='input', type='tensor(float)', shape=[1, 4])]

    def get_outputs(self):
        return [SimpleNamespace(name='probabilities', type='tensor(float)', shape=[1, 3])]

    def run(self, names, feed):
        self.calls.append((names, feed))
        value = feed['input'].tolist()[0][0]
        return [FixtureArray([[value, 0.25, 0.75]])]


class ProviderExperimentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        fixture = ROOT / 'tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx'
        data = fixture.read_bytes()
        (self.root / 'model.onnx').write_bytes(data)
        self.manifest = {
            'name': 'provider-fixture', 'version': '1', 'model_path': 'model.onnx',
            'sha256': hashlib.sha256(data).hexdigest(), 'provider': CUDA,
            'threads': {'intra_op': 1, 'inter_op': 1},
            'input': {'name': 'input', 'dtype': 'float32', 'shape': [1, 4]},
            'output': {'name': 'probabilities', 'dtype': 'float32', 'shape': [1, 3]},
        }

    def test_standalone_import_resolves_sibling_runtime_without_caller_path_repair(self):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'import provider_experiment; '
                'print(provider_experiment.validate_manifest.__module__)')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                                 str(ROOT / 'tools/xtos-ai-provider-experiment')],
                                cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'xtos_ai_runtime.protocol')

    def test_changed_model_bytes_are_refused_before_session_creation(self):
        runtime = ExecutingFixtureRuntime()
        original = runtime.get_available_providers
        def change_after_manifest_validation():
            model = self.root / 'model.onnx'
            model.write_bytes(model.read_bytes() + b'changed-after-validation')
            return original()
        runtime.get_available_providers = change_after_manifest_validation
        with self.assertRaisesRegex(ProviderExperimentError, 'model.*changed|digest'):
            self.invoke(runtime)
        self.assertEqual(runtime.created, [], 'unverified model bytes reached the session')

    def test_model_snapshot_loaded_is_the_verified_bytes_even_if_path_changes_at_constructor(self):
        runtime = ExecutingFixtureRuntime()
        expected = (self.root / 'model.onnx').read_bytes()
        original = runtime.InferenceSession
        def replace_at_constructor(model, **kwargs):
            (self.root / 'model.onnx').write_bytes(b'changed-after-snapshot')
            return original(model, **kwargs)
        runtime.InferenceSession = replace_at_constructor
        result = self.invoke(runtime)
        self.assertEqual(runtime.created[0][0], expected)
        self.assertEqual(result['safe_action'], 'no_action')

    def test_tensor_metadata_mismatch_refuses_before_any_run(self):
        cases = ([], [SimpleNamespace(name='wrong', type='tensor(float)', shape=[1, 4])],
                 [SimpleNamespace(name='input', type='tensor(double)', shape=[1, 4])],
                 [SimpleNamespace(name='input', type='tensor(float)', shape=[1, 5])])
        for metadata in cases:
            with self.subTest(metadata=metadata):
                runtime = ExecutingFixtureRuntime()
                runtime.get_inputs = lambda: metadata
                with self.assertRaises(ProviderExperimentError):
                    self.invoke(runtime)
                self.assertEqual(runtime.calls, [])

    def test_malformed_smoke_or_inference_output_never_returns_success(self):
        for stage in (1, 2):
            for malformed in ([], [FixtureArray([[float('nan'), 0, 1]])],
                              [FixtureArray([[1, 2]])]):
                with self.subTest(stage=stage, malformed=malformed):
                    runtime = ExecutingFixtureRuntime()
                    original = runtime.run
                    def bad_output(*args):
                        valid = original(*args)
                        return malformed if len(runtime.calls) == stage else valid
                    runtime.run = bad_output
                    with self.assertRaises(ProviderExperimentError):
                        self.invoke(runtime)
                    self.assertEqual(len(runtime.calls), stage)

    def test_provider_switch_during_actual_inference_is_refused(self):
        runtime = ExecutingFixtureRuntime()
        original = runtime.run
        def switched(*args):
            output = original(*args)
            if len(runtime.calls) == 2:
                runtime.selected = [CPU]
            return output
        runtime.run = switched
        with self.assertRaises(ProviderExperimentError):
            self.invoke(runtime)
        self.assertEqual(len(runtime.calls), 2)

    def test_unavailable_requested_provider_refuses_before_session(self):
        runtime = FixtureRuntime()
        original = copy.deepcopy(self.manifest)
        with self.assertRaises(ProviderExperimentError) as caught:
            run_experiment(self.manifest, self.root, [[0, 0, 0, 0]], runtime=runtime)
        self.assertEqual(type(caught.exception).__name__, 'ProviderUnavailableError')
        self.assertEqual(caught.exception.safe_action, 'no_action')
        self.assertEqual(runtime.created, [])
        self.assertEqual(self.manifest, original)

    def test_unknown_provider_is_refused_even_when_registered(self):
        for provider in ('AzureExecutionProvider', '', None, True, [], 'cuda'):
            with self.subTest(provider=provider):
                self.manifest['provider'] = provider
                runtime = FixtureRuntime(available=[provider])
                with self.assertRaises(experiment.ProviderExperimentError):
                    experiment.run_experiment(self.manifest, self.root, [[0, 0, 0, 0]], runtime=runtime)
                self.assertEqual(runtime.created, [])

    def test_missing_provider_has_no_cpu_default(self):
        self.manifest.pop('provider')
        with self.assertRaises(experiment.ProviderExperimentError):
            experiment.run_experiment(self.manifest, self.root, [[0, 0, 0, 0]], runtime=FixtureRuntime())

    def test_missing_runtime_is_typed_and_lazy(self):
        with patch.dict(sys.modules, {'onnxruntime': None}):
            with self.assertRaises(experiment.ProviderExperimentError) as caught:
                experiment.run_experiment(self.manifest, self.root, [[0, 0, 0, 0]])
        self.assertEqual(type(caught.exception).__name__, 'RuntimeUnavailableError')
        self.assertEqual(caught.exception.safe_action, 'no_action')

    def test_explicit_provider_runs_smoke_then_input_with_no_action(self):
        for provider in (CPU, CUDA, ROCM):
            with self.subTest(provider=provider):
                self.manifest['provider'] = provider
                runtime = ExecutingFixtureRuntime(provider)
                original = copy.deepcopy(self.manifest)
                with patch.dict(sys.modules, {'numpy': FixtureArrays}):
                    result = experiment.run_experiment(self.manifest, self.root, [[0.5, 0, 0, 0]], runtime=runtime)
                self.assertIsInstance(result, dict, 'execution must return a structured result')
                self.assertEqual(result, {
                    'status': 'success', 'safe_action': 'no_action', 'reason': 'experimental_only',
                    'provider': provider, 'selected_provider': provider,
                    'registered_providers': runtime.available,
                    'output': [[0.5, 0.25, 0.75]],
                })
                self.assertEqual(self.manifest, original)
                self.assertEqual(len(runtime.created), 1)
                model, options, providers = runtime.created[0]
                self.assertEqual(model, (self.root / 'model.onnx').read_bytes())
                self.assertEqual(providers, [provider])
                self.assertEqual((options.intra_op_num_threads, options.inter_op_num_threads), (1, 1))
                self.assertEqual(options.execution_mode, 'sequential')
                self.assertTrue(runtime.disabled)
                self.assertEqual(len(runtime.calls), 2)
                self.assertEqual(runtime.calls[0][0], ['probabilities'])
                self.assertEqual(runtime.calls[0][1]['input'].tolist(), [[0, 0, 0, 0]])
                self.assertEqual(runtime.calls[1][1]['input'].tolist(), [[0.5, 0, 0, 0]])
                self.assertEqual(runtime.calls[1][1]['input'].dtype, 'float32')

    def invoke(self, runtime=None, value=None):
        with patch.dict(sys.modules, {'numpy': FixtureArrays}):
            return experiment.run_experiment(self.manifest, self.root,
                [[0.5, 0, 0, 0]] if value is None else value,
                runtime=runtime or ExecutingFixtureRuntime())

    def test_activation_fallback_is_refused_before_smoke(self):
        for selected in ([CPU], [CUDA, CPU], [], [ROCM]):
            with self.subTest(selected=selected):
                runtime = ExecutingFixtureRuntime()
                runtime.selected = selected
                with self.assertRaises(experiment.ProviderExperimentError):
                    self.invoke(runtime)
                self.assertEqual(runtime.calls, [])

    def test_disable_fallback_failure_is_refused_before_smoke(self):
        runtime = ExecutingFixtureRuntime()
        def failed_disable():
            raise RuntimeError('cannot disable fallback')
        runtime.disable_fallback = failed_disable
        with self.assertRaises(experiment.ProviderExperimentError):
            self.invoke(runtime)
        self.assertEqual(runtime.calls, [])

    def test_provider_switch_during_smoke_is_refused(self):
        runtime = ExecutingFixtureRuntime()
        original = runtime.run
        def switched(*args):
            output = original(*args)
            runtime.selected = [CPU]
            return output
        runtime.run = switched
        with self.assertRaises(experiment.ProviderExperimentError):
            self.invoke(runtime)
        self.assertEqual(len(runtime.calls), 1)

    def test_runtime_without_disable_fallback_api_is_refused(self):
        runtime = ExecutingFixtureRuntime()
        runtime.disable_fallback = None
        with self.assertRaises(experiment.ProviderExperimentError):
            self.invoke(runtime)
        self.assertEqual(runtime.calls, [])


if __name__ == '__main__':
    unittest.main()
