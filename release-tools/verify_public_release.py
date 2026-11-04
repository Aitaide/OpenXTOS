#!/usr/bin/env python3
"""Read released assets back; verify public bytes and matching source contents."""
from email.parser import Parser
import csv
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(os.environ.get('XTOS_RELEASE_ROOT', '/root/xtos-release-1.0.2'))
OUT = Path(os.environ.get('XTOS_PUBLIC_READBACK_OUT', str(ROOT / '.build/published-v1.0.2')))
OUT.mkdir(parents=True, exist_ok=True)
API = 'https://api.github.com/repos/Aitaide/OpenXTOS/releases/tags/v1.0.2'
SHA = 'c24e923717670c4724cdaf24c7c105caee8dc2fb'
CANONICAL = Path(os.environ.get('XTOS_TAGGED_SOURCE', '/root/xtos-development/xtos'))


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'OpenXTOS-release-byte-verifier'}), timeout=90)


def project_inventory():
    raw = subprocess.check_output(['git', 'ls-tree', '-r', '-z', SHA], cwd=CANONICAL)
    rows = {}
    for record in raw.split(b'\0'):
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        rows[path.decode()] = {'mode': mode, 'kind': kind, 'oid': oid}
    return rows


def source_archive(path, expected):
    seen = set()
    errors = []
    runtime = {}
    descriptors = []
    source_manifest = None
    provenance = None
    recipe_members = {}
    prefix = 'project-source/openxtos-v1.0.2/'
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            if member.isdir():
                continue
            if name.startswith(prefix):
                rel = name[len(prefix):]
                row = expected.get(rel)
                if rel in seen or row is None:
                    errors.append('unexpected or duplicate project path: ' + rel)
                    continue
                seen.add(rel)
                if member.issym():
                    content = member.linkname.encode()
                    oid = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
                    if row['mode'] != '120000' or oid != row['oid']:
                        errors.append('project symlink mismatch: ' + rel)
                elif member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise RuntimeError('missing project member stream')
                    h = hashlib.sha1(b'blob ' + str(member.size).encode() + b'\0')
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(block)
                    executable = bool(member.mode & 0o111)
                    if row['kind'] != 'blob' or row['mode'] not in ['100644', '100755'] or h.hexdigest() != row['oid'] or executable != (row['mode'] == '100755'):
                        errors.append('project file type, blob or mode mismatch: ' + rel)
                else:
                    errors.append('unsupported project member type: ' + rel)
            elif name.startswith('runtime-sources/') and member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError('missing runtime member stream')
                h = hashlib.sha256()
                content = bytearray()
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
                    if name.endswith('.dsc'):
                        content.extend(block)
                runtime[name] = {'bytes': member.size, 'sha256': h.hexdigest()}
                if name.endswith('.dsc'):
                    descriptors.append((name, bytes(content).decode()))
            elif name in ['SOURCE_PACKAGES.tsv', 'PROVENANCE.json'] or name.startswith('release-recipe/'):
                if not member.isfile():
                    errors.append('non-regular source metadata: ' + name)
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError('missing metadata member stream')
                content = stream.read()
                if name == 'SOURCE_PACKAGES.tsv':
                    source_manifest = list(csv.DictReader(io.StringIO(content.decode()), delimiter='\t'))
                elif name == 'PROVENANCE.json':
                    provenance = json.loads(content)
                else:
                    rel = name.removeprefix('release-recipe/')
                    if rel in recipe_members:
                        errors.append('duplicate recipe member: ' + rel)
                    recipe_members[rel] = hashlib.sha256(content).hexdigest()
    missing = sorted(set(expected) - seen)
    if missing:
        errors.append('missing project members: ' + str(missing[:12]))
    descriptor_versions = []
    for name, text in descriptors:
        if text.startswith('-----BEGIN PGP SIGNED MESSAGE-----'):
            text = text.split('\n\n', 1)[1].split('-----BEGIN PGP SIGNATURE-----', 1)[0]
            text = '\n'.join(line[2:] if line.startswith('- ') else line for line in text.splitlines())
        fields = Parser().parsestr(text)
        descriptor_versions.append((fields.get('Source'), fields.get('Version')))
        entries = fields.get('Checksums-Sha256', '').splitlines()
        if not any(line.strip() for line in entries):
            errors.append('empty source descriptor: ' + name)
        for line in entries:
            if not line.strip():
                continue
            digest, size, filename = line.split()
            key = str(PurePosixPath(name).parent / filename)
            if runtime.get(key) != {'bytes': int(size), 'sha256': digest}:
                errors.append('runtime descriptor mismatch: ' + key)
    if not descriptors:
        errors.append('no corresponding runtime source descriptors')
    if not source_manifest:
        errors.append('empty or missing source package inventory')
    else:
        declared = [(row['source_package'], row['source_version']) for row in source_manifest]
        if sorted(declared) != sorted(descriptor_versions) or len(declared) != len(set(declared)):
            errors.append('source descriptor inventory differs from declared packages')
    recipe_sha = '110048c955c71a0701f5eee04351ab7a4903b0f6'
    if not provenance or provenance.get('source_commit') != SHA or provenance.get('recipe_commit') != recipe_sha:
        errors.append('source provenance does not match declared source and recipe')
    else:
        for overlay in provenance.get('applied_release_overlays', []):
            rel = overlay['recipe_source'].removeprefix('release-tools/')
            if recipe_members.get(rel) != overlay['sha256']:
                errors.append('source overlay digest mismatch: ' + rel)
    tracked = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', recipe_sha, '--', 'release-tools'], cwd=ROOT, text=True).splitlines()
    wanted = {name.removeprefix('release-tools/'): hashlib.sha256(subprocess.check_output(['git', 'show', recipe_sha + ':' + name], cwd=ROOT)).hexdigest() for name in tracked}
    if recipe_members != wanted:
        errors.append('packaged recipe differs from exact committed recipe inventory')
    result = {'project_files': len(seen), 'expected_project_files': len(expected), 'runtime_files': len(runtime), 'source_descriptors': len(descriptors), 'recipe_files': len(recipe_members), 'errors': errors}
    if errors:
        raise RuntimeError(json.dumps(result))
    return result


