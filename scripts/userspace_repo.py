#!/usr/bin/env python3
"""Create, sign, verify and reconstitute the self-hosted XTOS userspace source.

The userspace lock already carries per-package sha256/size/source_url. This
tool turns a lock into a signed local repository (build-time package source):

    repo/pool/<deb>          exact locked archives
    repo/manifest.json      deterministic index (lock identity + packages)
    repo/SHA256SUMS         sha256 of every pool archive + manifest.json
    repo/SHA256SUMS.sig     detached ed25519 signature over SHA256SUMS
    repo/manifest.json.sig detached ed25519 signature over manifest.json
    repo/public.pem         ed25519 public key of the repository

Builds then reconstitute the flat packages-dir consumed by userspace.py
without contacting any external mirror (fully offline assembly).

Private signing key lives OUTSIDE the repository tree
(XTOS_USERSPACE_REPO_KEY or ~/.xtos-keys/userspace-repo-ed25519.pem, mode 600).
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEY = Path(os.environ.get('XTOS_USERSPACE_REPO_KEY', str(Path.home() / '.xtos-keys/userspace-repo-ed25519.pem')))
ALLOWED_SOURCE_HOSTS = {'deb.debian.org', 'security.debian.org'}
FILENAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.+~_%-]*\.deb$')
KNOWN_REPO_FILES = {'manifest.json', 'SHA256SUMS', 'SHA256SUMS.sig', 'manifest.json.sig', 'public.pem'}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def openssl(args, data=None):
    result = subprocess.run(['openssl', *args], input=data, capture_output=True)
    if result.returncode:
        raise RuntimeError('openssl ' + ' '.join(args[:2]) + ' failed: ' + result.stderr.decode(errors='replace')[-400:])
    return result.stdout


def ensure_key(key_path):
    key_path = Path(key_path)
    if key_path.exists():
        if key_path.stat().st_mode & 0o077:
            raise ValueError(f'signing key {key_path} must be mode 600')
        return key_path
    if key_path.parent != Path.home() / '.xtos-keys':
        key_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
    openssl(['genpkey', '-algorithm', 'ed25519', '-out', str(key_path)])
    os.chmod(key_path, 0o600)
    return key_path


def sign_file(key_path, target_path):
    sig = Path(str(target_path) + '.sig')
    subprocess.run(['openssl', 'pkeyutl', '-sign', '-inkey', str(key_path), '-rawin',
                    '-in', str(target_path), '-out', str(sig)], check=True)
    return sig


def verify_signature(public_key, target_path, sig_path):
    result = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', str(public_key),
                             '-rawin', '-in', str(target_path), '-sigfile', str(sig_path)],
                            capture_output=True)
    return result.returncode == 0


def load_lock(lock_path):
    lock_path = Path(lock_path)
    lock = json.loads(lock_path.read_text())
    if lock.get('schema_version') != 1:
        raise ValueError('unsupported lock schema_version')
    packages = lock.get('packages') or []
    if not packages:
        raise ValueError('lock contains no packages')
    seen = set()
    for package in packages:
        for field in ('name', 'version', 'architecture', 'filename', 'sha256', 'size', 'source_url'):
            if field not in package:
                raise ValueError(f'package missing field {field}')
        if not FILENAME_RE.fullmatch(package['filename']):
            raise ValueError(f'unsafe filename {package["filename"]!r}')
        if package['filename'] in seen:
            raise ValueError(f'duplicate filename {package["filename"]}')
        seen.add(package['filename'])
        url = package['source_url']
        if not url.startswith('https://') or urllib_host(url) not in ALLOWED_SOURCE_HOSTS:
            raise ValueError(f'unsupported source host for {package["name"]}: {url}')
    lock['_path'] = str(lock_path)
    lock['_sha256'] = sha256_file(lock_path)
    return lock


def urllib_host(url):
    return url.split('://', 1)[1].split('/', 1)[0]


def fetch_into(url, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + '.part')
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'xtos-userspace-repo/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response, part.open('wb') as handle:
                shutil.copyfileobj(response, handle, 1024 * 1024)
            os.replace(part, target)
            return
        except Exception as exc:  # noqa: BLE001 - retry any network error
            last_error = exc
            part.unlink(missing_ok=True)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'download failed {url}: {last_error}')


def build_manifest(lock):
    return {
        'schema_version': 1,
        'kind': 'xtos-userspace-repo',
        'lock': {
            'path': Path(lock['_path']).name,
            'sha256': lock['_sha256'],
            'profile': lock.get('profile'),
            'architecture': lock.get('architecture'),
            'repository_suite': lock.get('repository_suite'),
            'package_count': len(lock['packages']),
            'total_bytes': sum(int(p['size']) for p in lock['packages']),
        },
        'packages': [
            {'name': p['name'], 'version': p['version'], 'architecture': p['architecture'],
             'filename': p['filename'], 'sha256': p['sha256'], 'size': int(p['size']),
             'source_url': p['source_url']}
            for p in sorted(lock['packages'], key=lambda p: p['filename'])
        ],
    }


def cmd_create(args):
    lock = load_lock(args.lock)
    repo = Path(args.repo)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / 'pool').mkdir(exist_ok=True)
    manifest_path = repo / 'manifest.json'
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        if existing.get('lock', {}).get('sha256') != lock['_sha256']:
            raise ValueError('repository already holds a manifest for a different lock')
    manifest = build_manifest(lock)
    key_path = ensure_key(args.key)

    def sync_one(package):
        target = repo / 'pool' / package['filename']
        if target.exists() and sha256_file(target) == package['sha256'] and target.stat().st_size == int(package['size']):
            return ('reused', package['filename'])
        if args.cache_dir:
            cached = Path(args.cache_dir) / package['filename']
            if cached.exists() and sha256_file(cached) == package['sha256']:
                shutil.copyfile(cached, target)
                return ('copied', package['filename'])
        fetch_into(package['source_url'], target)
        if sha256_file(target) != package['sha256'] or target.stat().st_size != int(package['size']):
            raise ValueError(f'{package["filename"]}: fetched bytes do not match lock')
        return ('downloaded', package['filename'])

    stats = {'reused': 0, 'copied': 0, 'downloaded': 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for status, _name in pool.map(sync_one, lock['packages']):
            stats[status] += 1
    for package in lock['packages']:
        target = repo / 'pool' / package['filename']
        if sha256_file(target) != package['sha256']:
            raise ValueError(f'{package["filename"]}: final verification failed')

    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    sums = repo / 'SHA256SUMS'
    lines = [f'{sha256_file(manifest_path)}  manifest.json']
    for package in manifest['packages']:
        lines.append(f'{package["sha256"]}  pool/{package["filename"]}')
    sums.write_text('\n'.join(sorted(lines)) + '\n')
    sign_file(key_path, sums)
    sign_file(key_path, manifest_path)
    public = repo / 'public.pem'
    subprocess.run(['openssl', 'pkey', '-in', str(key_path), '-pubout', '-out', str(public)], check=True)
    if args.publish_public_key:
        dest = Path(args.publish_public_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(public, dest)
    print(json.dumps({'status': 'created', 'repo': str(repo), 'lock': Path(lock['_path']).name,
                      'lock_sha256': lock['_sha256'], 'packages': len(lock['packages']),
                      'pool_bytes': manifest['lock']['total_bytes'], 'stats': stats}, sort_keys=True))


def repo_files(repo):
    files = {'manifest.json', 'SHA256SUMS', 'SHA256SUMS.sig', 'manifest.json.sig', 'public.pem'}
    for entry in (repo / 'pool').iterdir():
        if entry.is_file() and not entry.name.endswith('.part'):
            files.add('pool/' + entry.name)
    return files


def verify_repo(repo, public_key=None, lock_path=None):
    repo = Path(repo)
    public_key = Path(public_key) if public_key else repo / 'public.pem'
    for name in ('manifest.json', 'SHA256SUMS', 'SHA256SUMS.sig', 'manifest.json.sig', 'public.pem'):
        if not (repo / name).is_file():
            raise ValueError(f'repository missing {name}')
    if not verify_signature(public_key, repo / 'manifest.json', repo / 'manifest.json.sig'):
        raise ValueError('manifest.json signature invalid')
    if not verify_signature(public_key, repo / 'SHA256SUMS', repo / 'SHA256SUMS.sig'):
        raise ValueError('SHA256SUMS signature invalid')
    manifest = json.loads((repo / 'manifest.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('kind') != 'xtos-userspace-repo':
        raise ValueError('unsupported repository manifest')
    listed = {}
    for line in (repo / 'SHA256SUMS').read_text().splitlines():
        digest, _, path = line.partition('  ')
        if len(digest) != 64 or not path or path in listed:
            raise ValueError(f'malformed SHA256SUMS entry: {line!r}')
        listed[path] = digest
    if 'manifest.json' not in listed:
        raise ValueError('SHA256SUMS does not cover manifest.json')
    if sha256_file(repo / 'manifest.json') != listed['manifest.json']:
        raise ValueError('manifest.json does not match SHA256SUMS')
    pool_names = {'pool/' + p['filename'] for p in manifest['packages']}
    if pool_names != {p for p in listed if p.startswith('pool/')}:
        raise ValueError('SHA256SUMS pool coverage differs from manifest')
    actual = repo_files(repo)
    expected = set(listed) | {'SHA256SUMS', 'SHA256SUMS.sig', 'manifest.json.sig', 'public.pem'}
    stray = actual - expected
    part_files = {p for p in stray if p.endswith('.part')}
    if part_files:
        raise ValueError(f'incomplete downloads present: {sorted(part_files)}')
    if stray:
        raise ValueError(f'unlisted repository files: {sorted(stray)}')
    for package in manifest['packages']:
        entry = 'pool/' + package['filename']
        target = repo / entry
        if not target.is_file():
            raise ValueError(f'missing archive {entry}')
        if sha256_file(target) != package['sha256'] or target.stat().st_size != int(package['size']):
            raise ValueError(f'{entry}: bytes do not match signed index')
    if lock_path:
        lock = load_lock(lock_path)
        if lock['_sha256'] != manifest['lock']['sha256']:
            raise ValueError('repository was built from a different lock')
        by_name = {p['name']: p for p in manifest['packages']}
        for package in lock['packages']:
            if by_name.get(package['name']) != {'name': package['name'], 'version': package['version'],
                                                'architecture': package['architecture'], 'filename': package['filename'],
                                                'sha256': package['sha256'], 'size': int(package['size']),
                                                'source_url': package['source_url']}:
                raise ValueError(f'{package["name"]}: repository entry differs from lock')
    return manifest


def cmd_verify(args):
    manifest = verify_repo(args.repo, args.public_key, args.lock)
    print(json.dumps({'status': 'verified', 'repo': str(Path(args.repo)),
                      'packages': len(manifest['packages']),
                      'pool_bytes': manifest['lock']['total_bytes'],
                      'lock_sha256': manifest['lock']['sha256']}, sort_keys=True))


def cmd_populate(args):
    manifest = verify_repo(args.repo, args.public_key, args.lock)
    packages_dir = Path(args.packages_dir)
    if packages_dir.exists() and any(packages_dir.iterdir()):
        raise ValueError('packages-dir must be fresh/empty; never mix generations')
    packages_dir.mkdir(parents=True, exist_ok=True)
    for package in manifest['packages']:
        target = packages_dir / package['filename']
        shutil.copyfile(Path(args.repo) / 'pool' / package['filename'], target)
        if sha256_file(target) != package['sha256'] or target.stat().st_size != int(package['size']):
            raise ValueError(f'{package["filename"]}: copied bytes failed verification')
    print(json.dumps({'status': 'populated', 'packages_dir': str(packages_dir),
                      'packages': len(manifest['packages']),
                      'bytes': manifest['lock']['total_bytes'],
                      'for_offline_assembly': 'scripts/build_userspace.sh --lock <json> --packages-dir <dir> --output-dir <fresh-dir>'}, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create', help='build and sign a repository from a userspace lock')
    create.add_argument('--lock', required=True)
    create.add_argument('--repo', required=True)
    create.add_argument('--cache-dir', help='reuse previously fetched archives matching lock hashes')
    create.add_argument('--key', type=Path, default=DEFAULT_KEY)
    create.add_argument('--workers', type=int, default=6)
    create.add_argument('--publish-public-key', help='copy the repository public key to this path (commit it)')
    create.set_defaults(func=cmd_create)
    verify = sub.add_parser('verify', help='verify signatures, index and every archive')
    verify.add_argument('--repo', required=True)
    verify.add_argument('--public-key')
    verify.add_argument('--lock')
    verify.set_defaults(func=cmd_verify)
    populate = sub.add_parser('populate', help='reconstitute a flat packages-dir from a verified repository')
    populate.add_argument('--repo', required=True)
    populate.add_argument('--packages-dir', required=True)
    populate.add_argument('--public-key')
    populate.add_argument('--lock')
    populate.set_defaults(func=cmd_populate)
    args = parser.parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
