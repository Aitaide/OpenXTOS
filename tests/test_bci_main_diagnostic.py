"""Exercise the frozen diagnostic contract through the production main dispatcher."""
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'tools/xtos-cyton'
spec = importlib.util.spec_from_file_location('diagnostic_contract', ROOT / 'tests/test_bci_diagnostic_cli.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
contract.E = ROOT / '.build/xtos-roadmap-v1/main-diagnostic' / os.environ.get('BCI_DIAGNOSTIC_CLI_SANITIZE', '0')


class MainDiagnosticTests(contract.DiagnosticCliTests):
    @classmethod
    def setUpClass(cls):
        contract.E.mkdir(parents=True, exist_ok=True)
        cls.exe = contract.E / 'bci'
        flags = ['-O1', '-g']
        if os.environ.get('BCI_DIAGNOSTIC_CLI_SANITIZE') == '1':
            flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                      '-fno-omit-frame-pointer', '-no-pie']
        includes = ['-I' + str(HERE)] + ['-I' + str(ROOT / ('sdk/libxtos-' + n + '/include'))
                                       for n in ('rt', 'signal', 'bci')]
        common = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread', *flags, *includes]
        obj = contract.E / 'production-main.o'
        # Preserve the exact behavioral assertions and OS-boundary fault wrappers.
        # Only the wrapper's entrypoint changes to the actual production main.
        harness = contract.MAIN.replace('#include "bci_diagnostic_cli.h"',
                                        'int bci_production_main(int, char **);')
        harness = harness.replace('bci_diagnostic_cli_main(argc, argv)', 'bci_production_main(argc, argv)')
        wrapper = contract.E / 'boundary.c'
        wrapper.write_text(harness)
        commands = [
            [*common, '-Dmain=bci_production_main', '-c', str(HERE / 'main.c'), '-o', str(obj)],
            [*common, str(wrapper), str(obj), *[str(HERE / p) for p in
                ('bci_stream.c', 'cyton_serial.c', 'bci_config_cli.c',
                 'bci_calibration_cli.c', 'bci_calibration.c', 'bci_diagnostic_cli.c',
                 'cyton_diagnostic_serial.c', 'bci_diagnostic.c')],
             str(ROOT / 'sdk/libxtos-rt/src/spsc.c'),
             str(ROOT / 'sdk/libxtos-signal/src/signal.c'),
             str(ROOT / 'sdk/libxtos-bci/src/session.c'), '-Wl,--wrap=write', '-lm', '-o', str(cls.exe)],
        ]
        for index, command in enumerate(commands):
            result = subprocess.run(command, capture_output=True, text=True, timeout=25)
            (contract.E / ('compile-' + str(index) + '.log')).write_text(
                ' '.join(command) + '\n' + result.stdout + result.stderr)
            if result.returncode:
                raise AssertionError(result.stderr)

    def test_main_help_advertises_diagnostic_command(self):
        result = subprocess.run([str(self.exe), '--help'], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('bci diagnose --device', result.stdout)
        self.assertIn('shorted|impedance-p|impedance-n', result.stdout)


if __name__ == '__main__':
    unittest.main()
