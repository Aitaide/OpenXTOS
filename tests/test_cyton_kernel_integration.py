#!/usr/bin/env python3
"""Kernel source/config/patch integration, separate from device acceptance."""
from pathlib import Path
import json
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CytonKernelIntegrationTests(unittest.TestCase):
    def test_serdev_tty_controller_is_reachable_in_maintained_fragment(self):
        config = (ROOT / 'kernel/xtos_defconfig').read_text().splitlines()
        build = (ROOT / 'scripts/build_kernel.sh').read_text()
        # The selected upstream SERIAL_DEV_CTRL_TTYPORT requires built-in bus.
        for required in ('CONFIG_SERIAL_DEV_BUS=y', 'CONFIG_SERIAL_DEV_CTRL_TTYPORT=y'):
            self.assertIn(required, config)
            self.assertIn(required, build)
        kconfig = (ROOT / 'kernel/linux/drivers/tty/serdev/Kconfig').read_text()
        self.assertIn('depends on SERIAL_DEV_BUS != m', kconfig)

    def test_integration_patch_embeds_current_cyton_sources(self):
        pin = json.loads((ROOT / 'kernel/source-pin.json').read_bytes())
        patch = (ROOT / pin['xtos_patch']).read_text()
        sections = re.split(r'(?=^diff --git )', patch, flags=re.M)
        added = {}
        for section in sections:
            if '\n--- /dev/null\n' not in section:
                continue
            match = re.search(r'^\+\+\+ b/(.+)$', section, flags=re.M)
            self.assertIsNotNone(match)
            payload = ''.join(line[1:] for line in section.splitlines(keepends=True)
                              if line.startswith('+') and not line.startswith('+++'))
            added[match.group(1)] = payload
        for path in ('drivers/xtos/bci/devices/openbci_cyton.c',
                     'drivers/xtos/bci/xtos_cyton_packet.h',
                     'drivers/xtos/bci/xtos_cyton_config.h',
                     'drivers/xtos/bci/Makefile',
                     'drivers/xtos/bci/xtos_bci_core.c',
                     'drivers/xtos/bci/xtos_bci_buffer.c',
                     'drivers/xtos/bci/xtos_bci_snapshot.c',
                     'drivers/xtos/bci/xtos_bci_snapshot.h',
                     'include/linux/xtos_bci.h',
                     'include/uapi/linux/xtos_bci.h',
                     'include/uapi/linux/xtos_bci_config.h',
                     'include/uapi/linux/xtos_bci_snapshot.h'):
            with self.subTest(path=path):
                self.assertIn(path, added)
                self.assertEqual(added[path], (ROOT / 'kernel/linux' / path).read_text())

    def test_maintenance_wires_actual_parser_and_driver_regressions(self):
        text = (ROOT / 'scripts/test.sh').read_text()
        for name in ('test_cyton_kernel_parser.sh', 'test_cyton_driver.py',
                     'test_cyton_kernel_integration.py', 'test_cyton_fuzz.py'):
            self.assertIn(name, text)


if __name__ == '__main__':
    unittest.main()
