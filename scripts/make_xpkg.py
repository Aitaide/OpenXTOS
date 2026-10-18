#!/usr/bin/env python3
"""make_xpkg: build an xpkg application package from a payload tree.

Third-party developer entry point for the XTOS app format (docs/xpkg-spec.md).
Takes a payload directory and produces a canonical, gate-clean .xpkg:

    payload/<tree>       -> payload/<tree>            (regular files only)
    xpkg/manifest.json   canonical json (indent=2, sort_keys) + newline
    xpkg/SHA256SUMS      sha256sum-format payload listing
    xpkg/signature.bin   optional: ed25519 over the exact manifest bytes
                          (requires --sign; produced via openssl pkeyutl)

The output of this tool is accepted by `pkg app install` on the device and
must pass `scripts/verify_xpkg.py` before public distribution. With --sign,
users can install it with `pkg app install app.xpkg --pubkey vendor-pub.pem`.

Key hygiene: the signing key never leaves the build host; only the public
key is distributed with your installation instructions.

Determinism: member order is sorted, mtimes/modes are normalized, so the
same tree always produces the same archive bytes (modulo gzip details
that are stable for a fixed Python version).
"""
import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
VERSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.+~-]{0,63}$')
ARCH_OK = {'x86_64', 'all'}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_payload(root: Path):
    """All regular files under root, as {relpath: bytes}. Rejects everything else."""
    if not root.is_dir():
        raise SystemExit(f'make-xpkg: payload root is not a directory: {root}')
    payload = {}
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            raise SystemExit(f'make-xpkg: symlinks are rejected by the xpkg format: {rel}')
        if p.is_dir():
            continue
        if not p.is_file():
            raise SystemExit(f'make-xpkg: only regular files are allowed: {rel}')
        if p.stat().st_mode & 0o7000:
            raise SystemExit(f'make-xpkg: setuid/setgid/sticky bits are rejected: {rel}')
        data = p.read_bytes()
        if len(data) > 8 * 1024 * 1024 * 1024:
            raise SystemExit(f'make-xpkg: member too large: {rel}')
        payload[rel] = data
    if not payload:
        raise SystemExit('make-xpkg: payload root contains no files')
    return payload


def sign_manifest(manifest_bytes: bytes, key: Path) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        msg = Path(td) / 'manifest.json'
        sig = Path(td) / 'signature.bin'
        msg.write_bytes(manifest_bytes)
        r = subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key),
                            '-rawin', '-in', str(msg), '-out', str(sig)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f'make-xpkg: signing failed: {r.stderr.strip()}')
        out = sig.read_bytes()
    if len(out) != 64:
        raise SystemExit(f'make-xpkg: expected a 64-byte ed25519 signature, got {len(out)}')
    return out


def build(out: Path, payload: dict, *, name, version, arch, summary,
          vendor, license_, min_version, sign_key=None):
    files_meta = [{'path': f'payload/{rel}', 'sha256': sha256(data), 'size': len(data)}
                  for rel, data in sorted(payload.items())]
    manifest = {
        'architecture': arch,
        'files': files_meta,
        'license': license_,
        'name': name,
        'schema_version': 1,
        'summary': summary,
        'vendor': vendor,
        'version': version,
        'xtos_min_version': min_version,
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b'\n'
    sums = ''.join(f'{sha256(data)}  payload/{rel}\n' for rel, data in sorted(payload.items()))

    signature = sign_manifest(manifest_bytes, sign_key) if sign_key else None

    with tarfile.open(out, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
        def add_dir(arcname):
            info = tarfile.TarInfo(arcname)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            tar.addfile(info)

        def add_file(arcname, data, mode=0o644):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))

        add_dir('payload/')
        add_dir('xpkg/')
        seen = set()
        for rel in sorted(payload):
            parts = rel.split('/')
            for i in range(1, len(parts)):
                d = 'payload/' + '/'.join(parts[:i]) + '/'
                if d not in seen:
                    seen.add(d)
                    add_dir(d)
        for rel, data in sorted(payload.items()):
            add_file(f'payload/{rel}', data, 0o755 if rel.startswith('bin/') else 0o644)
        add_file('xpkg/SHA256SUMS', sums.encode())
        add_file('xpkg/manifest.json', manifest_bytes)
        if signature is not None:
            add_file('xpkg/signature.bin', signature)


def main() -> int:
    p = argparse.ArgumentParser(description='Build an XTOS xpkg application package')
    p.add_argument('--root', required=True, type=Path, help='payload tree (its files become payload/)')
    p.add_argument('--name', required=True, help='app name, [a-z0-9][a-z0-9-]{0,63}')
    p.add_argument('--version', required=True, help='app version')
    p.add_argument('--output', required=True, type=Path, help='output .xpkg path')
    p.add_argument('--arch', default='x86_64', choices=sorted(ARCH_OK))
    p.add_argument('--summary', required=True, help='one-line description')
    p.add_argument('--vendor', required=True, help='vendor name')
    p.add_argument('--license', dest='license_', default='Proprietary', help='SPDX id or license name')
    p.add_argument('--min-version', default='1.0.0-beta.4', help='minimum XTOS release')
    p.add_argument('--sign', type=Path, default=None,
                   help='ed25519 private key (PEM): adds xpkg/signature.bin over the manifest bytes')
    args = p.parse_args()

    if not NAME_RE.match(args.name) or args.name == 'apps':
        raise SystemExit(f'make-xpkg: invalid app name: {args.name!r}')
    if not VERSION_RE.match(args.version):
        raise SystemExit(f'make-xpkg: invalid version: {args.version!r}')
    payload = collect_payload(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build(args.output, payload, name=args.name, version=args.version, arch=args.arch,
          summary=args.summary, vendor=args.vendor, license_=args.license_,
          min_version=args.min_version, sign_key=args.sign)
    print(f'wrote {args.output} files={len(payload)} '
          f'bytes={sum(len(d) for d in payload.values())} '
          f'signed={"yes" if args.sign else "no"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
