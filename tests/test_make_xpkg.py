#!/usr/bin/env python3
"""SDK round trip: make_xpkg.py builds a gate-clean, device-installable
package from a payload tree; signing wires the third-party trust chain
end to end (builder -> gate --pubkey -> pkg app install --pubkey)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKE = ROOT / 'scripts' / 'make_xpkg.py'
GATE = ROOT / 'scripts' / 'verify_xpkg.py'
PKG_SRC = ROOT / 'tools' / 'xtos-native' / 'pkg' / 'pkg.c'
TEMPLATE = ROOT / 'sdk' / 'xpkg-template'


def main() -> int:
    if subprocess.run(['openssl', 'version'], capture_output=True).returncode != 0:
        print('skip: openssl unavailable')
        return 0
    failures = []
    with tempfile.TemporaryDirectory(prefix='xtos-sdk-') as td:
        td = Path(td)
        pkg_bin = td / 'pkg'
        mon = ROOT / 'tools' / 'xtos-native' / 'vendor' / 'monocypher'
        subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-static',
                        '-I', str(ROOT / 'tools' / 'xtos-native' / 'common'),
                        '-I', str(mon), '-o', str(pkg_bin), str(PKG_SRC),
                        str(mon / 'monocypher.c'), str(mon / 'monocypher-ed25519.c')], check=True)
        approot = td / 'apps'
        approot.mkdir()

        def run(*args, **kw):
            env = dict(os.environ)
            env['XTOS_APP_ROOT'] = str(approot)
            argv = []
            for a in args:
                if isinstance(a, (list, tuple)):
                    argv.extend(str(x) for x in a)
                else:
                    argv.append(str(a))
            return subprocess.run(argv, capture_output=True,
                                   text=True, env=env, timeout=300, **kw)

        # 1. template tree builds a gate-clean package
        out = td / 'hello-1.0.0.xpkg'
        r = run(['python3', MAKE, '--root', TEMPLATE / 'payload', '--name', 'hello-world',
                 '--version', '1.0.0', '--summary', 'Template hello world',
                 '--vendor', 'XTOS SDK', '--license', 'MIT', '--output', out])
        if r.returncode != 0 or not out.is_file():
            failures.append(f'1 make_xpkg template build failed: {r.stdout!r} {r.stderr!r}')
        else:
            r = run(['python3', GATE, '--xpkg', out])
            verdict = json.loads(r.stdout)
            if r.returncode != 0 or not verdict['passed']:
                failures.append(f'1 gate rejected template package: {verdict["errors"]}')
            elif verdict['signed']:
                failures.append('1 unsigned build reported signed=true')
            else:
                print('  PASS template build + gate')

        # 2. template package installs on the device binary
        if not failures:
            r = run(pkg_bin, 'app', 'install', str(out))
            if r.returncode != 0 or 'status=installed' not in r.stdout:
                failures.append(f'2 template install failed: {r.stdout!r} {r.stderr!r}')
            else:
                r = run(pkg_bin, 'app', 'info', 'hello-world')
                if 'Version:    1.0.0' not in r.stdout or 'hello-world' not in r.stdout:
                    failures.append(f'2 installed template not listed: {r.stdout!r}')
                else:
                    print('  PASS template device install')

        # 3. signed build: gate --pubkey verifies, device install --pubkey verifies
        key = td / 'vendor-ed25519.pem'
        pub = td / 'vendor-pub.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(key)],
                       check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(pub)],
                       check=True, capture_output=True)
        out_signed = td / 'hello-signed-1.0.1.xpkg'
        r = run(['python3', MAKE, '--root', TEMPLATE / 'payload', '--name', 'hello-world',
                 '--version', '1.0.1', '--summary', 'Template hello world',
                 '--vendor', 'XTOS SDK', '--license', 'MIT', '--output', out_signed,
                 '--sign', key])
        if r.returncode != 0 or 'signed=yes' not in r.stdout:
            failures.append(f'3 signed build failed: {r.stdout!r} {r.stderr!r}')
        else:
            r = run(['python3', GATE, '--xpkg', out_signed, '--pubkey', pub])
            verdict = json.loads(r.stdout)
            if r.returncode != 0 or not verdict.get('signature_verified'):
                failures.append(f'3 gate --pubkey failed: {verdict}')
            else:
                print('  PASS signed build + gate --pubkey')
            r = run(pkg_bin, 'app', 'upgrade', str(out_signed), '--pubkey', str(pub))
            if r.returncode != 0 or 'third-party-signature=verified' not in r.stdout:
                failures.append(f'3 device signed upgrade failed: {r.stdout!r} {r.stderr!r}')
            else:
                print('  PASS device signed upgrade')

        # 4. gate --pubkey must fail with the wrong key
        other = td / 'other-ed25519.pem'
        otherpub = td / 'other-pub.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(other)],
                       check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(other), '-pubout', '-out', str(otherpub)],
                       check=True, capture_output=True)
        r = run(['python3', GATE, '--xpkg', out_signed, '--pubkey', otherpub])
        verdict = json.loads(r.stdout)
        if r.returncode == 0:
            failures.append(f'4 gate accepted wrong key: {verdict}')
        elif not any('signature verification failed' in e for e in verdict['errors']):
            failures.append(f'4 unexpected gate error: {verdict["errors"]}')
        else:
            print('  PASS gate rejects wrong key')

        # 5. gate rejects packages with stray xpkg/ members (device parity)
        bad = td / 'stray.xpkg'
        run(['python3', MAKE, '--root', TEMPLATE / 'payload', '--name', 'stray-app',
             '--version', '1.0.0', '--summary', 's', '--vendor', 'v', '--output', bad])
        # repack with an injected stray member
        import tarfile
        repacked = td / 'stray2.xpkg'
        with tarfile.open(bad, 'r:gz') as src, tarfile.open(repacked, 'w:gz') as dst:
            for info in src:
                dst.addfile(info, src.extractfile(info) if info.isreg() else None)
            blob = tarfile.TarInfo('xpkg/notes.txt')
            blob.size = 5
            import io
            dst.addfile(blob, io.BytesIO(b'hello'))
        r = run(['python3', GATE, '--xpkg', repacked])
        verdict = json.loads(r.stdout)
        if r.returncode == 0:
            failures.append(f'5 gate accepted stray xpkg member: {verdict}')
        elif not any('unexpected xpkg member' in e for e in verdict['errors']):
            failures.append(f'5 unexpected error: {verdict["errors"]}')
        else:
            print('  PASS stray xpkg member rejected')

    if failures:
        print(f'FAIL: {len(failures)}')
        for f in failures:
            print('  ' + f)
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
