"""Shared wiring for production provider v2; not accelerated execution evidence."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProviderIntegrationTests(unittest.TestCase):
    def test_trust_and_integration_suites_are_maintained_exactly_once(self):
        runner = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_ai_provider_trust.py', 'test_ai_provider_client.py',
                     'test_ai_provider_worker.py', 'test_ai_recovery_budget.py',
                     'test_ai_provider_install.py', 'test_ai_provider_service_real.py',
                     'test_ai_provider_integration.py'):
            self.assertEqual(runner.count(name), 1, name + ' must run exactly once')

    def test_runtime_sbom_version_matches_provider_contract(self):
        spec = importlib.util.spec_from_file_location('provider_sbom_integration', ROOT / 'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('SBOM generator unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        component = next(row for row in module.COMPONENTS if row['name'] == 'xtos-ai-runtime')
        self.assertEqual(component['version'], '1.1.0')
        self.assertEqual(component['scope'], 'required')
        self.assertEqual(set(component['hash_globs']), {'__init__.py', '__main__.py', 'client.py',
                                                     'protocol.py', 'server.py', 'trust.py', 'worker.py'})


if __name__ == '__main__':
    unittest.main()
