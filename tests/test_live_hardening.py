#!/usr/bin/env python3
"""Exercise Live security wiring without touching the host network."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LiveSecurityTests(unittest.TestCase):
    def guard_scene(self, root, failure=False):
        for relative in ['usr/bin', 'etc/xtos', 'run']:
            (root / relative).mkdir(parents=True, exist_ok=True)
        # Execute unchanged guard logic with only its three literal paths relocated.
        source = (ROOT / 'system/live/xtos-net-guard').read_text()
        for old, new in [('FW=/usr/bin/fw', 'FW=' + str(root / 'usr/bin/fw')),
                         ('READY=/run/xtos-security.ready', 'READY=' + str(root / 'run/xtos-security.ready')),
                         ('POLICY=/etc/xtos/ssh-allow-from', 'POLICY=' + str(root / 'etc/xtos/ssh-allow-from'))]:
            self.assertEqual(source.count(old), 1)
            source = source.replace(old, new)
        (root / 'guard').write_text(source)
        fw = root / 'usr/bin/fw'
        fw.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >>' + str(root / 'run/fw.calls') + '\n' +
                      ('exit 7\n' if failure else 'if [ "$1" = status ]; then echo "XTOS_FW_STATUS enabled=true table=xtos_fw policy=drop"; fi\nexit 0\n'))
        fw.chmod(0o755)

    def test_guard_failure_clears_stale_readiness_and_refuses(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            self.guard_scene(root, failure=True)
            (root / 'run/xtos-security.ready').write_text('stale')
            result = subprocess.run(['/bin/sh', str(root / 'guard')],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'run/xtos-security.ready').exists())
            self.assertEqual((root / 'run/fw.calls').read_text(), 'enable\n')

    def test_guard_does_not_accept_false_status_with_zero_exit(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            self.guard_scene(root)
            (root / 'usr/bin/fw').write_text('#!/bin/sh\nif [ "$1" = status ]; then echo "XTOS_FW_STATUS enabled=false"; fi\nexit 0\n')
            result = subprocess.run(['/bin/sh', str(root / 'guard')],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, 'false firewall status was accepted')
            self.assertFalse((root / 'run/xtos-security.ready').exists())

    def test_guard_requires_one_exact_canonical_effective_status(self):
        cases = [
            'XTOS_FW_STATUS enabled=false diagnostic=not_enabled=true',
            'XTOS_FW_STATUS enabled=true table=xtos_fw policy=accept',
            'XTOS_FW_STATUS enabled=true table=xtos_fw policy=drop extra=unchecked',
            'diagnostic enabled=true',
            'XTOS_FW_STATUS enabled=true table=xtos_fw policy=drop\nXTOS_FW_STATUS enabled=false',
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
                root = Path(temporary)
                self.guard_scene(root)
                (root / 'usr/bin/fw').write_text('#!/bin/sh\nif [ "$1" = status ]; then cat <<\'STATUS\'\n' + payload + '\nSTATUS\nfi\nexit 0\n')
                result = subprocess.run(['/bin/sh', str(root / 'guard')],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0, 'noncanonical firewall status was accepted')
                self.assertFalse((root / 'run/xtos-security.ready').exists())

    def test_guard_success_binds_explicit_management_rule_before_readiness(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temporary:
            root = Path(temporary)
            self.guard_scene(root)
            (root / 'etc/xtos/ssh-allow-from').write_text('192.0.2.0/24\n')
            result = subprocess.run(['/bin/sh', str(root / 'guard')],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'run/fw.calls').read_text().splitlines(),
                             ['enable', 'allow --proto tcp --port 22 --from 192.0.2.0/24', 'status'])
            self.assertEqual((root / 'run/xtos-security.ready').read_text(), 'firewall=ready\n')

    def module(self):
        spec = importlib.util.spec_from_file_location('live_hardening', ROOT / 'scripts/build_live.py')
        if spec is None or spec.loader is None:
            raise RuntimeError('Live builder module is unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_management_cidr_is_explicit_and_canonical(self):
        module = self.module()
        validator = getattr(module, 'management_networks', None)
        if not callable(validator):
            self.fail('builder has no explicit management-source contract')
        self.assertEqual(validator(['192.0.2.4/24', '2001:db8::1/64']),
                         ['192.0.2.0/24', '2001:db8::/64'])
        for invalid in ['0.0.0.0/0', '::/0', '192.0.2.1;id', 'localhost', '']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validator([invalid])
        with self.assertRaises(ValueError):
            validator(['192.0.2.1/32'] * 17)

    def test_runtime_installs_the_network_guard(self):
        module = self.module()
        self.assertEqual(module.RUNTIME_FILES.get('xtos-net-guard'),
                         'usr/libexec/xtos/xtos-net-guard')

    def test_kernel_contract_requires_effective_protection_prerequisites(self):
        module = self.module()
        fragment = (ROOT / 'kernel/xtos_defconfig').read_text().splitlines()
        required = ['CONFIG_STACKPROTECTOR_STRONG=y', 'CONFIG_FORTIFY_SOURCE=y',
                    'CONFIG_HARDENED_USERCOPY=y', 'CONFIG_SLAB_FREELIST_HARDENED=y',
                    'CONFIG_SLAB_FREELIST_RANDOM=y', 'CONFIG_SECURITY_DMESG_RESTRICT=y',
                    'CONFIG_SECURITY_YAMA=y', 'CONFIG_MEMCG=y',
                    'CONFIG_CGROUP_PIDS=y', 'CONFIG_PSTORE=y',
                    'CONFIG_PSTORE_RAM=y']
        for setting in required:
            with self.subTest(setting=setting):
                self.assertIn(setting, fragment)
                self.assertIn(setting, module.REQUIRED_CONFIG)

    def test_ssh_never_accepts_root_public_key_login(self):
        text = (ROOT / 'system/live/xtos-ssh').read_text()
        command = next(line for line in text.splitlines() if line.startswith('exec /usr/sbin/dropbear '))
        self.assertIn('-w', command.split(), 'Dropbear still permits root public-key login')

    def test_security_regressions_are_registered_in_complete_maintenance(self):
        text = (ROOT / 'scripts/test.sh').read_text()
        for suite in ['test_native_fw_security.py', 'test_native_tools_fw_extended.py',
                      'test_update_apply_security.py', 'test_update_recovery.py',
                      'test_live_hardening.py']:
            with self.subTest(suite=suite):
                self.assertIn(suite, text, 'security regression omitted from maintained test runner')

    def test_network_guard_runs_before_interface_activation(self):
        text = (ROOT / 'system/live/xtos-start').read_text()
        guard = text.find('/usr/libexec/xtos/xtos-net-guard')
        network = text.index('/usr/sbin/ip link set lo up')
        self.assertGreaterEqual(guard, 0, 'network has no fail-closed guard')
        self.assertLess(guard, network)
        self.assertNotIn("xtos-net-guard || echo", text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
