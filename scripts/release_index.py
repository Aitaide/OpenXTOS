#!/usr/bin/env python3
"""Create and verify the signed XTOS release index (system update source).

The index is the single network answer for "what is the latest XTOS release".
It is published at the root of every XTOS package source as:

    release-index.json        canonical JSON (indent=2, sort_keys, trailing \n)
    release-index.json.sig    detached ed25519 signature (same repository key)

`update` (tools/xtos-native/update) fetches both, verifies the signature
against the pinned public key, and compares against /etc/xtos-release.

Spec format (build input, plain JSON):
{
  "version": "1.0.1",
  "release_name": "Stable 1",
  "channel": "stable",
  "published": "2026-10-23",
  "commit": "<40-hex>",
  "notes_url": "https://github.com/Aitaide/OpenXTOS/releases/tag/v1.0.1",
  "artifacts": [
    {"name": "...", "arch": "x86_64|arm64|all", "kind": "installer-iso|boot-bundle|source|checksums",
     "size": <bytes>, "sha256": "<64-hex>", "url": "https://..."}
  ]
}
`checksums` artifacts may omit size/sha256 (the file itself is a hash list).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from userspace_repo import sha256_file, sign_file, verify_signature, ensure_key  # noqa: E402

VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$')
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+~-]*$')
KINDS = {'installer-iso', 'boot-bundle', 'source', 'checksums'}
ARCHES = {'x86_64', 'arm64', 'all'}


def canonical(data):
    return (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()


def validate_spec(spec):
    for key in ('version', 'release_name', 'channel', 'published', 'commit', 'notes_url', 'artifacts'):
        if key not in spec:
            raise ValueError(f'spec missing key: {key}')
    if not VERSION_RE.fullmatch(spec['version']):
        raise ValueError(f'invalid version: {spec["version"]!r}')
    if not re.fullmatch(r'[0-9a-f]{40}', spec['commit']):
        raise ValueError('commit must be 40 hex chars')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', spec['published']):
        raise ValueError('published must be YYYY-MM-DD')
    if not spec['notes_url'].startswith('https://'):
        raise ValueError('notes_url must be https')
    seen = set()
    for art in spec['artifacts']:
        for key in ('name', 'arch', 'kind', 'url'):
            if key not in art:
                raise ValueError(f'artifact missing key: {key}')
        if not NAME_RE.fullmatch(art['name']) or art['name'] in seen:
            raise ValueError(f'bad or duplicate artifact name: {art["name"]!r}')
        seen.add(art['name'])
        if art['arch'] not in ARCHES:
            raise ValueError(f'bad artifact arch: {art["arch"]!r}')
        if art['kind'] not in KINDS:
            raise ValueError(f'bad artifact kind: {art["kind"]!r}')
        if not art['url'].startswith('https://'):
            raise ValueError(f'artifact url must be https: {art["name"]}')
        if art['kind'] == 'checksums':
            continue
        for key in ('size', 'sha256'):
            if key not in art:
                raise ValueError(f'{art["name"]}: missing {key}')
        if not isinstance(art['size'], int) or art['size'] <= 0:
            raise ValueError(f'{art["name"]}: bad size')
        if not HEX64_RE.fullmatch(art['sha256']):
            raise ValueError(f'{art["name"]}: bad sha256')
    return spec


def cmd_create(args):
    spec = json.loads(Path(args.spec).read_text())
    index = {'schema_version': 1, 'kind': 'xtos-release-index', 'latest': validate_spec(spec)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(canonical(index))
    key = ensure_key(args.key)
    sign_file(key, out)
    print(json.dumps({'status': 'created', 'index': str(out),
                      'version': spec['version'], 'artifacts': len(spec['artifacts']),
                      'index_sha256': sha256_file(out)}, sort_keys=True))


def cmd_verify(args):
    index_path = Path(args.index)
    sig = Path(str(index_path) + '.sig')
    pub = Path(args.public_key)
    if not sig.is_file():
        raise ValueError(f'missing signature: {sig}')
    if not verify_signature(pub, index_path, sig):
        raise ValueError('release index signature invalid')
    raw = index_path.read_bytes()
    index = json.loads(raw)
    if raw != canonical(index):
        raise ValueError('index is not in canonical form')
    if index.get('schema_version') != 1 or index.get('kind') != 'xtos-release-index':
        raise ValueError('unsupported index format')
    validate_spec(index['latest'])
    print(json.dumps({'status': 'verified', 'index': str(index_path),
                      'version': index['latest']['version'],
                      'published': index['latest']['published'],
                      'artifacts': len(index['latest']['artifacts'])}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--spec', required=True)
    create.add_argument('--out', required=True)
    create.add_argument('--key', type=Path, default=Path.home() / '.xtos-keys/userspace-repo-ed25519.pem')
    create.set_defaults(func=cmd_create)
    verify = sub.add_parser('verify')
    verify.add_argument('--index', required=True)
    verify.add_argument('--public-key', required=True)
    verify.set_defaults(func=cmd_verify)
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
