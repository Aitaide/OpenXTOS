#!/usr/bin/env python3
"""verify_xpkg gate tests: canonical good package passes; tampered, unsafe,
non-canonical and wrong-architecture packages fail with explicit errors."""
import io
import json
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'scripts'))
from test_native_tools_xpkg import build_xpkg, sha  # noqa: E402
import verify_xpkg  # noqa: E402


def elf(machine, cls=2):
    return b'\x7fELF' + bytes([cls, 1, 1, 0]) + b'\x00' * 8 + struct.pack('<HH', 2, machine) + b'\x00' * 8


def run_gate(path, arch='x86_64'):
    ok, verdict = verify_xpkg.verify(path, arch)
    return ok, verdict


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix='xtos-gate-test-') as td:
        td = Path(td)
        good = td / 'good.xpkg'
        build_xpkg(good)
        ok, v = run_gate(good)
        if not ok:
            failures.append(f'good package rejected: {v["errors"]}')
        else:
            print('  PASS good package accepted')
            if v['files'] != 2 or v['name'] != 'demo-app' or len(v['xpkg_sha256']) != 64:
                failures.append(f'good verdict fields wrong: {v}')

        bad = td / 'tampered.xpkg'
        build_xpkg(bad, corrupt_sums=True)
        ok, v = run_gate(bad)
        if ok or not any('hash mismatch' in e for e in v['errors']):
            failures.append(f'tampered package not caught: {v["errors"]}')
        else:
            print('  PASS tampered rejected')

        skip = td / 'skip.xpkg'
        build_xpkg(skip, skip_in_sums=('share/data.txt',))
        ok, v = run_gate(skip)
        if ok or not any('file sets differ' in e for e in v['errors']):
            failures.append(f'set mismatch not caught: {v["errors"]}')
        else:
            print('  PASS set mismatch rejected')

        # non-canonical manifest (no indent) must fail the canonical check
        noncanon = td / 'noncanon.xpkg'
        payload = {'bin/tool': b'echo hi\n'}
        files_meta = [{'path': 'payload/bin/tool', 'sha256': sha(payload['bin/tool']), 'size': 8}]
        manifest = {'schema_version': 1, 'name': 'demo-app', 'version': '1.0.0',
                    'architecture': 'x86_64', 'summary': 'x', 'license': 'MIT',
                    'vendor': 't', 'xtos_min_version': '1.0.0-beta.4', 'files': files_meta}
        with tarfile.open(noncanon, 'w:gz') as tar:
            for arc, mode in (('payload/', 0o755), ('payload/bin/', 0o755), ('xpkg/', 0o755)):
                info = tarfile.TarInfo(arc)
                info.type = tarfile.DIRTYPE
                info.mode = mode
                tar.addfile(info)
            f = tarfile.TarInfo('payload/bin/tool')
            f.size = 8
            f.mode = 0o755
            tar.addfile(f, io.BytesIO(payload['bin/tool']))
            m = tarfile.TarInfo('xpkg/manifest.json')
            mb = json.dumps(manifest, separators=(',', ':')).encode()  # minified: device parser would fail
            m.size = len(mb)
            tar.addfile(m, io.BytesIO(mb))
            s = tarfile.TarInfo('xpkg/SHA256SUMS')
            sb = f'{sha(payload["bin/tool"])}  payload/bin/tool\n'.encode()
            s.size = len(sb)
            tar.addfile(s, io.BytesIO(sb))
        ok, v = run_gate(noncanon)
        if ok or not any('canonical' in e for e in v['errors']):
            failures.append(f'non-canonical manifest not caught: {v["errors"]}')
        else:
            print('  PASS non-canonical manifest rejected')

        # wrong-architecture ELF rejected, matching architecture accepted
        for machine, label, want_ok in ((0x3E, 'x86-64', True), (0xB7, 'aarch64', False)):
            p = td / f'elf-{label}.xpkg'
            binary = elf(machine)
            build_xpkg(p, payload={'bin/app': binary})
            ok, v = run_gate(p)
            if ok != want_ok:
                failures.append(f'ELF {label}: ok={ok} errors={v["errors"]}')
            else:
                print(f'  PASS ELF {label} {"accepted" if want_ok else "rejected"}')

        ko = td / 'ko.xpkg'
        build_xpkg(ko, payload={'driver.ko': b'\x7fELF' + b'\x02' * 16})
        ok, v = run_gate(ko)
        if ok or not any('kernel module' in e for e in v['errors']):
            failures.append(f'kernel module not caught: {v["errors"]}')
        else:
            print('  PASS kernel module rejected')

        junk = td / 'junk.xpkg'
        junk.write_bytes(b'garbage')
        ok, v = run_gate(junk)
        if ok:
            failures.append('junk accepted by gate')
        else:
            print('  PASS junk rejected')

        # CLI form produces exit code + report file
        report = td / 'report.json'
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_xpkg.py'),
                            '--xpkg', str(good), '--report', str(report)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not json.loads(report.read_text())['passed']:
            failures.append(f'gate CLI failed: rc={r.returncode}')
        else:
            print('  PASS gate CLI report')
    if failures:
        print(f'FAIL: {len(failures)}')
        for f in failures:
            print('  ' + f)
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