def main():
    with get(API) as response:
        release = json.load(response)
    if release['draft'] or release['tag_name'] != 'v1.0.2':
        raise RuntimeError('wrong public release state')
    (OUT / 'release.json').write_text(json.dumps(release, indent=2))
    assets = release['assets']
    names = {x['name'] for x in assets}
    required = {'openxtos-v1.0.2-x86_64-installer.iso', 'openxtos-v1.0.2-arm64.tar.gz', 'openxtos-v1.0.2-x86_64-corresponding-source.tar.gz', 'openxtos-v1.0.2-arm64-corresponding-source.tar.gz', 'SHA256SUMS.txt', 'RELEASE-MANIFEST.json'}
    if not required.issubset(names) or len(names) != len(assets):
        raise RuntimeError('incomplete or duplicate release inventory')
    results = []
    for asset in assets:
        path = OUT / asset['name']
        h = hashlib.sha256()
        count = 0
        with get(asset['browser_download_url']) as response, path.open('wb') as f:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                f.write(block)
                h.update(block)
                count += len(block)
        if count != asset['size'] or (asset.get('digest') and asset['digest'] != 'sha256:' + h.hexdigest()):
            raise RuntimeError('downloaded asset mismatch: ' + path.name)
        row = {'name': path.name, 'bytes': count, 'sha256': h.hexdigest()}
        if path.name.endswith('-corresponding-source.tar.gz'):
            row['source_validation'] = source_archive(path, project_inventory())
            path.unlink()
            row['retained'] = 'digest and full source-member verification only; public source archive remains at release URL'
        results.append(row)
        with (OUT / 'asset-readback.jsonl').open('a') as f:
            f.write(json.dumps(row) + '\n')
        print(json.dumps(row), flush=True)
    checks = {}
    for line in (OUT / 'SHA256SUMS.txt').read_text().splitlines():
        digest, name = line.split(None, 1)
        checks[name.strip().lstrip('*')] = digest
    for row in results:
        if row['name'] != 'SHA256SUMS.txt' and checks.get(row['name']) != row['sha256']:
            raise RuntimeError('checksum-list mismatch: ' + row['name'])
    manifest = json.loads((OUT / 'RELEASE-MANIFEST.json').read_text())
    recipe_sha = '110048c955c71a0701f5eee04351ab7a4903b0f6'
    if manifest.get('source_commit') != SHA or manifest.get('source_tag') != 'v1.0.2' or manifest.get('release_recipe_commit') != recipe_sha:
        raise RuntimeError('release manifest provenance mismatch')
    expected_artifacts = {row['name']: {'size': row['bytes'], 'sha256': row['sha256']} for row in results if row['name'] not in ['SHA256SUMS.txt', 'RELEASE-MANIFEST.json']}
    if manifest.get('artifacts') != expected_artifacts:
        raise RuntimeError('release manifest artifact coverage mismatch')
    build = json.loads((OUT / 'x86_64-build-manifest.json').read_text())
    qemu = json.loads((OUT / 'x86_64-qemu-results.json').read_text())
    iso = expected_artifacts['openxtos-v1.0.2-x86_64-installer.iso']
    if build.get('git_head') != SHA or build.get('dirty') or build.get('installer_variant') != 'public':
        raise RuntimeError('x86 build source or public-variant mismatch')
    if build.get('installer_artifact', {}).get('sha256') != iso['sha256'] or qemu.get('iso_sha256') != iso['sha256']:
        raise RuntimeError('x86 validation does not bind downloaded image')
    qemu_checks = qemu.get('results')
    required_checks = {'bios_iso_install=PASS', 'bios_disk_modules_sdk_selftest=PASS', 'public_installer_has_no_embedded_ssh_key=PASS', 'bios_reboot_persistence_selftest=PASS', 'uefi_fallback_disk_selftest=PASS', 'uefi_iso_boot=PASS'}
    if not isinstance(qemu_checks, list) or not required_checks.issubset(qemu_checks) or any(not value.endswith('=PASS') for value in qemu_checks):
        raise RuntimeError('x86 QEMU validation inventory incomplete or failing')
    (OUT / 'verified.json').write_text(json.dumps({'status': 'PASS', 'release_id': release['id'], 'release_url': release['html_url'], 'assets': results, 'x86_qemu_checks': qemu_checks}, indent=2))
    print('public_release_byte_readback=pass', flush=True)


if __name__ == '__main__':
    main()
