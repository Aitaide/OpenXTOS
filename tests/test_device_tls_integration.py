"""Maintenance and SBOM coverage of explicit verified TLS source."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeviceTLSIntegrationTests(unittest.TestCase):
    def test_tls_source_and_installation_suites_are_maintained_once(self):
        runner = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_xtos_tls_transport.py', 'test_device_tls_install.py',
                     'test_device_tls_integration.py'):
            self.assertEqual(runner.count(name), 1, name + ' missing from maintained runner')

    def test_installed_tls_source_is_bound_by_required_sbom(self):
        spec = importlib.util.spec_from_file_location('tls_sbom_test', ROOT / 'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('SBOM generator unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        component = next(row for row in app.COMPONENTS if row['name'] == 'xtos-device-plugins')
        self.assertEqual(component['scope'], 'required')
        self.assertIn('tls_transport.py', component['hash_globs'])


if __name__ == '__main__':
    unittest.main()
