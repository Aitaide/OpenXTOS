"""Run the frozen file-command behavioral suite through production bci main."""
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/xtos-cyton'
spec = importlib.util.spec_from_file_location('calibration_file_contract', ROOT / 'tests/test_bci_calibration_cli.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
contract.E = ROOT / '.build/xtos-roadmap-v1/main-calibration' / os.environ.get('BCI_TEST_MODE', 'normal')


class MainCalibrationTests(contract.CalibrationCLI):
    @classmethod
    def setUpClass(cls):
        contract.E.mkdir(parents=True, exist_ok=True)
        cls.binary = contract.E / 'bci'
        flags = ['-O2']
        if os.environ.get('BCI_TEST_MODE') == 'sanitize':
            flags = ['-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                     '-fno-omit-frame-pointer', '-no-pie']
        includes = ['-I' + str(ROOT / ('sdk/libxtos-' + n + '/include'))
                    for n in ('rt', 'signal', 'bci')]
        command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread', *flags, *includes,
                   *[str(HERE / p) for p in ('main.c', 'bci_stream.c', 'cyton_serial.c',
                                            'bci_config_cli.c', 'bci_calibration_cli.c', 'bci_calibration.c',
                                            'bci_diagnostic_cli.c', 'cyton_diagnostic_serial.c', 'bci_diagnostic.c')],
                   str(ROOT / 'sdk/libxtos-rt/src/spsc.c'),
                   str(ROOT / 'sdk/libxtos-signal/src/signal.c'),
                   str(ROOT / 'sdk/libxtos-bci/src/session.c'), '-lm', '-o', str(cls.binary)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=25)
        (contract.E / 'compile.log').write_text(' '.join(command) + '\n' + result.stdout + result.stderr)
        if result.returncode:
            raise AssertionError(result.stderr)

    def test_main_help_advertises_calibration_commands(self):
        result = subprocess.run([str(self.binary), '--help'], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('bci calibrate --points', result.stdout)
        self.assertIn('bci apply-calibration --model', result.stdout)


if __name__ == '__main__':
    unittest.main()
