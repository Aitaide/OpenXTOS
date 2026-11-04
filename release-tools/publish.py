#!/usr/bin/env python3
"""Create a new release only after checking the complete local artifact set."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from release import release_overlays

REPO = 'Aitaide/OpenXTOS'
TAG = 'v1.0.2'
SOURCE_SHA = 'c24e923717670c4724cdaf24c7c105caee8dc2fb'
EXPECTED = [f'openxtos-{TAG}-x86_64-installer.iso', f'openxtos-{TAG}-arm64.tar.gz', f'openxtos-{TAG}-x86_64-corresponding-source.tar.gz', f'openxtos-{TAG}-arm64-corresponding-source.tar.gz']


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class API:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None, content_type='application/json'):
        url = path if path.startswith('https://') else f'https://api.github.com/repos/{REPO}/{path}'
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json', 'Content-Type': content_type, 'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def upload(self, url, path):
        # File objects stream without materializing a source archive in memory.
        with path.open('rb') as f:
            req = urllib.request.Request(url + '?name=' + path.name, data=f, method='POST', headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/octet-stream', 'Content-Length': str(path.stat().st_size)})
            with urllib.request.urlopen(req, timeout=1200) as r:
                return json.load(r)


def publish(dist, api, recipe_sha):
    for name in EXPECTED:
        p = dist / name
        if not p.is_file() or p.stat().st_size == 0:
            raise RuntimeError('missing artifact: ' + name)
    checks = {p.name: {'size': p.stat().st_size, 'sha256': digest(p)} for p in sorted(dist.iterdir()) if p.is_file() and p.name not in ['SHA256SUMS.txt', 'RELEASE-MANIFEST.json']}
    manifest = {'source_tag': TAG, 'source_commit': SOURCE_SHA, 'release_recipe_commit': recipe_sha, 'applied_release_overlays': {'x86_64': release_overlays(), 'arm64': []}, 'scope': 'Tagged core system source plus disclosed release installer overlay; x86_64 BIOS/UEFI public installer and ARM64 QEMU core boot bundle; not developer Live; QEMU validation only; Secure Boot unsupported', 'artifacts': checks}
    (dist / 'RELEASE-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    files = [p for p in sorted(dist.iterdir()) if p.is_file() and p.name != 'SHA256SUMS.txt']
    (dist / 'SHA256SUMS.txt').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in files))
    files.append(dist / 'SHA256SUMS.txt')
    body = ('OpenXTOS 1.0.2 / Stable 2 source, Linux 7.3.1 mainline PREEMPT_RT.\n\n'
            'Downloads: x86_64 BIOS/UEFI installer ISO and ARM64 QEMU virt boot bundle. '
            'These core-system images are built from the fixed v1.0.2 source; they are not the private developer Live image shown in the website recording.\n\n'
            'Public installer has no embedded maintenance SSH key. Disk installation is manual and erases the selected disk only after disk identity and explicit confirmation. Back up data first. Secure Boot is unsupported. ARM64 requires board-specific integration outside QEMU. QEMU test results do not establish physical hardware latency, device acceptance, medical safety, or production certification.\n\n'
            f'Source commit: `{SOURCE_SHA}`. Release packaging recipe commit: `{recipe_sha}` on `release/v1.0.2-images`. '
            'The separately versioned recipe restores distribution notices and the public installer UEFI loader generation fix, and applies a bounds-checking patch to the x86 Cyton diagnostic read loop; all are included in corresponding-source archives. '
            'Each corresponding-source archive includes project source and exact third-party distribution sources. Retain it with the matching binary when redistributing.\n\n'
            'Verify SHA256SUMS.txt before use. Checksums establish integrity, not independent signature authentication. See RELEASE-MANIFEST.json and QEMU reports for artifact provenance and validation scope.')
    # A create conflict raises before any upload or update. Never upsert a tag.
    release = api.request('POST', 'releases', {'tag_name': TAG, 'target_commitish': SOURCE_SHA, 'name': 'OpenXTOS v1.0.2', 'body': body, 'draft': True, 'prerelease': True, 'generate_release_notes': False})
    rid = release['id']
    upload_url = release['upload_url'].split('{')[0]
    for p in files:
        result = api.upload(upload_url, p)
        if result['size'] != p.stat().st_size or result.get('state') != 'uploaded':
            raise RuntimeError('upload readback mismatch: ' + p.name)
        remote_digest = result.get('digest')
        if remote_digest and remote_digest != 'sha256:' + digest(p):
            raise RuntimeError('GitHub asset digest mismatch: ' + p.name)
    readback = api.request('GET', f'releases/{rid}')
    if sorted(x['name'] for x in readback['assets']) != sorted(x.name for x in files):
        raise RuntimeError('remote asset inventory mismatch')
    result = api.request('PATCH', f'releases/{rid}', {'draft': False, 'prerelease': True, 'make_latest': 'false'})
    if result['draft'] or result['tag_name'] != TAG:
        raise RuntimeError('release publication readback mismatch')
    print(json.dumps({'release_id': rid, 'url': result['html_url'], 'assets': [p.name for p in files]}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('dist', type=Path)
    parser.add_argument('--recipe-sha', required=True)
    args = parser.parse_args()
    publish(args.dist, API(os.environ['GH_TOKEN']), args.recipe_sha)
