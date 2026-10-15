#!/usr/bin/env python3
"""Real BIOS/UEFI ISO install tests. Only creates file-backed QEMU disks."""
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
B = Path(os.environ.get('XTOS_BAREMETAL_BUILD_ROOT', str(ROOT / '.build/baremetal')))
INSTALLER_ISO = Path(os.environ.get('XTOS_INSTALLER_ISO', str(B / 'images/xtos-installer-public-x86_64.iso')))
E = B / 'evidence'
E.mkdir(parents=True, exist_ok=True)
DISK = E / 'test-nvme.raw'
SERIAL = 'XTOS-QEMU-TEST-0001'
SIZE = 3 * 1024**3
TIMEOUT = int(os.environ.get('QEMU_TIMEOUT', '300'))

class Guest:
    def __init__(self, name, iso=False, efi=False, ssh_port=None):
        self.log = open(E / (name + '.serial.log'), 'wb')
        self.buffer = b''
        self.path = E / (name + '.sock')
        self.path.unlink(missing_ok=True)
        args = ['qemu-system-x86_64', '-machine', 'q35', '-accel', 'tcg', '-m', '2048', '-smp', '2', '-display', 'none', '-monitor', 'none', '-no-reboot', '-serial', f'unix:{self.path},server=on,wait=off', '-drive', f'file={DISK},format=raw,if=none,id=nvm', '-device', f'nvme,drive=nvm,serial={SERIAL}', '-netdev', 'user,id=net', '-device', 'e1000e,netdev=net']
        if ssh_port is not None:
            args[args.index('-netdev') + 1] = f'user,id=net,hostfwd=tcp:127.0.0.1:{ssh_port}-:22'
        if iso:
            args += ['-cdrom', str(INSTALLER_ISO), '-boot', 'd']
        if efi:
            shutil.copy2('/usr/share/OVMF/OVMF_VARS_4M.fd', E / (name + '.vars.fd'))
            args += ['-drive', 'if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd', '-drive', f'if=pflash,format=raw,file={E / (name + ".vars.fd")}']
        self.err = open(E / (name + '.qemu.log'), 'wb')
        self.process = subprocess.Popen(args, stdout=self.err, stderr=self.err)
        self.sock = socket.socket(socket.AF_UNIX)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    self.sock.connect(str(self.path)); break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline: raise RuntimeError('serial socket unavailable')
                    time.sleep(.1)
            self.wait(b'XTOS_READY=1')
            time.sleep(1)
            self.sock.sendall(b'\n')

        except BaseException:
            self.close()
            raise

    def wait(self, needle):
        deadline = time.monotonic() + TIMEOUT
        while needle not in self.buffer:
            if time.monotonic() > deadline:
                raise RuntimeError(f'timed out waiting {needle!r}; see {self.log.name}')
            if not select.select([self.sock], [], [], 1)[0]: continue
            data = self.sock.recv(65536)
            if not data: raise RuntimeError(f'guest exited waiting {needle!r}; see {self.log.name}')
            self.log.write(data); self.log.flush(); self.buffer += data
        data = self.buffer
        self.buffer = b''
        return data.decode(errors='replace')

    def command(self, command):
        # Build the marker at execution time so echoed command text cannot satisfy it.
        self.sock.sendall((command + "; RESULT=$?; printf '\\nCHECK_%s=%s\\n' DONE $RESULT\n").encode())
        out = self.wait(b'CHECK_DONE=')
        while not __import__('re').search(r'CHECK_DONE=\d+\r?\n', out):
            out += self.wait(b'\n')
        if not __import__('re').search(r'CHECK_DONE=0\r?\n', out): raise RuntimeError(out[-4000:])
        return out

    def stop(self, reboot=False):
        self.sock.sendall(b'sync; reboot -f\n' if reboot else b'sync; poweroff -f\n')
        self.process.wait(timeout=30)
        self.sock.close(); self.log.close(); self.err.close()

    def close(self):
        if self.process.poll() is None: self.process.terminate(); self.process.wait(timeout=10)
        self.sock.close(); self.log.close(); self.err.close()

