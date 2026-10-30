"""Exercise config CLI with real SDK; wrap only open/close/ioctl."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/xtos-cyton'
SDK = ROOT / 'sdk/libxtos-bci'

class ConfigCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.binary = Path(cls.tmp.name) / 'config-cli'
        cmd = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pedantic',
               '-I' + str(TOOL), '-I' + str(SDK / 'include'),
               str(ROOT / 'tests/bci_config_cli_host.c'),
               str(TOOL / 'bci_config_cli.c'), str(SDK / 'src/session.c'),
               '-Wl,--wrap=open', '-Wl,--wrap=close', '-Wl,--wrap=ioctl',
               '-o', str(cls.binary)]
        sanitizer = os.environ.get('CONFIG_CLI_SANITIZE')
        if sanitizer:
            cmd[1:1] = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                        '-fno-omit-frame-pointer', '-g', '-no-pie']
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            cls.tmp.cleanup()
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_cli(self, args=None, extra_env=None, stdout=subprocess.PIPE):
        env = dict(os.environ)
        env.update(extra_env or {})
        return subprocess.run([str(self.binary), *(args or ['config', '--device', '/mock/bci'])],
                              env=env, text=True, stdout=stdout, stderr=subprocess.PIPE, timeout=5)

    def test_output_failure_is_not_success(self):
        with open('/dev/full', 'w') as output:
            r = self.run_cli(stdout=output)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('output', r.stderr)

    def test_query_errors_have_no_json(self):
        for key in ['CONFIG_CLI_QUERY_ERROR', 'CONFIG_CLI_INVALID']:
            with self.subTest(key=key):
                r = self.run_cli(extra_env={key: '1'})
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertEqual(r.stdout, '')
                self.assertIn('query', r.stderr)

    def test_invalid_options(self):
        for args in [['config'], ['config', '--device', ''],
                     ['config', '--device', '/mock/bci', '--device', 'x'],
                     ['config', '--unknown', 'x']]:
            with self.subTest(args=args):
                r = self.run_cli(args)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, '')

    def test_valid_configuration(self):
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['generation'], 123456789012345)
        self.assertEqual(d['gain'], [24]*8)
        self.assertEqual(d['registers'][5:13], [0x68]*8)
        self.assertEqual(d['firmware'], 'OpenBCI V3 8-16 channel\r\nFirmware: "quoted\\version"\r\n')
        self.assertFalse(d['physical_acceptance'])

if __name__ == '__main__': unittest.main()
