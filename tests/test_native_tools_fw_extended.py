#!/usr/bin/env python3
"""Production-C fw lifecycle regression, strictly in a fresh network namespace.

Unavailable isolation returns 77 (guest pending), never an all-pass verdict.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / "tools/xtos-native/fw/fw.c"


def build_fw(tmpdir: Path) -> Path:
    out = tmpdir / "fw"
    subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                    "-I", str(ROOT / "tools/xtos-native/common"),
                    "-o", str(out), str(FW)], check=True)
    return out


def run(binary: Path, *args: str):
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=20)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--inside":
        assert os.readlink('/proc/self/ns/net') != sys.argv[2], 'refusing host netns'
    else:
        if sys.platform != "linux" or not all(shutil.which(p) for p in ('nft', 'unshare')):
            print("SKIP guest pending: linux/nft/unshare unavailable")
            return 77
        probe = subprocess.run(['unshare', '-Urn', 'nft', 'list', 'ruleset'],
                               capture_output=True, text=True)
        if probe.returncode:
            print("SKIP guest pending: isolation/CAP_NET_ADMIN unavailable: " + probe.stderr)
            return 77
        return subprocess.run(['unshare', '-Urn', sys.executable, __file__, '--inside',
                               os.readlink('/proc/self/ns/net')], timeout=60).returncode

    import tempfile
    failures = []
    with tempfile.TemporaryDirectory(prefix='fw-extended-', dir=ROOT / '.build') as directory:
        fw = build_fw(Path(directory))
        def check(name, args, want_rc=0, marker=None, absent=None):
            result = run(fw, *args)
            good = (result.returncode == want_rc and
                    (marker is None or marker in result.stdout) and
                    (absent is None or absent not in result.stdout))
            print(f"{'PASS' if good else 'FAIL'} {name}")
            if not good:
                failures.append((name, result.returncode, result.stdout, result.stderr))
        try:
            check('enable', ('enable',), marker='XTOS_FW_ENABLE')
            check('allow tcp 22', ('allow', '--proto', 'tcp', '--port', '22', '--from', '192.0.2.2/32'), marker='FW_ALLOW')
            check('allow udp 53', ('allow', '--proto', 'udp', '--port', '53'), marker='FW_ALLOW')
            check('deny tcp 8080', ('deny', '--proto', 'tcp', '--port', '8080'), marker='FW_DENY')
            check('list shows rules', ('list',), marker='dport 22')
            check('list shows deny', ('list',), marker='dport 8080 drop')
            check('delete 8080', ('delete', '--proto', 'tcp', '--port', '8080'), marker='FW_DELETE')
            check('deleted gone', ('list',), absent='dport 8080')
            check('flush', ('flush',), marker='FW_FLUSH')
            check('status enabled', ('status',), marker='enabled=true')
            check('disable', ('disable', '--confirm'), marker='XTOS_FW_DISABLE')
            check('status disabled', ('status',), marker='enabled=false')
            for name, proto, port in [('bad proto', 'sctp', '22'),
                                      ('bad port high', 'tcp', '70000'),
                                      ('port zero', 'tcp', '0')]:
                result = run(fw, 'allow', '--proto', proto, '--port', port)
                good = result.returncode == 1 and result.stderr.startswith('fw:')
                print(f"{'PASS' if good else 'FAIL'} reject {name}")
                if not good:
                    failures.append((name, result.returncode, result.stdout, result.stderr))
        finally:
            run(fw, 'disable', '--confirm')
    if failures:
        print(f"FAIL: {len(failures)}")
        for failure in failures:
            print(failure)
        return 1
    print('ALL PASS (isolated network namespace)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