def main():
    results = []
    guest = None
    try:
        if not INSTALLER_ISO.is_file(): raise RuntimeError(f'installer ISO not found: {INSTALLER_ISO}')
        with open(DISK, 'wb') as f: f.truncate(SIZE)
        guest = Guest('bios-install', iso=True)
        guest.command('dd if=/dev/nvme0n1 of=/run/before bs=512 count=2048')
        for name, command in [
            ('missing_identity', 'xtos-install /dev/nvme0n1'),
            ('wrong_serial', f'xtos-install /dev/nvme0n1 WRONG {SIZE} ERASE-XTOS'),
            ('wrong_capacity', f'xtos-install /dev/nvme0n1 {SERIAL} 2147483648 ERASE-XTOS'),
            ('wrong_token', f'xtos-install /dev/nvme0n1 {SERIAL} {SIZE} WRONG'),
            ('rom', f'xtos-install /dev/sr0 {SERIAL} {SIZE} ERASE-XTOS'),
        ]:
            guest.command(f'{command}; R=$?; [ "$R" -ne 0 ] && dd if=/dev/nvme0n1 of=/run/after bs=512 count=2048 && cmp /run/before /run/after')
            results.append(name + '=PASS')
        out = guest.command(f'xtos-install /dev/nvme0n1 {SERIAL} {SIZE} ERASE-XTOS')
        assert 'XTOS_INSTALL=PASS' in out
        results.append('bios_iso_install=PASS')
        guest.command('mount /dev/nvme0n1p3 /mnt/xtos; dd if=/dev/nvme0n1 of=/run/before bs=512 count=2048')
        guest.command(f'xtos-install /dev/nvme0n1 {SERIAL} {SIZE} ERASE-XTOS; R=$?; [ "$R" -ne 0 ] && dd if=/dev/nvme0n1 of=/run/after bs=512 count=2048 && cmp /run/before /run/after; CHECK=$?; umount /mnt/xtos; [ "$CHECK" = 0 ]')
        results.append('mounted_partition_rejected=PASS')
        guest.stop(); guest = None
        guest = Guest('bios-disk')
        guest.command('grep -q "xtos.root=" /proc/cmdline && [ "$(cat /sys/kernel/realtime)" = 1 ] && xtos-selftest')
        results.append('bios_disk_modules_sdk_selftest=PASS')
        if 'public' in INSTALLER_ISO.name:
            guest.command('test -f /root/.ssh/authorized_keys && ! test -s /root/.ssh/authorized_keys')
            results.append('public_installer_has_no_embedded_ssh_key=PASS')
        guest.command(f'xtos-install /dev/nvme0n1 {SERIAL} {SIZE} ERASE-XTOS; [ "$?" -ne 0 ]')
        results.append('non_installer_rejected=PASS')
        guest.command('echo XTOS_PERSISTENT_TEST > /root/persistence-test; sync')
        guest.stop(reboot=True); guest = None
        guest = Guest('bios-reboot')
        guest.command('grep -qx XTOS_PERSISTENT_TEST /root/persistence-test && [ "$(cat /sys/kernel/realtime)" = 1 ] && xtos-selftest')
        results.append('bios_reboot_persistence_selftest=PASS')
        guest.stop(); guest = None
        guest = Guest('uefi-disk', efi=True)
        guest.command('[ -d /sys/firmware/efi ] && grep -qx XTOS_PERSISTENT_TEST /root/persistence-test && xtos-selftest')
        results.append('uefi_fallback_disk_selftest=PASS')
        guest.stop(); guest = None
        guest = Guest('uefi-iso', iso=True, efi=True)
        guest.command('grep -qw xtos.installer=1 /proc/cmdline && [ -d /sys/firmware/efi ] && [ "$(cat /sys/kernel/realtime)" = 1 ]')
        results.append('uefi_iso_boot=PASS')
        guest.stop(); guest = None
        report = {'results': results, 'iso': str(INSTALLER_ISO), 'iso_sha256': hashlib.sha256(INSTALLER_ISO.read_bytes()).hexdigest(), 'disk': str(DISK), 'serial': SERIAL, 'bytes': SIZE, 'scope': 'QEMU TCG only; no physical hardware validation'}
        (E / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    finally:
        if guest: guest.close()


if __name__ == "__main__":
    main()
