"""Production bci main dispatches config through the real SDK (OS calls wrapped)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/xtos-cyton'


class MainConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.binary = Path(cls.tmp.name) / 'bci'
        wrapper = Path(cls.tmp.name) / 'boundary.o'
        includes = ['-I' + str(HERE)] + ['-I' + str(ROOT / ('sdk/libxtos-' + name + '/include'))
                                       for name in ('rt', 'signal', 'bci')]
        flags = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread', *includes]
        if os.environ.get('BCI_MAIN_CONFIG_SANITIZE') == '1':
            flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                      '-fno-omit-frame-pointer', '-g', '-no-pie']
        commands = [
            [*flags, '-Dmain=boundary_fixture_unused_main', '-c',
             str(ROOT / 'tests/bci_config_cli_host.c'), '-o', str(wrapper)],
            [*flags, str(HERE / 'main.c'), str(HERE / 'bci_config_cli.c'),
             str(HERE / 'bci_calibration_cli.c'), str(HERE / 'bci_calibration.c'),
             str(HERE / 'bci_diagnostic_cli.c'), str(HERE / 'cyton_diagnostic_serial.c'), str(HERE / 'bci_diagnostic.c'),
             str(HERE / 'bci_stream.c'), str(HERE / 'cyton_serial.c'),
             str(ROOT / 'sdk/libxtos-rt/src/spsc.c'),
             str(ROOT / 'sdk/libxtos-signal/src/signal.c'),
             str(ROOT / 'sdk/libxtos-bci/src/session.c'), str(wrapper),
             '-Wl,--wrap=open', '-Wl,--wrap=close', '-Wl,--wrap=ioctl',
             '-lm', '-o', str(cls.binary)],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            if result.returncode:
                cls.tmp.cleanup()
                raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def invoke(self, args=None, env=None, **kwargs):
        return subprocess.run([str(self.binary), *(args or ['config', '--device', '/mock/bci'])],
                              env={**os.environ, **(env or {})}, capture_output=True,
                              text=True, timeout=3, **kwargs)

    def test_actual_main_config_uses_real_sdk(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['generation'], 123456789012345)
        self.assertEqual(report['gain'], [24] * 8)
        self.assertEqual(len(report['registers']), 24)
        self.assertIn('"quoted\\version"', report['firmware'])

    def test_main_help_advertises_config(self):
        result = self.invoke(['--help'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('bci config --device', result.stdout)

    def test_bad_config_options_have_no_json(self):
        for args in (['config'], ['config', '--device', ''],
                     ['config', '--device', '/mock/bci', '--unknown', 'x']):
            with self.subTest(args=args):
                result = self.invoke(args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')

    def test_main_query_failure_has_no_json(self):
        result = self.invoke(env={'CONFIG_CLI_QUERY_ERROR': '1'})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertIn('config', result.stderr)

    def test_main_malformed_response_has_no_json(self):
        result = self.invoke(env={'CONFIG_CLI_INVALID': '1'})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_main_config_stdout_full_is_failure(self):
        with open('/dev/full', 'w') as full:
            result = subprocess.run([str(self.binary), 'config', '--device', '/mock/bci'],
                                    stdout=full, stderr=subprocess.PIPE, text=True, timeout=3)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('output', result.stderr)


if __name__ == '__main__':
    unittest.main()
