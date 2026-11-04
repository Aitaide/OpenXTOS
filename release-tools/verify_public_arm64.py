#!/usr/bin/env python3
"""Boot downloaded, unmodified ARM64 bundle through its shipped init."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tarfile
import time

ROOT = Path(os.environ.get('XTOS_PUBLIC_READBACK_OUT', '/root/xtos-release-1.0.2/.build/published-v1.0.2'))
ARCHIVE = ROOT / 'openxtos-v1.0.2-arm64.tar.gz'
OUT = ROOT / 'arm64-normal-boot'
OUT.mkdir(parents=True, exist_ok=True)

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    verified = json.loads((ROOT / 'verified.json').read_text())
    if verified.get('status') != 'PASS':
        raise RuntimeError('public byte/source readback required first')
    asset = next(row for row in verified['assets'] if row['name'] == ARCHIVE.name)
    if sha(ARCHIVE) != asset['sha256']:
        raise RuntimeError('downloaded ARM bundle changed')
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        for name in ['Image', 'initramfs-arm64.cpio.gz', 'kernel.config']:
            member = archive.getmember('arm64/' + name)
            if not member.isfile():
                raise RuntimeError('nonregular boot input: ' + name)
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError('missing boot input')
            with (OUT / name).open('wb') as target:
                while chunk := stream.read(1024 * 1024):
                    target.write(chunk)
    args = ['qemu-system-aarch64', '-machine', 'virt', '-cpu', 'cortex-a57', '-m', '2G', '-smp', '2', '-kernel', str(OUT / 'Image'), '-initrd', str(OUT / 'initramfs-arm64.cpio.gz'), '-append', 'console=ttyAMA0 panic=1', '-nographic', '-no-reboot', '-monitor', 'none']
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    if process.stdout is None:
        process.kill()
        process.wait(timeout=10)
        raise RuntimeError('missing serial output pipe')
    serial = process.stdout
    selector = selectors.DefaultSelector()
    selector.register(serial, selectors.EVENT_READ)
    log = bytearray()
    sent = False
    completed = False
    command = "K=0; [ \"$(uname -m)\" = aarch64 ] || K=1; [ \"$(hostname)\" = xtos-arm64 ] || K=1; . /etc/os-release; printf 'observed_version=%s\\n' \"$VERSION_ID\"; [ \"$VERSION_ID\" = 1.0.2 ] || K=1; printf 'observed_kernel=%s\\n' \"$(uname -r)\"; R=$(/usr/bin/xtos-rt-status); printf 'observed_realtime=%s\\n' \"$R\"; [ \"$R\" = 1 ] || K=1; zcat /proc/config.gz | grep -qx CONFIG_PREEMPT_RT=y || K=1; zcat /proc/config.gz | grep -qx CONFIG_XTOS_SCHED=y || K=1; printf 'observed_config='; zcat /proc/config.gz | sha256sum; for M in xtos_bci_core xtos_bci_buffer openbci_cyton; do modprobe \"$M\" && [ -d \"/sys/module/$M\" ] || K=1; done; for T in test_rt test_spsc test_signal; do \"/bin/$T\" >/dev/null 2>&1 || K=1; done; printf 'NORMAL_%s=%s\\n' BOOT \"$K\"; sync; poweroff -f\n"
    deadline = time.monotonic() + 420
    try:
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=1):
                block = serial.read(65536)
                if block:
                    log.extend(block)
                else:
                    selector.unregister(key.fileobj)
            if not sent and (b"can't access tty; job control turned off" in log or re.search(rb'(?:\r?\n)[^\r\n]{0,100}# ', log)):
                if process.stdin is None:
                    raise RuntimeError('missing console input')
                process.stdin.write(command.encode())
                process.stdin.flush()
                sent = True
            if re.search(rb'(?:^|\n)NORMAL_BOOT=0\r?(?:\n|$)', log):
                completed = True
            if process.poll() is not None:
                if process.stdout:
                    rest = process.stdout.read()
                    if rest:
                        log.extend(rest)
                break
        else:
            raise RuntimeError('normal boot console timeout')
        text = bytes(log).decode(errors='replace').replace('\r', '')
        completed = bool(re.search(r'(?:^|\n)NORMAL_BOOT=0(?:\n|$)', text))
        if process.returncode != 0 or not sent or not completed or 'Kernel panic' in text:
            raise RuntimeError('downloaded normal-boot checks failed; see guest.log')
        configs = re.findall(r'^observed_config=([a-f0-9]{64})\s+-\s*$', text, re.MULTILINE)
        if configs != [sha(OUT / 'kernel.config')]:
            raise RuntimeError('running kernel configuration differs from shipped config')
        (OUT / 'verified.json').write_text(json.dumps({'status': 'PASS', 'scope': 'Downloaded unmodified ARM64 normal init, QEMU TCG; no hardware acceptance', 'archive_sha256': asset['sha256'], 'kernel_sha256': sha(OUT / 'Image'), 'initramfs_sha256': sha(OUT / 'initramfs-arm64.cpio.gz'), 'config_sha256': configs[0], 'checks': ['shipped init and sysinit reached', 'version 1.0.2', 'aarch64', 'PREEMPT_RT runtime and configuration', 'XTOS scheduler configuration', 'BCI modules loaded', 'three shipped test binaries', 'clean poweroff']}, indent=2))
        print('downloaded_arm64_normal_boot=pass', flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        selector.close()
        (OUT / 'guest.log').write_bytes(log)

if __name__ == '__main__':
    main()
