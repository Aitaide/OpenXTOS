"""Nonprivileged acceptance for Live runtime; no host network/mount mutations."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / 'system/live'


def load_script(name):
    path = LIVE / name
    if not path.is_file():
        raise AssertionError(f'missing Live runtime: {path}')
    loader = importlib.machinery.SourceFileLoader(name.replace('-', '_'), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DHCPTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_script('xtos-dhcp')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sysfs = self.base / 'net'
        interface = self.sysfs / 'eth0'
        interface.mkdir(parents=True)
        (interface / 'type').write_text('1\n')
        (interface / 'device').mkdir()
        self.log = self.base / 'commands.jsonl'
        self.ip = self.base / 'ip'
        self.ip.write_text('#!/usr/bin/python3\nimport json, sys\nfrom pathlib import Path\nwith Path(' + repr(str(self.log)) + ').open("a") as out: out.write(json.dumps(sys.argv[1:])+"\\n")\n')
        self.ip.chmod(0o755)
        self.resolv = self.base / 'resolv.conf'
        self.env = dict(interface='eth0', ip='192.0.2.10', subnet='255.255.255.0', router='192.0.2.1', dns='192.0.2.53 2001:db8::53')

    def invoke(self, event='bound'):
        return self.runtime.configure(event, self.env, ip_command=str(self.ip), sysfs=self.sysfs, resolv=self.resolv)

    def test_valid_lease_executes_argv_and_writes_only_nameservers(self):
        self.invoke()
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(['-4', 'addr', 'add', '192.0.2.10/24', 'dev', 'eth0'], commands)
        self.assertIn(['-4', 'route', 'replace', 'default', 'via', '192.0.2.1', 'dev', 'eth0', 'metric', '100'], commands)
        self.assertEqual(self.resolv.read_text(), 'nameserver 192.0.2.53\nnameserver 2001:db8::53\n')

    def test_address_is_added_after_route_flush_so_gateway_stays_on_link(self):
        self.invoke()
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        add = commands.index(['-4', 'addr', 'add', '192.0.2.10/24', 'dev', 'eth0'])
        flush = commands.index(['-4', 'route', 'flush', 'dev', 'eth0'])
        self.assertLess(flush, add, 'route flush must precede addr add or the link route is lost')

    def test_hostile_lease_is_rejected_before_any_effect(self):
        attacks = {'interface': ['eth0;touch /tmp/pwn', '-eth0', '../eth0', 'lo', 'missing'],
                   'ip': ['$(id)', '192.0.2.999', '0.0.0.0', '224.0.0.1'],
                   'subnet': ['255.0.255.0', '24;id', 'badmask'],
                   'router': ['gateway.example', '192.0.2.1;id', '192.0.2.1\n$(id)'],
                   'dns': ['nameserver.example', '192.0.2.53\nsearch attacker', '2001:db8::53%eth0']}
        for field, values in attacks.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    original = self.env[field]
                    self.env[field] = value
                    with self.assertRaises(ValueError):
                        self.invoke()
                    self.env[field] = original
                    self.assertFalse(self.log.exists())
                    self.assertFalse(self.resolv.exists())

    def test_deconfig_does_not_need_lease_and_only_clears_its_interface(self):
        self.env = {'interface': 'eth0'}
        self.invoke('deconfig')
        commands = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(commands, [['-4', 'route', 'flush', 'dev', 'eth0'], ['-4', 'addr', 'flush', 'dev', 'eth0']])

    def test_wireless_and_virtual_interfaces_are_rejected(self):
        (self.sysfs / 'eth0/wireless').mkdir()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertFalse(self.log.exists())


class BootContractTests(unittest.TestCase):
    def test_boot_preserves_readonly_media_and_ram_mounts(self):
        path = LIVE / 'init'
        self.assertTrue(path.is_file(), 'missing Live init')
        text = path.read_text()
        for contract in ['XTOS-LIVE-DEVELOPER-1', 'removable', 'device/type',
                         'rootfs.sha256', 'sha256sum -c', 'ro,nodev,nosuid',
                         'size=50%', 'lowerdir=', 'upperdir=', 'workdir=',
                         '--move /run /newroot/run', 'switch_root /newroot /sbin/xtos-start --init',
                         'XTOS_LIVE_ERROR', 'ambiguous']:
            self.assertIn(contract, text)
        self.assertLess(text.index('sha256sum -c'), text.index('-t squashfs'))
        for forbidden in ['mkfs', 'sfdisk', 'grub-install', 'eval ']:
            self.assertNotIn(forbidden, text)

    def test_boot_explicitly_binds_readonly_loop_before_squashfs(self):
        text = (LIVE / 'init').read_text()
        self.assertIn('losetup -f', text)
        self.assertIn('losetup -r', text)
        self.assertIn('-t squashfs -o ro,nodev "$loop"', text)
        self.assertNotIn('-t squashfs -o loop,', text)
        self.assertLess(text.index('losetup -r'), text.index('-t squashfs'))

    def test_startup_hands_off_to_init_without_sysinit_recursion(self):
        path = LIVE / 'xtos-start'
        self.assertTrue(path.is_file(), 'missing Live startup')
        text = path.read_text()
        for contract in ['--init', 'exec "$BB" init', 'XTOS_READY',
                         'clock status', 'PERSIST=0', 'persistence=$PERSIST', 'udhcpc', 'device', 'wireless',
                         'GOTOOLCHAIN=local', 'GOROOT=/usr/lib/go']:
            self.assertIn(contract, text)
        self.assertNotIn('xtos-ssh &', text)

    def test_live_device_manager_supports_gpu_permissions_and_hotplug(self):
        config = (ROOT / 'system/busybox_defconfig').read_text()
        for setting in ['CONFIG_FEATURE_MDEV_CONF=y', 'CONFIG_FEATURE_MDEV_DAEMON=y', 'CONFIG_FEATURE_MDEV_EXEC=y']:
            self.assertIn(setting, config)
        rules = LIVE / 'mdev.conf'
        self.assertTrue(rules.is_file(), 'DRM access rules missing')
        text = rules.read_text()
        for rule in ['dri/card[0-9]+ 0:video 0660', 'dri/renderD[0-9]+ 0:render 0660',
                     'ttyS[0-9]+|ttyUSB[0-9]+|ttyACM[0-9]+ 0:dialout 0660',
                     'null|zero|full|random|urandom 0:0 0666', 'tty|ptmx 0:5 0666']:
            self.assertIn(rule, text)
        startup = (LIVE / 'xtos-start').read_text()
        self.assertIn('XTOS_DEVICES', startup)
        self.assertIn('mdev -d', startup)
        self.assertLess(startup.index('mdev -d'), startup.index('while IFS= read -r ma; do'))

    def test_console_drops_to_developer_and_ssh_disables_passwords(self):
        console, ssh = LIVE / 'xtos-console', LIVE / 'xtos-ssh'
        self.assertTrue(console.is_file(), 'missing developer console')
        self.assertTrue(ssh.is_file(), 'missing key-only SSH runtime')
        self.assertIn('su -l xtos', console.read_text())
        self.assertNotIn('setuidgid', console.read_text(), 'setuidgid discards supplementary GPU groups')
        text = ssh.read_text()
        self.assertIn('authorized_keys', text)
        self.assertIn('XTOS_SSH_DISABLED', text)
        command = next(line for line in text.splitlines() if line.startswith('exec /usr/sbin/dropbear '))
        for option in ['-F', '-E', '-s', '-g', '-w', '-j', '-k']:
            self.assertIn(option, command.split())
        self.assertIn('/run/xtos-ssh', text)
        self.assertIn('XTOS_SSH_HOSTKEY', text)


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.runtime = load_script('xtos-clock')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fake_chronyd(self, code):
        command = self.base / 'chronyd'
        command.write_text('#!/usr/bin/python3\nimport pathlib, sys\nconfig=pathlib.Path(sys.argv[sys.argv.index("-f")+1]).read_text()\npathlib.Path(' + repr(str(self.base / 'seen.conf')) + ').write_text(config)\nsys.exit(' + str(code) + ')\n')
        command.chmod(0o755)
        return str(command)

    def test_unverified_wall_time_is_never_called_synchronized(self):
        state = self.runtime.status(self.base / 'clock.json')
        self.assertFalse(state['synchronized'])
        self.assertEqual(state['source'], 'unsynchronized')
        self.assertIn('utc', state)

    def test_explicit_sync_has_bounded_command_and_reports_actual_result(self):
        state_path = self.base / 'clock.json'
        result = self.runtime.sync('time.example.org', timeout=5, chronyd=self.fake_chronyd(0), run_dir=self.base, state_path=state_path)
        self.assertEqual(result, 0)
        self.assertIn('server time.example.org iburst\n', (self.base / 'seen.conf').read_text())
        state = self.runtime.status(state_path)
        self.assertEqual(state['source'], 'chronyd-one-shot')
        self.assertFalse(state['synchronized'], 'one-shot clock set is not continuous synchronization')
        self.assertEqual(state['last_sync']['result'], 'success')
        self.assertEqual(list(self.base.glob('xtos-chrony-*')), [], 'private temporary configs must be removed')
        self.assertNotEqual(self.runtime.sync('time.example.org', timeout=5, chronyd=self.fake_chronyd(1), run_dir=self.base, state_path=state_path), 0)
        self.assertEqual(self.runtime.status(state_path)['source'], 'unsynchronized')

    def test_sync_rejects_configuration_injection_before_spawn(self):
        command = self.fake_chronyd(0)
        for server in ['time.example.org\nallow all', '$(id)', '-x', 'x;id', 'bad/name', '2001:db8::1%eth0']:
            with self.subTest(server=server), self.assertRaises(ValueError):
                self.runtime.sync(server, timeout=5, chronyd=command, run_dir=self.base, state_path=self.base / 'clock.json')
        self.assertFalse((self.base / 'seen.conf').exists())
        self.assertFalse((self.base / 'clock.json').exists())


if __name__ == '__main__':
    unittest.main()
