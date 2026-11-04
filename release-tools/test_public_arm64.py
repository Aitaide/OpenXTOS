#!/usr/bin/env python3
"""Controller fixtures only: not bootable images or real ARM acceptance."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('arm_readback_fixture', HERE / 'verify_public_arm64.py')
if spec is None or spec.loader is None:
    raise RuntimeError('ARM verifier module could not be loaded')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CONFIG = b'CONFIG_PREEMPT_RT=y\nCONFIG_XTOS_SCHED=y\n'
DIGEST = hashlib.sha256(CONFIG).hexdigest()
results = []
for case in ['pass', 'negative-guest', 'kernel-panic', 'wrong-config', 'nonzero-qemu', 'changed-archive', 'unverified-input']:
    with tempfile.TemporaryDirectory(prefix='arm-controller-fixture-') as directory:
        root = Path(directory)
        archive = root / 'openxtos-v1.0.2-arm64.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            for name, data in [('Image', b'Controlled kernel fixture; not bootable'), ('initramfs-arm64.cpio.gz', b'Controlled initramfs fixture; not bootable'), ('kernel.config', CONFIG)]:
                entry = tarfile.TarInfo('arm64/' + name)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if case == 'changed-archive':
            digest = '0' * 64
        (root / 'verified.json').write_text(json.dumps({'status':'FAIL' if case == 'unverified-input' else 'PASS', 'assets':[{'name':archive.name,'sha256':digest}]}))
        module.ROOT = root
        module.ARCHIVE = archive
        module.OUT = root / 'arm64-normal-boot'
        module.OUT.mkdir()
        observed = '0' * 64 if case == 'wrong-config' else DIGEST
        marker = b'NORMAL_BOOT=1\n' if case == 'negative-guest' else b'NORMAL_BOOT=0\n'
        payload = ('\nobserved_config=' + observed + '  -\n').encode() + marker
        if case == 'kernel-panic':
            payload += b'Kernel panic: fixture\n'
        state = {'reads':0,'popen':False,'killed':False}
        class Serial:
            def read(self, count=-1):
                state['reads'] += 1
                return b'Booted unmodified init\n# ' if state['reads'] == 1 else payload if state['reads'] == 2 else b''
        class Process:
            def __init__(self, args, **kwargs):
                state['popen'] = True
                state['args'] = args
                self.stdout = Serial()
                self.stdin = io.BytesIO()
                self.returncode = None
                state['process'] = self
            def poll(self):
                if state['reads'] >= 2:
                    self.returncode = 7 if case == 'nonzero-qemu' else 0
                return self.returncode
            def kill(self): state['killed'] = True; self.returncode = -9
            def wait(self, **kwargs): return self.returncode
        class Selector:
            def register(self, stream, event): self.stream = stream
            def select(self, **kwargs): return [(SimpleNamespace(fileobj=self.stream),1)]
            def unregister(self, stream): pass
            def close(self): pass
        with patch.object(module.subprocess, 'Popen', Process), patch.object(module.selectors, 'DefaultSelector', Selector):
            try:
                module.main()
            except RuntimeError:
                if case == 'pass': raise
                if (module.OUT / 'verified.json').exists(): raise RuntimeError('failed controller wrote PASS receipt')
                results.append({'case':case,'rejected':True})
            else:
                if case != 'pass': raise RuntimeError('accepted negative controller fixture: ' + case)
                record = json.loads((module.OUT / 'verified.json').read_text())
                args = state['args']
                append = args[args.index('-append')+1]
                if record['status'] != 'PASS' or append != 'console=ttyAMA0 panic=1' or 'init=' in append:
                    raise RuntimeError('normal-init contract weakened')
                if b"printf 'NORMAL_%s=%s" not in state['process'].stdin.getvalue():
                    raise RuntimeError('marker not generated at guest execution time')
                results.append({'case':case,'accepted':True,'normal_init_preserved':True})
        if case in ['changed-archive','unverified-input'] and state['popen']:
            raise RuntimeError('unverified input reached QEMU')
print(json.dumps({'scope':'controller fixtures only; no real ARM boot acceptance','results':results}))
