"""Regress the explicit Kbuild alias rule's module-identity override.

Actual generated compiler metadata remains a separate module-build gate.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BCIAliasMetadataTests(unittest.TestCase):
    def test_explicit_alias_sets_final_module_identity(self):
        source = ROOT / 'kernel/linux/drivers/xtos/bci'
        with tempfile.TemporaryDirectory(prefix='bci-alias-') as directory:
            output = Path(directory)
            # An explicit (not pattern) rule has no pattern stem. Exercise the
            # actual recipe's target-specific variable with the observed default.
            fragment = (
                'obj := ' + str(output) + '\n'
                'src := ' + str(source) + '\n'
                'CONFIG_XTOS_BCI := m\n'
                'modname = xtos_bci_core_impl\n'
                'if_changed_rule = @printf "MODULE_ID=%s\\n" "$(modname)"\n'
                'include ' + str(source/'Makefile') + '\n'
                '.PHONY: FORCE\nFORCE:\n'
            )
            result = subprocess.run(['make', '--no-print-directory', '-f', '-',
                                     str(output/'xtos_bci_core_impl.o')],
                                    input=fragment, text=True, capture_output=True,
                                    timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, 'MODULE_ID=xtos_bci_core\n')
            self.assertFalse((output/'xtos_bci_core_impl.o').exists())


if __name__ == '__main__':
    unittest.main()
