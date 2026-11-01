"""Versioned provider admission; real Ed25519, no GPU execution claims."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-ai-runtime'))
from ai_trust_fixtures import make_fixture, model_claims, signed_envelope, policy_claims
from xtos_ai_runtime import protocol, RUNTIME_VERSION
from xtos_ai_runtime.trust import TrustError

V2_SCHEMA = 'xtos-ai-model-v2'
V2_DOMAIN = b'XTOS-AI-MODEL-v2\x00'


def profile(provider='CPUExecutionProvider'):
    files = {'capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so': 'a' * 64}
    if provider != 'CPUExecutionProvider':
        suffix = 'cuda' if provider == 'CUDAExecutionProvider' else 'migraphx'
        files[f'capi/libonnxruntime_providers_{suffix}.so'] = 'b' * 64
    return {'onnxruntime_version': '1.23.0', 'python_abi': 'cpython-311',
            'device_id': 0, 'memory_limit_bytes': 0 if provider == 'CPUExecutionProvider' else 1048576,
            'runtime_files': files, 'cpu_affinity': [0], 'scheduler': 'SCHED_OTHER'}


class ProviderTrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='xtos-provider-')
        self.addCleanup(self.tmp.cleanup)
        self.fx = make_fixture(Path(self.tmp.name) / 'fixture')

    def claims(self, provider='CPUExecutionProvider', **changes):
        return model_claims(self.fx.now, 'model.onnx', self.fx.model_bytes,
                            provider=provider, execution=profile(provider), min_runtime_version='1.1.0', **changes)

    def envelope(self, claims=None, schema=V2_SCHEMA, domain=V2_DOMAIN):
        return signed_envelope(schema, hashlib.sha256(self.fx.publisher.public_raw).hexdigest(),
                               self.claims() if claims is None else claims, self.fx.publisher, domain)

    def raw_manifest(self, provider='CPUExecutionProvider'):
        claims = self.claims(provider)
        return {**{k: claims[k] for k in ('name', 'version', 'model_path', 'sha256', 'provider', 'threads', 'input', 'output')},
                'schema': 'xtos-ai-manifest-v2', 'execution': claims['execution']}

    def assert_reason(self, reason, raw):
        with self.assertRaises(TrustError) as caught:
            self.fx.policy.authorize(raw, self.fx.model_root)
        self.assertEqual(caught.exception.reason, reason)

    def test_real_signed_v2_cpu_authorizes_immutable_execution(self):
        raw = json.loads(self.envelope())
        ticket = self.fx.policy.authorize(raw, self.fx.model_root)
        self.assertEqual(RUNTIME_VERSION, '1.1.0')
        self.assertEqual(ticket.manifest.provider, 'CPUExecutionProvider')
        execution = ticket.manifest.execution
        self.assertEqual(execution.cpu_affinity, (0,))
        self.assertEqual(execution.runtime_files, tuple(sorted(profile()['runtime_files'].items())))
        raw['claims']['execution']['cpu_affinity'].append(1)
        self.assertEqual(execution.cpu_affinity, (0,))
        with self.assertRaises(FrozenInstanceError):
            execution.device_id = 1
        with self.assertRaises(TypeError):
            ticket.claims['execution']['runtime_files']['x'] = '0' * 64
        self.fx.policy.check(ticket)

    def test_profile_scalar_bounds_fail_closed(self):
        mutations = {'onnxruntime_version': ['01.2.3', '1.2', '1.2.3\n', '2147483648.0.0', 1],
                     'python_abi': ['cpython-0311', 'cpython-', 'pypy-311', 'cpython-311\n', 'cpython-' + '1' * 60],
                     'device_id': [True, -1, 1, '0'], 'memory_limit_bytes': [True, -1, 1],
                     'scheduler': ['SCHED_FIFO', 0]}
        for field, values in mutations.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    raw = profile()
                    raw[field] = value
                    with self.assertRaises(protocol.ManifestError):
                        protocol.validate_execution_profile('CPUExecutionProvider', raw)
        self.assertEqual(protocol.validate_execution_profile('CPUExecutionProvider', profile()).device_id, 0)

    def test_profile_file_and_cpuset_boundaries_are_exact(self):
        good = profile()
        pybind = next(iter(good['runtime_files']))
        bad_files = [{}, [], {pybind: 'A' * 64}, {pybind: '0' * 63},
                     {'other.so': '0' * 64}, {'capi/onnxruntime_pybind11_state.so.evil': '0' * 64},
                     {'nested/capi/onnxruntime_pybind11_state.so': '0' * 64}]
        for path in ['/x', 'a//b', './x', 'a/../b', 'a\\b', 'a\x00b', 'a' * 257]:
            bad_files.append({pybind: '0' * 64, path: '0' * 64})
        bad_files.append({**good['runtime_files'], **{f'x{i}': '0' * 64 for i in range(32)}})
        for files in bad_files:
            with self.subTest(files=files):
                raw = profile()
                raw['runtime_files'] = files
                with self.assertRaises(protocol.ManifestError):
                    protocol.validate_execution_profile('CPUExecutionProvider', raw)
        for cpus in [[], [True], [-1], [4096], [1, 0], [0, 0], list(range(257)), (0,), '0']:
            with self.subTest(cpus=cpus):
                raw = profile()
                raw['cpu_affinity'] = cpus
                with self.assertRaises(protocol.ManifestError):
                    protocol.validate_execution_profile('CPUExecutionProvider', raw)
        raw = profile()
        raw['cpu_affinity'] = list(range(255)) + [4095]
        raw['runtime_files'].update({f'x{i}': '0' * 64 for i in range(31)})
        self.assertEqual(len(protocol.validate_execution_profile('CPUExecutionProvider', raw).runtime_files), 32)

    def test_v2_gpu_admission_and_provider_bounds(self):
        for provider in ['CUDAExecutionProvider', 'MIGraphXExecutionProvider']:
            with self.subTest(provider=provider):
                claims = self.claims(provider)
                ticket = self.fx.policy.authorize(self.envelope(claims), self.fx.model_root)
                self.assertEqual(ticket.manifest.provider, provider)
                self.assertEqual(protocol.validate_manifest(self.raw_manifest(provider), self.fx.model_root).provider, provider)
                for field, values in {'device_id': [-1, True, 32], 'memory_limit_bytes': [0, True, 1048575, 68719476737]}.items():
                    for value in values:
                        raw = profile(provider)
                        raw[field] = value
                        with self.assertRaises(protocol.ManifestError):
                            protocol.validate_execution_profile(provider, raw)
                raw = profile(provider)
                raw['device_id'] = 31
                raw['memory_limit_bytes'] = 68719476736
                self.assertEqual(protocol.validate_execution_profile(provider, raw).device_id, 31)
                raw['runtime_files'] = profile()['runtime_files']
                with self.assertRaises(protocol.ManifestError):
                    protocol.validate_execution_profile(provider, raw)
        self.assert_reason('provider_deprecated', self.envelope(self.claims('ROCMExecutionProvider')))
        with self.assertRaises(protocol.ManifestError):
            protocol.validate_execution_profile('unknown', profile())

    def test_v2_requires_provider_aware_runtime_floor(self):
        for minimum in ('0.0.0', '1.0.0', '1.0.1'):
            with self.subTest(minimum=minimum):
                claims = self.claims()
                claims['min_runtime_version'] = minimum
                self.assert_reason('runtime_version_unsupported', self.envelope(claims))
        claims = self.claims()
        claims['min_runtime_version'] = '1.1.1'
        self.assert_reason('runtime_version_unsupported', self.envelope(claims))

    def test_v2_domain_and_execution_are_covered_by_real_signature(self):
        self.assert_reason('signature_invalid', self.envelope(domain=b'XTOS-AI-MODEL-v1\x00'))
        raw = json.loads(self.envelope())
        raw['claims']['execution']['cpu_affinity'] = [1]
        self.assert_reason('signature_invalid', raw)
        raw = json.loads(self.envelope())
        raw['schema'] = 'xtos-ai-model-v1'
        self.assert_reason('signature_invalid', raw)
        v1 = model_claims(self.fx.now, 'model.onnx', self.fx.model_bytes, provider='CUDAExecutionProvider')
        self.assert_reason('malformed_envelope', self.envelope(v1, schema='xtos-ai-model-v1', domain=b'XTOS-AI-MODEL-v1\x00'))

    def test_v2_policy_expiry_license_and_revocation_are_not_bypassed(self):
        claims = self.claims()
        claims['license'] = 'not-permitted'
        self.assert_reason('license_not_allowed', self.envelope(claims))
        claims = self.claims()
        claims['expires_at'] = self.fx.now
        self.assert_reason('model_signature_expired', self.envelope(claims))
        ticket = self.fx.policy.authorize(self.envelope(), self.fx.model_root)
        policy = policy_claims(self.fx.now, self.fx.publisher, sequence=2,
                               revoked_models=[ticket.manifest.sha256])
        bundle = signed_envelope('xtos-ai-policy-v1', hashlib.sha256(self.fx.root_signer.public_raw).hexdigest(),
                                 policy, self.fx.root_signer, b'XTOS-AI-POLICY-v1\x00')
        self.fx.policy.install_policy(bundle)
        with self.assertRaises(TrustError) as caught:
            self.fx.policy.check(ticket)
        self.assertEqual(caught.exception.reason, 'model_revoked')

    def test_public_profile_exports_are_explicit(self):
        import xtos_ai_runtime
        self.assertIs(xtos_ai_runtime.ExecutionProfile, protocol.ExecutionProfile)
        self.assertIs(xtos_ai_runtime.validate_execution_profile, protocol.validate_execution_profile)
        self.assertIn('ExecutionProfile', xtos_ai_runtime.__all__)
        self.assertIn('validate_execution_profile', xtos_ai_runtime.__all__)


if __name__ == '__main__':
    unittest.main()
