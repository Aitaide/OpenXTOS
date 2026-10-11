#!/usr/bin/env python3
"""Exercise private key-only SSH on existing QEMU file disks; no installation."""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
B = ROOT / '.build/baremetal'
E = B / 'evidence'

def main():
    from verify_baremetal import Guest, DISK
    checks = []
    iso_hash = hashlib.sha256((B / 'images/xtos-private-usb.iso').read_bytes()).hexdigest()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    common = ['ssh', '-F', '/dev/null', '-p', str(port), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
              '-o', 'IdentityAgent=none', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new']
    guest = None
    try:
        for name, iso in [('ssh-installer', True), ('ssh-persistent', False), ('ssh-persistent-reboot', False)]:
            guest = Guest(name, iso=iso, ssh_port=port)
            known = E / (('ssh-installer' if iso else 'ssh-persistent') + '.known_hosts')
            if name != 'ssh-persistent-reboot':
                known.unlink(missing_ok=True)
            opts = common + ['-o', f'UserKnownHostsFile={known}']
            if name == 'ssh-persistent-reboot':
                opts = [value.replace('StrictHostKeyChecking=accept-new', 'StrictHostKeyChecking=yes') for value in opts]
            key = opts + ['-i', str(B / 'keys/maintenance'), 'root@127.0.0.1']
            deadline = time.monotonic() + 60
            while True:
                ready = subprocess.run(key + ["awk '/^Uid:/ {print $2}' /proc/self/status"], capture_output=True, text=True, timeout=10)
                if ready.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('key login failed: ' + ready.stderr)
                time.sleep(1)
            if ready.stdout.strip() != '0':
                raise RuntimeError('maintenance login is not root')
            (E / (name + '.login.log')).write_text(ready.stdout + ready.stderr)
            command = 'grep -qw xtos.installer=1 /proc/cmdline' if iso else 'grep -q xtos.root= /proc/cmdline && grep -qx XTOS_PERSISTENT_TEST /root/persistence-test'
            command += ' && [ "$(cat /sys/kernel/realtime)" = 1 ] && xtos-selftest'
            result = subprocess.run(key + [command], capture_output=True, text=True, timeout=120)
            (E / (name + '.ssh.log')).write_text(result.stdout + result.stderr)
            if result.returncode or 'guest_userspace=pass' not in result.stdout:
                raise RuntimeError('SSH selftest failed: ' + result.stdout + result.stderr)
            denied = subprocess.run(opts + ['-v', '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive', 'root@127.0.0.1', 'id'], capture_output=True, text=True, timeout=15)
            (E / (name + '.rejection.log')).write_text(denied.stdout + denied.stderr)
            offers = [line.split('Authentications that can continue:', 1)[1].strip() for line in denied.stderr.splitlines() if 'Authentications that can continue:' in line]
            if denied.returncode != 255 or not offers or any(value != 'publickey' for value in offers):
                raise RuntimeError('server did not exclusively offer publickey: ' + denied.stderr)
            checks.append({'guest': name, 'key_login_root': True, 'ssh_modules_sdk_selftest': True, 'password_keyboard_interactive_rejected': True, 'server_authentication_offers': offers, 'persistent_host_key_verified': name == 'ssh-persistent-reboot'})
            guest.stop(reboot=name == 'ssh-persistent'); guest = None
        report = {'results': checks, 'iso_sha256': iso_hash, 'disk': str(DISK), 'host_forward': f'127.0.0.1:{port}', 'scope': 'QEMU file-backed NVMe only; existing disk retained; private key never copied or printed'}
        (E / 'ssh-results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    finally:
        if guest:
            guest.close()

if __name__ == '__main__':
    main()
