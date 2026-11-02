"""Exercise maintained TLS cases against exact installed source modules."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeviceTLSInstallTests(unittest.TestCase):
    def test_installed_tls_source_runs_real_loopback_and_legacy_api(self):
        spec = importlib.util.spec_from_file_location('tls_test_installer', ROOT / 'scripts/install_devices.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('installer module unavailable')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        self.assertEqual(app.SOURCES.get('tls_transport.py'), 'tools/xtos-devices/tls_transport.py',
                         'verified TLS source missing from installed device API')
        # Even a successful child must retain its real execution when requested.
        evidence = os.environ.get('XTOS_TLS_INSTALL_EVIDENCE')
        with tempfile.TemporaryDirectory(prefix='tls-installed-') as tmp:
            stage = Path(tmp)
            receipt = app.install(stage, ROOT)
            package = stage / app.DESTINATION
            for name in ('network_transport.py', 'tls_transport.py'):
                actual = package / name
                expected = (ROOT / 'tools/xtos-devices' / name).read_bytes()
                self.assertEqual(actual.read_bytes(), expected)
                self.assertEqual(receipt['source_sha256'][name], hashlib.sha256(expected).hexdigest())
                self.assertEqual(actual.stat().st_mode & 0o7777, 0o644)
            self.assertIs(receipt['physical_device_evidence'], False)
            self.assertIs(receipt['authority_initialized'], False)
            self.assertIs(receipt['fixture_or_test_sources_installed'], False)
            code = '''import json, pathlib, runpy, sys, unittest
package, source_tests = pathlib.Path(sys.argv[1]).resolve(), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(package))
import tls_transport, network_transport
for module in (tls_transport, network_transport):
    if pathlib.Path(module.__file__).resolve().parent != package:
        raise RuntimeError('checkout module shadowed installed API')
namespace = runpy.run_path(str(source_tests), run_name='installed_tls_cases')
suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace['TLSTransportTests'])
result = unittest.TextTestRunner(verbosity=2).run(suite)
for module in (tls_transport, network_transport):
    if pathlib.Path(module.__file__).resolve().parent != package:
        raise RuntimeError('installed API changed origin')
print(json.dumps(dict(status='pass' if result.wasSuccessful() else 'fail',
    methods=result.testsRun, skipped=len(result.skipped), optimization=sys.flags.optimize,
    tls_module=tls_transport.__file__, tcp_module=network_transport.__file__)))
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
            for flags in ([], ['-O']):
                with self.subTest(flags=flags):
                    run = subprocess.run([sys.executable, *flags, '-B', '-I', '-c', code,
                        str(package), str(ROOT / 'tests/test_xtos_tls_transport.py')],
                        capture_output=True, text=True, timeout=60)
                    if evidence:
                        target = Path(evidence)
                        target.mkdir(parents=True, exist_ok=True)
                        with (target / ('optimized.json' if flags else 'normal.json')).open('x') as stream:
                            json.dump(dict(command=run.args, exit=run.returncode,
                                stdout=run.stdout, stderr=run.stderr, source_inventory=receipt), stream, indent=2)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(run.stdout)
                    self.assertEqual(result['status'], 'pass')
                    self.assertEqual(result['methods'], 14)
                    self.assertEqual(result['skipped'], 0)
                    self.assertEqual(result['optimization'], int(bool(flags)))
                    self.assertEqual(Path(result['tls_module']), package / 'tls_transport.py')
                    self.assertEqual(Path(result['tcp_module']), package / 'network_transport.py')
            self.assertEqual(app.install(stage, ROOT), receipt)
            self.assertEqual(json.loads((stage / app.RECORD).read_bytes()), receipt)


if __name__ == '__main__':
    unittest.main()
