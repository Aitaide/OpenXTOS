#!/usr/bin/env python3
"""Verify a clean-source Live candidate and its exact QEMU evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODES = ('bios-cd', 'uefi-cd', 'bios-usb', 'uefi-usb')
PAYLOADS = {
    'iso_sha256': 'images/xtos-developer-live-x86_64.iso',
    'kernel_sha256': 'iso/boot/bzImage',
    'initramfs_sha256': 'iso/boot/initramfs.gz',
    'rootfs_squashfs_sha256': 'iso/live/rootfs.squashfs',
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_document(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'cannot read JSON {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'JSON object required: {path}')
    return value


def verify_artifacts(candidate: Path, expected_source: str,
                     kernel_config: Path | None = None) -> dict:
    candidate = Path(candidate).resolve(strict=True)
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected_source):
        raise ValueError('expected source must be a complete Git SHA')
    manifest = read_document(candidate / 'images/build-manifest.json')
    if manifest.get('schema_version') != 1 or manifest.get('architecture') != 'x86_64':
        raise ValueError('unsupported Live manifest schema or architecture')
    if manifest.get('source_git_head') != expected_source:
        raise ValueError('candidate source does not match expected commit')
    if manifest.get('source_state') != 'clean':
        raise ValueError('candidate was not built from clean source')
    if manifest.get('private_key_included') is not False:
        raise ValueError('candidate must explicitly exclude private keys')
    hashes = {}
    for field, relative in PAYLOADS.items():
        path = candidate / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(candidate):
            raise ValueError(f'candidate payload is missing or unsafe: {relative}')
        expected = manifest.get(field)
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError(f'missing or invalid payload hash: {field}')
        actual = sha256(path)
        if actual != expected:
            raise ValueError(f'candidate payload hash mismatch: {relative}')
        hashes[field] = actual
    embedded = verify_iso_payloads(candidate / PAYLOADS['iso_sha256'],
                                   {key: hashes[key] for key in PAYLOADS if key != 'iso_sha256'})
    if embedded != {key: hashes[key] for key in PAYLOADS if key != 'iso_sha256'}:
        raise ValueError('ISO embedded payloads differ from candidate staging files')
    iso_size = (candidate / PAYLOADS['iso_sha256']).stat().st_size
    if type(manifest.get('iso_bytes')) is not int or iso_size <= 0 or manifest['iso_bytes'] != iso_size:
        raise ValueError('candidate ISO byte count mismatch')
    config = Path(kernel_config) if kernel_config is not None else candidate / 'images/kernel.config'
    if not config.is_file() or sha256(config) != manifest.get('kernel_config_sha256'):
        raise ValueError('kernel config missing or hash mismatch')
    return {
        'source_commit': expected_source,
        'source_state': 'clean',
        'candidate': str(candidate),
        'manifest_sha256': sha256(candidate / 'images/build-manifest.json'),
        'kernel_config_sha256': sha256(config),
        'iso_bytes': iso_size,
        **hashes,
    }


def verify_iso_payloads(iso: Path, expected: dict) -> dict:
    iso = Path(iso).resolve(strict=True)
    if iso.is_symlink() or not iso.is_file():
        raise ValueError('ISO is missing or unsafe')
    if not expected:
        raise ValueError('embedded payload hash set is empty')
    with tempfile.TemporaryDirectory(prefix='xtos-iso-') as directory:
        extract = Path(directory)
        command = ['xorriso', '-osirrox', 'on', '-indev', str(iso)]
        for field, relative in PAYLOADS.items():
            if field == 'iso_sha256':
                continue
            target = extract / field
            command += ['-extract', '/' + str(Path(relative).relative_to('iso')), str(target)]
        subprocess.run(command, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, timeout=300)
        actual = {}
        for field, relative in PAYLOADS.items():
            if field == 'iso_sha256':
                continue
            path = extract / field
            if not path.is_file() or path.is_symlink():
                raise ValueError(f'ISO embedded payload missing: {relative}')
            actual[field] = sha256(path)
            if actual[field] != expected.get(field):
                raise ValueError(f'ISO embedded payload hash mismatch: {relative}')
        return actual


def verify_boot_evidence(evidence: Path, artifacts: dict) -> list[str]:
    evidence = Path(evidence)
    document = read_document(evidence / 'results.json')
    if document.get('schema_version') != 1 or document.get('status') != 'pass':
        raise ValueError('Live boot evidence did not pass')
    if document.get('iso_sha256') != artifacts['iso_sha256']:
        raise ValueError('boot evidence belongs to a different ISO')
    rows = document.get('modes')
    if not isinstance(rows, list) or len(rows) != len(MODES):
        raise ValueError('boot evidence must contain exactly four modes')
    names = []
    for row in rows:
        if not isinstance(row, dict) or row.get('status') != 'pass':
            raise ValueError('failed or malformed boot mode')
        mode = row.get('mode')
        if mode not in MODES or mode in names:
            raise ValueError('duplicate or unexpected boot mode')
        names.append(mode)
        checks = row.get('checks')
        if not isinstance(checks, dict):
            raise ValueError('boot checks must be an object')
        for name in ('internal_disk_sha256_unchanged', 'host_key_trusted_via_serial'):
            if checks.get(name) is not True:
                raise ValueError(f'{mode} missing required check: {name}')
        offers = checks.get('authentication_offers')
        if not isinstance(offers, list) or not offers or any(value != 'publickey' for value in offers):
            raise ValueError(f'{mode} did not prove publickey-only authentication')
        for key, marker in (('root_mounts', 'LIVE_MOUNTS=PASS'),
                            ('sdk_selftest', 'guest_userspace=pass'),
                            ('developer_tools', 'DEVELOPER_USER=PASS'),
                            ('readonly_media', 'LIVE_READONLY=PASS')):
            value = checks.get(key)
            if not isinstance(value, str) or marker not in value:
                raise ValueError(f'{mode} missing required result: {key}')
        if read_document(evidence / f'{mode}.result.json') != row:
            raise ValueError(f'{mode} per-mode result does not match aggregate')
        serial = (evidence / f'{mode}.serial.log').read_text(errors='replace')
        if 'XTOS_READY mode=live' not in serial or 'XTOS_DHCP interface=' not in serial:
            raise ValueError(f'{mode} startup readiness not present in serial log')
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path, help='all four boot modes are mandatory')
    parser.add_argument('--kernel-config', type=Path, help='external resolved config pinned by manifest')
    parser.add_argument('--expected-source', default=None)
    parser.add_argument('--report', type=Path,
                        help='new report file; existing paths and links are refused')
    args = parser.parse_args()
    try:
        expected = args.expected_source or subprocess.check_output(
            ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        artifacts = verify_artifacts(args.candidate, expected, args.kernel_config)
        modes = verify_boot_evidence(args.evidence, artifacts)
        result = {'schema': 'xtos-live-acceptance-v1', 'status': 'pass', **artifacts,
                  'boot_modes': modes,
                  'scope': 'artifact integrity and four QEMU boot modes; no physical hardware claim'}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            # Resolve only the parent: exclusive creation must reject even dangling leaf symlinks.
            report = args.report.parent.resolve(strict=True) / args.report.name
            with report.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(result, indent=2, sort_keys=True) + '\n')
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'LIVE_ACCEPTANCE=FAIL {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
