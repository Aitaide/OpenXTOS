"""XR must pass its target execution gate before Live packaging."""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LiveXRIntegrationTests(unittest.TestCase):
    def test_builder_installs_and_verifies_xr_before_image_packaging(self):
        source = (ROOT / 'scripts/build_live.py').read_text()
        tree = ast.parse(source)
        imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                   and n.module == 'live_xr']
        self.assertEqual(len(imports), 1)
        self.assertIn(('install_and_verify', 'install_xr_runtime'),
                      [(a.name, a.asname) for a in imports[0].names])
        build = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'build')
        calls = [n for n in ast.walk(build) if isinstance(n, ast.Call)]
        selected = [n for n in calls if isinstance(n.func, ast.Name)
                    and n.func.id == 'install_xr_runtime']
        self.assertEqual(len(selected), 1)
        self.assertEqual([ast.unparse(n) for n in selected[0].args], ['runtime', 'ROOT'])
        squash = [n for n in calls if 'mksquashfs' in ast.unparse(n)
                  and isinstance(n.func, ast.Name) and n.func.id == 'run']
        self.assertEqual(len(squash), 1)
        self.assertLess(selected[0].lineno, squash[0].lineno)
        assignments = [n for n in ast.walk(build) if isinstance(n, ast.Assign)
                       and any(ast.unparse(t) == "build_manifest['xr_runtime']"
                               for t in n.targets)]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(ast.unparse(assignments[0].value), 'xr_runtime')

    def test_installed_guest_xr_checks_run_in_live_verifier(self):
        source = (ROOT / 'scripts/verify_live.py').read_text()
        self.assertIn('live_xr_guest_check.py', source)
        self.assertIn("rows['xr_in_guest']", source)
        self.assertIn("'safe_action'", source)
        self.assertIn("'authority_store_created'", source)
        self.assertIn('runtime_unverified', source)

    def test_guest_report_requires_typed_no_action_receipts(self):
        spec = importlib.util.spec_from_file_location('xr_guest_verifier', ROOT / 'scripts/verify_live.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('Live verifier unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        native = dict(status='unavailable', safe_action='no_action', physical_acceptance=False)
        demo = dict(safe_action='no_action', physical_acceptance=False, runtime_mode='simulated')
        report = {
            'authority_store_created': False,
            'python': dict(executable='/usr/bin/python3', implementation='cpython'),
            'commands': {
                'native_missing_loader': dict(argv=['/usr/bin/xr-probe', '--loader', '/__xtos_live_xr_missing_loader__.so'], exit=3, stdout=json.dumps(native), stderr=''),
                'eye_missing_loader': dict(argv=['/usr/bin/xr-eye-probe', '--loader', '/__xtos_live_xr_missing_loader__.so', '--accept-eye-data'], exit=1,
                    stdout=json.dumps(dict(status='runtime_unavailable', safe_action='no_action', physical_acceptance=False,
                        source_kind='runtime_unverified', sample_count=0, samples=[])), stderr=''),
                'launcher_argv': dict(argv=['/usr/bin/xr-feedback-demo', '--not-a-demo'], exit=2, stdout='', stderr='usage: xr-feedback-demo'),
                'demo': dict(argv=['/usr/bin/xr-feedback-demo', '--demo'], exit=0, stdout=json.dumps(demo), stderr=''),
            },
        }
        self.assertEqual(module.validate_xr_guest_report(copy.deepcopy(report)), report)
        mutations = []
        for key in ('native_missing_loader', 'eye_missing_loader', 'demo'):
            for value in (0, 0.0, None, True):
                changed = copy.deepcopy(report)
                doc = json.loads(changed['commands'][key]['stdout'])
                doc['physical_acceptance'] = value
                changed['commands'][key]['stdout'] = json.dumps(doc)
                mutations.append(changed)
        for value in (False, 0.0):
            changed = copy.deepcopy(report)
            changed['commands']['demo']['exit'] = value
            mutations.append(changed)
        for changed in mutations:
            with self.subTest(report=changed):
                with self.assertRaises(ValueError):
                    module.validate_xr_guest_report(changed)

    def test_boot_acceptance_requires_xr_when_artifacts_declare_it(self):
        spec = importlib.util.spec_from_file_location('xr_boot_acceptance', ROOT / 'scripts/verify_live_acceptance.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('Live acceptance verifier unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            rows = []
            for mode in module.MODES:
                row = {'mode': mode, 'status': 'pass', 'checks': {
                    'internal_disk_sha256_unchanged': True,
                    'host_key_trusted_via_serial': True,
                    'authentication_offers': ['publickey'],
                    'root_mounts': 'LIVE_MOUNTS=PASS',
                    'sdk_selftest': 'guest_userspace=pass',
                    'developer_tools': 'DEVELOPER_USER=PASS',
                    'readonly_media': 'LIVE_READONLY=PASS',
                }}
                rows.append(row)
                (evidence / (mode + '.result.json')).write_text(json.dumps(row))
                (evidence / (mode + '.serial.log')).write_text('XTOS_READY mode=live\nXTOS_DHCP interface=eth0\n')
            (evidence / 'results.json').write_text(json.dumps({
                'schema_version': 1, 'status': 'pass', 'iso_sha256': 'a' * 64, 'modes': rows}))
            artifacts = {'iso_sha256': 'a' * 64, 'xr_runtime_required': True}
            with self.assertRaisesRegex(ValueError, 'XR'):
                module.verify_boot_evidence(evidence, artifacts)

    def test_current_xr_suites_are_maintained_once(self):
        source = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_live_xr.py', 'test_live_xr_integration.py',
                     'test_xr_eye_session.py', 'test_xr_eye_spec.py', 'test_xr_eye_cli.py',
                     'test_current_delivery_status.py'):
            with self.subTest(suite=name):
                self.assertEqual(source.count(name), 1)


if __name__ == '__main__':
    unittest.main()
