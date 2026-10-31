"""Maintenance and SBOM coverage for the explicit TCP byte transport."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeviceNetworkIntegrationTests(unittest.TestCase):
    def test_maintained_runner_executes_network_suites_once(self):
        runner = (ROOT/'scripts/test.sh').read_text()
        for name in ('test_xtos_network_transport.py', 'test_device_network_install.py',
                     'test_device_network_integration.py'):
            self.assertEqual(runner.count(name), 1, name+' missing from maintained runner')

    def test_installed_network_source_is_bound_by_required_device_sbom(self):
        spec = importlib.util.spec_from_file_location('network_sbom_test', ROOT/'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('SBOM generator unavailable')
        sbom = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sbom)
        component = next(row for row in sbom.COMPONENTS if row['name']=='xtos-device-plugins')
        self.assertEqual(component['scope'], 'required')
        self.assertIn('network_transport.py', component['hash_globs'])


if __name__ == '__main__':
    unittest.main()
