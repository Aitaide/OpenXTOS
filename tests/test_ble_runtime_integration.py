"""Maintain optional BLE provisioning without claiming image or radio inclusion."""
import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BLERuntimeIntegrationTests(unittest.TestCase):
    def test_optional_installer_and_integration_suites_are_maintained_once(self):
        runner = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_ble_runtime_install.py', 'test_ble_runtime_integration.py'):
            with self.subTest(suite=name):
                self.assertEqual(runner.count(name), 1, name + ' missing from maintained runner')

    def test_sbom_binds_optional_provisioner_and_lock_without_image_claim(self):
        spec = importlib.util.spec_from_file_location('ble_runtime_sbom', ROOT / 'tools/xtos-sbom/gen_sbom.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('SBOM generator unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        matches = [row for row in app.COMPONENTS if row['name'] == 'xtos-ble-runtime-provisioner']
        self.assertEqual(len(matches), 1, 'optional BLE provisioner missing from SBOM')
        entry = matches[0]
        self.assertEqual(entry['scope'], 'excluded')
        self.assertEqual(entry['path'], '.')
        expected = ['scripts/install_ble_runtime.py', 'system/packages/ble-runtime-cp313-x86_64.json']
        self.assertEqual(entry['hash_globs'], expected)
        component = app.build_component(entry)
        self.assertEqual(component['scope'], 'excluded')
        self.assertEqual(component['hashes'], [
            {'alg': 'SHA-256', 'content': hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
            for name in expected])


if __name__ == '__main__':
    unittest.main()
