"""Installation surface for the real serial plugin, independent of fixture tests."""
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DeviceSerialInstallTests(unittest.TestCase):
    def test_source_only_install_runs_exact_api_and_preserves_existing_destinations(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        self.assertIsNotNone(importlib.util.find_spec('install_devices'),
                             'bounded real serial plugin installer missing')
        installer = importlib.import_module('install_devices')
        with tempfile.TemporaryDirectory() as scratch:
            stage = Path(scratch)
            result = installer.install(stage, ROOT)
            self.assertEqual(result['schema'], 'xtos-device-plugin-source-inventory-v1')
            self.assertFalse(result['physical_device_evidence'])
            self.assertFalse(result['fixture_or_test_sources_installed'])
            expected = {'xtos_devices.py', 'serial_transport.py', 'cyton_protocol.py',
                        'cyton_serial_plugin.py', 'openbci_cyton.py'}
            package = stage/'usr/lib/xtos/devices'
            self.assertEqual({p.name for p in package.iterdir()}, expected)
            code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                    'from cyton_serial_plugin import CytonSerialPlugin; '
                    'from serial_transport import LinuxSerialTransport; '
                    'from cyton_protocol import CytonProtocolError; '
                    '\nif LinuxSerialTransport("/dev/ttyUSB0").connected: '
                    'raise RuntimeError("constructor opened a tty")'
                    '\nprint("INSTALLED_DEVICE_API=PASS")')
            run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(package)],
                                 capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('INSTALLED_DEVICE_API=PASS', run.stdout)
            self.assertEqual(installer.install(stage, ROOT), result)
            member = package/'serial_transport.py'
            member.write_bytes(b'caller sentinel\n')
            with self.assertRaises(ValueError):
                installer.install(stage, ROOT)
            self.assertEqual(member.read_bytes(), b'caller sentinel\n')
            self.assertFalse((stage/'var/lib/xtos/action-gate').exists())
            stored = json.loads((stage/'usr/share/xtos/device-plugin-sources.json').read_text())
            self.assertEqual(stored, result)


    def test_competing_package_publication_preserves_caller_directory(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        installer = importlib.import_module('install_devices')
        with tempfile.TemporaryDirectory() as scratch:
            stage = Path(scratch)
            publish = installer._publish_noreplace
            def compete(source, destination):
                if destination == stage/installer.DESTINATION:
                    destination.mkdir()
                    (destination/'sentinel').write_text('caller')
                return publish(source, destination)
            with patch.object(installer, '_publish_noreplace', side_effect=compete):
                with self.assertRaises(FileExistsError):
                    installer.install(stage, ROOT)
            package = stage/installer.DESTINATION
            self.assertEqual({p.name for p in package.iterdir()}, {'sentinel'})
            self.assertEqual((package/'sentinel').read_text(), 'caller')
            self.assertFalse((stage/installer.RECORD).exists())
            self.assertEqual(list(stage.glob('.xtos-devices-*')), [])

    def test_conflicting_record_is_rejected_before_package_publication(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        installer = importlib.import_module('install_devices')
        with tempfile.TemporaryDirectory() as scratch:
            stage = Path(scratch)
            record = stage/installer.RECORD
            record.parent.mkdir(parents=True)
            record.write_bytes(b'caller sentinel')
            with self.assertRaises(ValueError):
                installer.install(stage, ROOT)
            self.assertEqual(record.read_bytes(), b'caller sentinel')
            self.assertFalse((stage/installer.DESTINATION).exists())

    def test_symlinked_destination_ancestor_is_rejected_before_external_write(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        installer = importlib.import_module('install_devices')
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            stage, outside = work/'stage', work/'outside'
            stage.mkdir()
            outside.mkdir()
            (stage/'usr').mkdir()
            (stage/'usr/lib').symlink_to(outside, target_is_directory=True)
            (outside/'sentinel').write_text('caller')
            with self.assertRaises(ValueError, msg='symlinked ancestor allowed an out-of-root install'):
                installer.install(stage, ROOT)
            self.assertEqual({p.name for p in outside.iterdir()}, {'sentinel'})
            self.assertEqual((outside/'sentinel').read_text(), 'caller')

    def test_peerless_fifo_member_is_refused_without_read_or_destination_change(self):
        sys.path.insert(0, str(ROOT/'scripts'))
        installer = importlib.import_module('install_devices')
        import os
        with tempfile.TemporaryDirectory() as scratch:
            stage = Path(scratch)
            installer.install(stage, ROOT)
            member = stage/'usr/lib/xtos/devices/serial_transport.py'
            member.unlink()
            os.mkfifo(member)
            before = member.lstat()
            code = ('import sys;sys.path.insert(0,sys.argv[1]);import install_devices;'
                    '\ntry: install_devices.install(sys.argv[2],sys.argv[3])'
                    '\nexcept ValueError: print("FIFO_REJECTED")'
                    '\nelse: raise RuntimeError("FIFO accepted")')
            run = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT/'scripts'),
                                  str(stage), str(ROOT)], capture_output=True, text=True, timeout=3)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('FIFO_REJECTED', run.stdout)
            self.assertEqual((member.lstat().st_dev, member.lstat().st_ino),
                             (before.st_dev, before.st_ino))


if __name__ == '__main__':
    unittest.main()
