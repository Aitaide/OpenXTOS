#!/usr/bin/env python3
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class BaremetalTests(unittest.TestCase):
    def test_required_hardware_drivers_are_explicit(self):
        config = (ROOT / 'kernel/xtos_baremetal_x86.config').read_text()
        for setting in ['CONFIG_BLK_DEV_NVME=y', 'CONFIG_IXGBE=y', 'CONFIG_EXT4_FS=y', 'CONFIG_EFI_PARTITION=y', 'CONFIG_NR_CPUS=256']:
            self.assertIn(setting, config)

    def test_x722_driver_is_built_in_and_matches_target_pci_id(self):
        fragment = (ROOT / 'kernel/xtos_baremetal_x86.config').read_text().splitlines()
        self.assertIn('CONFIG_I40E=y', fragment)
        resolved = ROOT / '.build/baremetal/kernel-build/.config'
        self.assertTrue(resolved.is_file(), 'run scripts/build_baremetal.sh first')
        settings = resolved.read_text().splitlines()
        for setting in ['CONFIG_I40E=y', 'CONFIG_PCI=y', 'CONFIG_AUXILIARY_BUS=y']:
            self.assertIn(setting, settings)
        # Built-in drivers do not emit MODULE_DEVICE_TABLE aliases on this kernel.
        # Inspect the PCI table linked into vmlinux instead of assuming module metadata.
        import re
        import struct
        kernel = ROOT / '.build/baremetal/kernel-build/vmlinux'
        symbols = subprocess.check_output(['nm', '-S', str(kernel)], text=True)
        symbol = next(line.split() for line in symbols.splitlines() if line.endswith(' i40e_pci_tbl'))
        address, size = (int(value, 16) for value in symbol[:2])
        dump = subprocess.check_output(['objdump', '-s', f'--start-address={address}',
                                        f'--stop-address={address + size}', str(kernel)], text=True)
        data = bytes.fromhex(''.join(word for line in dump.splitlines()
                                   if re.match(r'^\s+[0-9a-f]+\s', line)
                                   for word in line.split()[1:5] if re.fullmatch(r'[0-9a-f]{8}', word)))
        self.assertEqual(len(data), size)
        # Linux 6.6 x86_64 pci_device_id is 40 bytes (includes override_only).
        ids = [struct.unpack_from('<II', data, offset) for offset in range(0, size, 40)]
        self.assertIn((0x8086, 0x37D2), ids, 'linked i40e must claim the observed X722')

    def test_installer_rejects_missing_identity_without_writes(self):
        script = ROOT / 'system/baremetal/xtos-install'
        result = subprocess.run(['sh', str(script), '/dev/not-a-disk'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('usage:', result.stderr)

    def test_install_requires_serial_capacity_and_confirmation(self):
        script = (ROOT / 'system/baremetal/xtos-install').read_text()
        for guard in ['EXPECTED_SERIAL', 'EXPECTED_BYTES', 'ERASE-XTOS', 'xtos.installer=1', '/sys/class/block', 'XTOS_INSTALL=PASS']:
            self.assertIn(guard, script)
        self.assertLess(script.index('EXPECTED_BYTES'), script.index('sfdisk --wipe'))
        self.assertIn('grub-install --target=x86_64-efi', script)
        self.assertIn('--no-nvram --removable', script)

    def test_boot_uses_persistent_root_uuid(self):
        init = (ROOT / 'system/baremetal/init').read_text()
        self.assertIn('xtos.root=', init)
        self.assertIn('switch_root', init)
        self.assertIn('xtos.installer=1', init)

    def test_generated_iso_boot_menu_preserves_explicit_install_mode(self):
        import subprocess
        iso = ROOT / '.build/baremetal/images/xtos-private-usb-x722.iso'
        self.assertTrue(iso.is_file(), 'run scripts/build_baremetal.sh first')
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / 'grub.cfg'
            subprocess.run(
                ['xorriso', '-osirrox', 'on', '-indev', str(iso),
                 '-extract', '/boot/grub/grub.cfg', str(target)],
                capture_output=True, text=True, check=True)
            cfg = target.read_text()
        self.assertIn("menuentry 'XTOS installer / maintenance", cfg)
        self.assertIn('linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.installer=1', cfg)
        self.assertIn("menuentry 'XTOS recovery (installation disabled)", cfg)
        self.assertNotIn("recovery (installation disabled)' {\n linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.installer=1", cfg)

    def test_network_maintenance_is_key_only(self):
        startup = (ROOT / 'system/baremetal/xtos-start').read_text()
        self.assertIn('dropbear', startup)
        self.assertIn('-s', startup)
        self.assertIn('authorized_keys', startup)
        self.assertNotIn('telnetd', startup)

if __name__ == '__main__':
    unittest.main()
