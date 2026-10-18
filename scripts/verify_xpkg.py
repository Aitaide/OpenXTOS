#!/usr/bin/env python3
"""xpkg publishing gate: full structural + integrity + ELF policy check.

Runs the same rules `pkg app install` enforces on the device, plus archive
canonical-form and ELF architecture policy that only the gate checks.
A package must pass this gate before it may be listed in any XTOS source.
Exit 0 + JSON verdict on success; nonzero with error list otherwise.
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
MAX_FILES = 50000
MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
ELF_MACHINES = {'x86_64': 0x3E}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def member_ok(name):
    if not name or len(name) >= 1024 or name.startswith('/'):
        return False
    if not (name.startswith('payload/') or name.startswith('xpkg/')):
        return False
    for part in name.rstrip('/').split('/'):
        if not part or len(part) >= 256 or part in ('.', '..') or any(ord(c) < 32 for c in part):
            return False
    return True


def verify(path, arch, pubkey=None):
    errors, checks = [], []
    xpkg = Path(path)
    if not xpkg.is_file():
        return False, {'errors': [f'not a file: {path}'], 'checks': []}
    raw = xpkg.read_bytes()
    members = {}
    ancestors = set()
    try:
        with tarfile.open(xpkg, 'r:gz') as tar:
            for info in tar:
                name = info.name if info.type == tarfile.REGTYPE else info.name.rstrip('/')
                if info.type == tarfile.DIRTYPE and name in ('payload', 'xpkg'):
                    continue  # root directory members
                if not member_ok(name):
                    errors.append(f'unsafe member name: {info.name!r}')
                    continue
                if info.type not in (tarfile.REGTYPE, tarfile.DIRTYPE):
                    errors.append(f'forbidden member type: {name}')
                    continue
                if info.type == tarfile.REGTYPE and info.mode & 0o7000:
                    errors.append(f'setuid/setgid/sticky payload: {name}')
                    continue
                if info.type == tarfile.REGTYPE:
                    if len(members) >= MAX_FILES:
                        errors.append('too many files')
                        break
                    handle = tar.extractfile(info)
                    if handle is None:
                        errors.append(f'unreadable member: {name}')
                        continue
                    data = handle.read()
                    members[name] = (data, info.mode)
                base = name
                if '/' in base:
                    parts = base.split('/')
                    ancestors.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    except tarfile.TarError as exc:
        errors.append(f'not a readable tar.gz: {exc}')
        return False, {'errors': errors, 'checks': []}
    if not members:
        errors.append('archive is empty')
    for name in members:
        if name in ancestors:
            errors.append(f'member doubles as directory prefix: {name}')
    if errors:
        return False, {'errors': errors, 'checks': []}

    def need(name):
        if name not in members:
            errors.append(f'missing archive member: {name}')
            return None
        return members[name][0]

    sums_raw = need('xpkg/SHA256SUMS')
    manifest_raw = need('xpkg/manifest.json')
    if sums_raw is None or manifest_raw is None:
        return False, {'errors': errors, 'checks': []}
    if len(manifest_raw) > MAX_MANIFEST:
        errors.append('manifest oversized')
    checks.append('members')
    # xpkg/ holds metadata only; the device installer rejects anything else.
    allowed_xpkg = {'xpkg/SHA256SUMS', 'xpkg/manifest.json', 'xpkg/signature.bin'}
    for name in members:
        if name.startswith('xpkg/') and name not in allowed_xpkg:
            errors.append(f'unexpected xpkg member: {name}')
        if name == 'xpkg/signature.bin' and len(members[name][0]) != 64:
            errors.append('xpkg/signature.bin must be exactly 64 bytes')
    if errors:
        return False, {'errors': errors, 'checks': checks}
    try:
        manifest = json.loads(manifest_raw)
    except json.JSONDecodeError as exc:
        return False, {'errors': errors + [f'manifest not JSON: {exc}'], 'checks': checks}
    if json.dumps(manifest, indent=2, sort_keys=True).encode() + b'\n' != manifest_raw:
        errors.append('manifest not canonical (indent=2, sort_keys, trailing newline)')
    checks.append('manifest-canonical')
    for key in ('schema_version', 'name', 'version', 'architecture', 'summary', 'license', 'vendor', 'xtos_min_version', 'files'):
        if key not in manifest:
            errors.append(f'manifest missing key: {key}')
    if errors:
        return False, {'errors': errors, 'checks': checks}
    if manifest['schema_version'] != 1:
        errors.append('unsupported schema_version')
    if not NAME_RE.fullmatch(manifest['name']) or manifest['name'] == 'apps':
        errors.append(f'invalid app name: {manifest["name"]!r}')
    if not VERSION_RE.fullmatch(manifest['version']):
        errors.append(f'invalid version: {manifest["version"]!r}')
    if manifest['architecture'] not in ARCH_OK:
        errors.append(f'invalid architecture: {manifest["architecture"]!r}')
    checks.append('manifest-schema')

    sums = {}
    for line in sums_raw.decode().splitlines():
        digest, sep, rel = line.partition('  ')
        if not sep or len(digest) != 64 or not member_ok(rel) or not rel.startswith('payload/'):
            errors.append(f'malformed SHA256SUMS line: {line!r}')
            continue
        if rel in sums:
            errors.append(f'duplicate sums entry: {rel}')
        sums[rel] = digest
    checks.append('sums-parse')

    files_meta = {}
    for entry in manifest['files']:
        rel = entry.get('path', '')
        if not member_ok(rel) or not rel.startswith('payload/') or rel in files_meta:
            errors.append(f'bad manifest files entry: {entry}')
            continue
        files_meta[rel] = entry
    if set(sums) != set(files_meta):
        errors.append(f'sums/manifest file sets differ: sums={len(sums)} manifest={len(files_meta)}')
    if set(sums) != {n for n in members if n.startswith('payload/')}:
        errors.append('payload set on disk differs from sums')
    total = 0
    for rel, digest in sums.items():
        data = members.get(rel, (b'',))[0]
        total += len(data)
        entry = files_meta.get(rel, {})
        if sha256(data) != digest:
            errors.append(f'hash mismatch: {rel}')
        if entry.get('sha256') != digest:
            errors.append(f'manifest hash mismatch: {rel}')
        if entry.get('size') != len(data):
            errors.append(f'size mismatch: {rel}')
    if total > MAX_BYTES:
        errors.append('payload exceeds 2GiB cap')
    checks.append('integrity')
    machine_want = ELF_MACHINES.get(arch if arch != 'all' else 'x86_64', 0x3E)
    for rel, (data, _mode) in members.items():
        if not rel.startswith('payload/'):
            continue
        base = Path(rel).name
        if base.endswith(('.ko', '.ko.xz', '.ko.gz', '.o')):
            errors.append(f'kernel module/object in payload: {rel}')
        if data[:4] == b'\x7fELF':
            if len(data) < 20:
                errors.append(f'truncated ELF: {rel}')
            elif int.from_bytes(data[18:20], 'little') != machine_want:
                errors.append(f'ELF machine mismatch in {rel}: want {machine_want:#x}')
    checks.append('elf-policy')
    # Optional third-party signature: with --pubkey the package must carry a
    # 64-byte ed25519 signature over the exact manifest bytes.
    signature_entry = members.get('xpkg/signature.bin')
    signature = signature_entry[0] if signature_entry is not None else None
    if pubkey:
        if signature is None:
            errors.append('no xpkg/signature.bin in package (use make_xpkg.py --sign)')
        else:
            with tempfile.TemporaryDirectory() as td:
                msg, sig = Path(td) / 'manifest.json', Path(td) / 'signature.bin'
                msg.write_bytes(manifest_raw)
                sig.write_bytes(signature)
                r = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin',
                                    '-inkey', str(pubkey), '-rawin',
                                    '-in', str(msg), '-sigfile', str(sig)],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    errors.append('third-party signature verification failed')
        checks.append('signature')
    verdict = {
        'passed': not errors,
        'xpkg': str(xpkg),
        'xpkg_sha256': sha256(raw),
        'name': manifest.get('name'),
        'version': manifest.get('version'),
        'architecture': manifest.get('architecture'),
        'files': len(sums),
        'bytes': total,
        'signed': signature is not None,
        'signature_verified': bool(pubkey) and not any('signature' in e for e in errors),
        'checks': checks,
        'errors': errors,
    }
    return not errors, verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xpkg', required=True)
    parser.add_argument('--arch', default='x86_64')
    parser.add_argument('--pubkey', default=None,
                        help='verify xpkg/signature.bin (ed25519 over the manifest bytes)')
    parser.add_argument('--report')
    args = parser.parse_args()
    ok, verdict = verify(args.xpkg, args.arch, args.pubkey)
    if args.report:
        Path(args.report).write_text(json.dumps(verdict, indent=2, sort_keys=True) + '\n')
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
